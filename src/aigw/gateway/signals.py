"""Routing signals (docs/spec/04 §5.1): per-deployment latency EWMA, in-flight counts (local admission control)
and vLLM queue pressure published by the worker through Valkey.

Latency and in-flight state are per gateway replica and in-process: they need no coordination, and admission
control deliberately stays local (a replica only limits what it sends itself). Queue pressure comes from the
worker's ``/metrics`` scrape (``deployment_health``) and reaches gateways as ``dq:{deployment_id}`` JSON in Valkey
with a short TTL; when Valkey is unreachable the signal is simply absent and routing falls back to weights.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import redis.asyncio as redis


class LatencyTracker:
    """Exponentially weighted moving average of time-to-first-byte per deployment (seconds)."""

    def __init__(self, alpha: float = 0.2):
        self.alpha = float(alpha)
        self._ewma: dict[str, float] = {}

    def observe(self, deployment_id: str, seconds: float) -> float:
        prev = self._ewma.get(deployment_id)
        value = seconds if prev is None else self.alpha * seconds + (1 - self.alpha) * prev
        self._ewma[deployment_id] = value
        return value

    def get(self, deployment_id: str) -> float | None:
        return self._ewma.get(deployment_id)

    def forget(self, deployment_id: str) -> None:
        self._ewma.pop(deployment_id, None)


class InflightTracker:
    """Requests this replica currently has open against each deployment."""

    def __init__(self):
        self._n: dict[str, int] = {}

    def inc(self, deployment_id: str) -> int:
        self._n[deployment_id] = self._n.get(deployment_id, 0) + 1
        return self._n[deployment_id]

    def dec(self, deployment_id: str) -> int:
        n = max(self._n.get(deployment_id, 0) - 1, 0)
        if n:
            self._n[deployment_id] = n
        else:
            self._n.pop(deployment_id, None)
        return n

    def get(self, deployment_id: str) -> int:
        return self._n.get(deployment_id, 0)


@dataclass(frozen=True)
class Pressure:
    waiting: int
    running: int
    kv_cache_usage: float | None = None
    observed_at: float = 0.0


class PressureStore:
    """Queue pressure per deployment, published by the worker (``publish``) and read by gateways (``get_many``)
    with a short in-process cache so a burst of requests costs one Valkey round trip per second."""

    KEY = "dq:{}"

    def __init__(self, client: redis.Redis | None, cache_seconds: float = 1.0):
        self.client = client
        self.cache_seconds = float(cache_seconds)
        self._cache: dict[str, tuple[float, Pressure | None]] = {}

    async def publish(self, deployment_id: str, pressure: Pressure, ttl_seconds: float) -> None:
        if self.client is None:
            return
        payload = json.dumps(
            {"w": pressure.waiting, "r": pressure.running, "kv": pressure.kv_cache_usage, "t": pressure.observed_at}
        )
        try:
            await self.client.set(self.KEY.format(deployment_id), payload, ex=max(int(ttl_seconds), 1))
        except (redis.RedisError, OSError):
            pass

    async def get_many(self, deployment_ids: list[str]) -> dict[str, Pressure | None]:
        now = time.monotonic()
        out: dict[str, Pressure | None] = {}
        missing: list[str] = []
        for did in deployment_ids:
            hit = self._cache.get(did)
            if hit and hit[0] > now:
                out[did] = hit[1]
            else:
                missing.append(did)
        if missing and self.client is not None:
            try:
                raw = await self.client.mget([self.KEY.format(d) for d in missing])
            except (redis.RedisError, OSError):
                raw = [None] * len(missing)
            for did, value in zip(missing, raw, strict=True):
                p = _decode(value)
                self._cache[did] = (now + self.cache_seconds, p)
                out[did] = p
        for did in missing:
            out.setdefault(did, None)
        return out


def _decode(value) -> Pressure | None:
    if not value:
        return None
    try:
        d = json.loads(value)
        return Pressure(int(d["w"]), int(d["r"]), d.get("kv"), float(d.get("t") or 0))
    except (ValueError, KeyError, TypeError):
        return None


@dataclass
class RoutingSignals:
    latency: LatencyTracker
    inflight: InflightTracker
    pressure: PressureStore

    @classmethod
    def build(cls, client: redis.Redis | None, alpha: float = 0.2) -> RoutingSignals:
        return cls(LatencyTracker(alpha), InflightTracker(), PressureStore(client))

"""Load-test harness core (docs/spec/06 §"Performance"): concurrent chat / stream / embedding traffic with latency,
TTFB, throughput and error accounting, plus a baseline pass straight at the upstream so the gateway's *added*
latency (gateway TTFB minus upstream TTFB, per percentile) is reported in isolation.

Owned and dependency-free (httpx + asyncio); `scripts/loadtest.py` is the CLI, `tests/test_load.py` drives it
in-process against the mock upstream and checks the ledger invariants afterwards.
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx


@dataclass
class LoadConfig:
    model: str = "local-chat"
    requests: int = 200
    concurrency: int = 16
    duration_seconds: float | None = None  # when set, run until elapsed instead of a fixed count
    stream_ratio: float = 0.5
    embeddings_ratio: float = 0.0
    embeddings_model: str = "local-embed"
    prompt_words: int = 120  # payload size knob (~1.3 tokens per word)
    max_tokens: int = 64
    steer: str | None = None  # mock upstream `user` steering (e.g. "slow") to emulate a real model's latency
    seed: int = 1


@dataclass
class Sample:
    kind: str  # chat | stream | embeddings
    status: int
    ttfb_ms: float
    total_ms: float
    error: str | None = None
    cache: str | None = None
    chunks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    deployment: str | None = None


@dataclass
class Report:
    label: str
    elapsed_s: float
    samples: list[Sample] = field(default_factory=list)

    @property
    def ok(self) -> list[Sample]:
        return [s for s in self.samples if s.error is None]

    def percentile(self, values: list[float], p: float) -> float | None:
        if not values:
            return None
        values = sorted(values)
        k = min(len(values) - 1, max(0, round(p / 100 * (len(values) - 1))))
        return values[k]

    def summary(self) -> dict[str, Any]:
        ok = self.ok
        ttfb = [s.ttfb_ms for s in ok]
        total = [s.total_ms for s in ok]
        errors = Counter(s.error for s in self.samples if s.error)
        by_kind = Counter(s.kind for s in self.samples)
        return {
            "label": self.label,
            "requests": len(self.samples),
            "ok": len(ok),
            "errors": dict(errors),
            "error_rate": (len(self.samples) - len(ok)) / len(self.samples) if self.samples else 0.0,
            "rps": len(self.samples) / self.elapsed_s if self.elapsed_s else 0.0,
            "elapsed_s": round(self.elapsed_s, 3),
            "by_kind": dict(by_kind),
            "ttfb_ms": {
                "p50": self.percentile(ttfb, 50),
                "p95": self.percentile(ttfb, 95),
                "p99": self.percentile(ttfb, 99),
                "mean": statistics.fmean(ttfb) if ttfb else None,
            },
            "total_ms": {
                "p50": self.percentile(total, 50),
                "p95": self.percentile(total, 95),
                "p99": self.percentile(total, 99),
            },
            "tokens": {
                "prompt": sum(s.prompt_tokens for s in ok),
                "completion": sum(s.completion_tokens for s in ok),
            },
            "cache": dict(Counter(s.cache for s in ok if s.cache)),
            "deployments": dict(Counter(s.deployment for s in ok if s.deployment)),
        }


def _prompt(rng: random.Random, words: int) -> str:
    vocab = ["gateway", "budget", "tenant", "latency", "vllm", "token", "route", "cache", "policy", "audit"]
    return " ".join(rng.choice(vocab) for _ in range(words))


def _plan(cfg: LoadConfig, rng: random.Random) -> str:
    r = rng.random()
    if r < cfg.embeddings_ratio:
        return "embeddings"
    return "stream" if rng.random() < cfg.stream_ratio else "chat"


async def _one(
    client: httpx.AsyncClient, cfg: LoadConfig, kind: str, prompt: str, headers: dict, path_prefix: str, model: str
) -> Sample:
    t0 = time.perf_counter()
    ttfb = None
    try:
        if kind == "embeddings":
            body: dict[str, Any] = {"model": cfg.embeddings_model if model == cfg.model else model, "input": prompt}
            if cfg.steer:
                body["user"] = cfg.steer
            r = await client.post(f"{path_prefix}/embeddings", json=body, headers=headers)
            ttfb = (time.perf_counter() - t0) * 1000
            if r.status_code >= 400:
                return Sample(kind, r.status_code, ttfb, ttfb, error=_code(r))
            data = r.json()
            return Sample(
                kind,
                r.status_code,
                ttfb,
                (time.perf_counter() - t0) * 1000,
                cache=r.headers.get("x-aigw-cache"),
                prompt_tokens=(data.get("usage") or {}).get("prompt_tokens", 0),
                deployment=r.headers.get("x-aigw-deployment"),
            )
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": cfg.max_tokens}
        if cfg.steer:
            body["user"] = cfg.steer
        if kind == "stream":
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
            chunks = 0
            usage: dict = {}
            async with client.stream("POST", f"{path_prefix}/chat/completions", json=body, headers=headers) as r:
                if r.status_code >= 400:
                    text = (await r.aread()).decode("utf-8", "replace")
                    ttfb = (time.perf_counter() - t0) * 1000
                    return Sample(kind, r.status_code, ttfb, ttfb, error=_code_text(r.status_code, text))
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    if ttfb is None:
                        ttfb = (time.perf_counter() - t0) * 1000
                    if line == "data: [DONE]":
                        break
                    chunks += 1
                    try:
                        obj = json.loads(line[6:])
                    except ValueError:
                        continue
                    if obj.get("usage"):
                        usage = obj["usage"]
                    if obj.get("error"):
                        return Sample(
                            kind,
                            r.status_code,
                            ttfb or 0.0,
                            (time.perf_counter() - t0) * 1000,
                            error=f"stream:{obj['error'].get('code', 'error')}",
                        )
                return Sample(
                    kind,
                    r.status_code,
                    ttfb or 0.0,
                    (time.perf_counter() - t0) * 1000,
                    cache=r.headers.get("x-aigw-cache"),
                    chunks=chunks,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    deployment=r.headers.get("x-aigw-deployment"),
                )
        r = await client.post(f"{path_prefix}/chat/completions", json=body, headers=headers)
        ttfb = (time.perf_counter() - t0) * 1000
        if r.status_code >= 400:
            return Sample(kind, r.status_code, ttfb, ttfb, error=_code(r))
        data = r.json()
        u = data.get("usage") or {}
        return Sample(
            kind,
            r.status_code,
            ttfb,
            (time.perf_counter() - t0) * 1000,
            cache=r.headers.get("x-aigw-cache"),
            prompt_tokens=u.get("prompt_tokens", 0),
            completion_tokens=u.get("completion_tokens", 0),
            deployment=r.headers.get("x-aigw-deployment"),
        )
    except httpx.HTTPError as exc:
        now = (time.perf_counter() - t0) * 1000
        return Sample(kind, 0, ttfb or now, now, error=f"transport:{type(exc).__name__}")


def _code(r: httpx.Response) -> str:
    return _code_text(r.status_code, r.text)


def _code_text(status: int, text: str) -> str:
    try:
        return f"{status}:{json.loads(text)['error']['code']}"
    except (ValueError, KeyError, TypeError):
        return f"{status}"


async def run(
    client: httpx.AsyncClient,
    cfg: LoadConfig,
    *,
    headers: dict | None = None,
    path_prefix: str = "/v1",
    model: str | None = None,
    label: str = "gateway",
) -> Report:
    """Drive `cfg.concurrency` workers until `cfg.requests` (or `cfg.duration_seconds`) is reached."""
    rng = random.Random(cfg.seed)
    headers = headers or {}
    model = model or cfg.model
    samples: list[Sample] = []
    sem = asyncio.Semaphore(cfg.concurrency)
    started = time.perf_counter()
    deadline = started + cfg.duration_seconds if cfg.duration_seconds else None
    plans = [(_plan(cfg, rng), _prompt(rng, cfg.prompt_words)) for _ in range(cfg.requests if not deadline else 0)]

    async def worker(kind: str, prompt: str) -> None:
        async with sem:
            samples.append(await _one(client, cfg, kind, prompt, headers, path_prefix, model))

    if deadline:
        tasks: set[asyncio.Task] = set()
        while time.perf_counter() < deadline:
            while len(tasks) < cfg.concurrency and time.perf_counter() < deadline:
                t = asyncio.create_task(worker(_plan(cfg, rng), _prompt(rng, cfg.prompt_words)))
                tasks.add(t)
                t.add_done_callback(tasks.discard)
            await asyncio.sleep(0.005)
        if tasks:
            await asyncio.gather(*tasks)
    else:
        await asyncio.gather(*(worker(k, p) for k, p in plans))
    return Report(label, time.perf_counter() - started, samples)


def added_latency(gateway: Report, upstream: Report) -> dict[str, float | None]:
    """Gateway overhead per percentile: gateway TTFB minus upstream TTFB (docs/spec/06)."""
    g, u = gateway.summary()["ttfb_ms"], upstream.summary()["ttfb_ms"]
    return {p: (g[p] - u[p]) if g[p] is not None and u[p] is not None else None for p in ("p50", "p95", "p99", "mean")}


def check_thresholds(
    summary: dict[str, Any],
    *,
    max_error_rate: float | None = None,
    max_p95_ms: float | None = None,
    min_rps: float | None = None,
    added: dict | None = None,
    max_added_p95_ms: float | None = None,
) -> list[str]:
    """Return the list of violated thresholds (empty = pass); used as a CI gate."""
    bad: list[str] = []
    if max_error_rate is not None and summary["error_rate"] > max_error_rate:
        bad.append(f"error_rate {summary['error_rate']:.3f} > {max_error_rate}")
    if max_p95_ms is not None and (summary["ttfb_ms"]["p95"] or 0) > max_p95_ms:
        bad.append(f"ttfb p95 {summary['ttfb_ms']['p95']:.0f} ms > {max_p95_ms}")
    if min_rps is not None and summary["rps"] < min_rps:
        bad.append(f"rps {summary['rps']:.1f} < {min_rps}")
    if max_added_p95_ms is not None and added and added.get("p95") is not None and added["p95"] > max_added_p95_ms:
        bad.append(f"added p95 {added['p95']:.0f} ms > {max_added_p95_ms}")
    return bad


def to_json(report: Report) -> dict[str, Any]:
    return {"summary": report.summary(), "samples": [asdict(s) for s in report.samples]}

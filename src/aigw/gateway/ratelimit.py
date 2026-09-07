"""Valkey-backed RPM/TPM limiter and deployment cooldowns (docs/spec/04 §3, §6).

Atomic check-and-increment through one Lua script. Valkey is an accelerator only: on failure the
configured fail mode applies and a degraded metric is raised.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import redis.asyncio as redis

from aigw.core.errors import ErrorType, GatewayError

log = logging.getLogger(__name__)

# KEYS[1..n] = counter keys (pairs: rpm key, tpm key per scope)
# ARGV = limit_rpm_1, limit_tpm_1, ..., tokens, ttl
_LUA = """
local n = #KEYS / 2
local tokens = tonumber(ARGV[#ARGV - 1])
local ttl = tonumber(ARGV[#ARGV])
for i = 1, n do
  local rpm_limit = tonumber(ARGV[2*i - 1])
  local tpm_limit = tonumber(ARGV[2*i])
  local rpm = tonumber(redis.call('GET', KEYS[2*i - 1]) or '0')
  local tpm = tonumber(redis.call('GET', KEYS[2*i]) or '0')
  if rpm_limit > 0 and rpm + 1 > rpm_limit then return {0, 'rpm', i} end
  if tpm_limit > 0 and tpm + tokens > tpm_limit then return {0, 'tpm', i} end
end
for i = 1, n do
  redis.call('INCRBY', KEYS[2*i - 1], 1)
  redis.call('EXPIRE', KEYS[2*i - 1], ttl)
  redis.call('INCRBY', KEYS[2*i], tokens)
  redis.call('EXPIRE', KEYS[2*i], ttl)
end
return {1, '', 0}
"""


@dataclass
class LimitScope:
    name: str  # "key" | "project"
    id: str
    rpm: int | None
    tpm: int | None


class RateLimiter:
    def __init__(self, client: redis.Redis | None, fail_mode: str = "open"):
        self.client = client
        self.fail_mode = fail_mode
        self.degraded = False
        self._script = client.register_script(_LUA) if client else None

    async def check(self, scopes: list[LimitScope], tokens: int) -> None:
        scopes = [s for s in scopes if s.rpm or s.tpm]
        if not scopes:
            return
        if not self._script:
            return self._degraded()
        minute = int(time.time() // 60)
        keys: list[str] = []
        args: list[int] = []
        for s in scopes:
            keys += [f"rl:{s.name}:{s.id}:rpm:{minute}", f"rl:{s.name}:{s.id}:tpm:{minute}"]
            args += [s.rpm or 0, s.tpm or 0]
        args += [max(tokens, 0), 120]
        try:
            ok, which, idx = await self._script(keys=keys, args=args)
            self.degraded = False
        except (redis.RedisError, OSError) as exc:
            log.warning("rate limiter degraded: %s", exc)
            return self._degraded()
        if int(ok) == 0:
            scope = scopes[int(idx) - 1]
            which = which.decode() if isinstance(which, bytes) else which
            raise GatewayError(
                ErrorType.rate_limit,
                f"{which.upper()} limit exceeded for {scope.name}",
                code=f"{which}_limit_exceeded",
                headers={"Retry-After": str(60 - int(time.time() % 60))},
            )

    async def adjust_tokens(self, scopes: list[LimitScope], delta: int) -> None:
        """Correct the TPM counter after settlement (estimated → actual)."""
        if not self.client or delta == 0:
            return
        minute = int(time.time() // 60)
        try:
            async with self.client.pipeline(transaction=False) as p:
                for s in scopes:
                    if s.tpm:
                        p.incrby(f"rl:{s.name}:{s.id}:tpm:{minute}", delta)
                await p.execute()
        except (redis.RedisError, OSError):
            pass

    def _degraded(self) -> None:
        self.degraded = True
        if self.fail_mode == "closed":
            raise GatewayError(
                ErrorType.unavailable, "Rate limiter unavailable (fail-closed)", code="ratelimit_unavailable"
            )


class CooldownStore:
    """Deployment cooldown + consecutive failure counters; Valkey-backed with in-process mirror."""

    def __init__(self, client: redis.Redis | None):
        self.client = client
        self._local: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    async def is_cooling(self, deployment_id: str) -> bool:
        until = self._local.get(deployment_id)
        if until and until > time.time():
            return True
        if self.client:
            try:
                if await self.client.exists(f"cd:{deployment_id}"):
                    return True
            except (redis.RedisError, OSError):
                pass
        return False

    async def set(self, deployment_id: str, seconds: float) -> None:
        self._local[deployment_id] = time.time() + seconds
        if self.client:
            try:
                await self.client.set(f"cd:{deployment_id}", "1", ex=max(int(seconds), 1))
            except (redis.RedisError, OSError):
                pass

    def record_failure(self, deployment_id: str) -> int:
        self._failures[deployment_id] = self._failures.get(deployment_id, 0) + 1
        return self._failures[deployment_id]

    def record_success(self, deployment_id: str) -> None:
        self._failures.pop(deployment_id, None)

"""Active health checks (docs/spec/04 §6): a worker job that probes every active deployment and turns the result
into gateway routing state.

Each probe is a cheap authenticated ``GET …/models`` against the deployment's base URL (Anthropic: ``/v1/models``).
Only transport failures, timeouts and 5xx count as failures: a 4xx means the upstream is up (some servers do not
implement ``/models`` at all). Results are stored in ``deployment_health`` (observability, portal) and, after
``AIGW_HEALTH_FAILURE_THRESHOLD`` consecutive failures, the deployment is cooled down through the same Valkey key
the passive path uses, so the router skips it before any client request hits it. Recovery clears the cooldown at
once. PostgreSQL stays the authority; Valkey only carries the signal to the gateways.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from aigw.adapters.anthropic import ANTHROPIC_VERSION
from aigw.adapters.registry import AdapterRegistry
from aigw.core.errors import GatewayError
from aigw.core.secrets import SecretResolver
from aigw.db.models import Deployment, DeploymentHealth
from aigw.gateway.ratelimit import CooldownStore
from aigw.gateway.signals import Pressure, PressureStore

log = logging.getLogger("aigw.worker.health")

HEALTHY, DEGRADED, UNHEALTHY = "healthy", "degraded", "unhealthy"


class HealthChecker:
    def __init__(
        self,
        db,
        settings,
        adapters: AdapterRegistry,
        secrets: SecretResolver,
        cooldowns: CooldownStore,
        http: httpx.AsyncClient,
        concurrency: int = 10,
    ):
        self.db = db
        self.settings = settings
        self.adapters = adapters
        self.secrets = secrets
        self.cooldowns = cooldowns
        self.http = http
        self.timeout = float(settings.health_check_timeout_seconds)
        self.threshold = int(settings.health_failure_threshold)
        self.cooldown_seconds = float(settings.health_cooldown_seconds)
        self.pressure = PressureStore(cooldowns.client)
        self.pressure_ttl = max(3 * float(settings.health_check_interval_seconds), 30.0)
        self._sem = asyncio.Semaphore(concurrency)

    # ---- probing --------------------------------------------------------

    def probe_request(self, d: Deployment) -> tuple[str, dict[str, str]]:
        adapter = self.adapters.get(d.provider)
        credential = self.secrets.resolve(d.credential_ref)
        if hasattr(adapter, "probe_request"):  # provider-specific target (Azure: /openai/models?api-version=…)
            return adapter.probe_request(d, credential)
        base = (d.base_url or adapter.default_base_url).rstrip("/")
        headers: dict[str, str] = {k: str(v) for k, v in (d.extra_headers or {}).items()}
        if d.provider == "anthropic":
            headers["anthropic-version"] = ANTHROPIC_VERSION
            if credential:
                headers["x-api-key"] = credential
            return f"{base}/v1/models", headers
        if credential:
            headers["authorization"] = f"Bearer {credential}"
        return f"{base}/models", headers

    async def probe(self, d: Deployment) -> tuple[bool, int | None, str | None]:
        """(ok, latency_ms, error). Transport errors, timeouts and 5xx are failures; any other answer means up."""
        try:
            url, headers = self.probe_request(d)
        except GatewayError as exc:
            return False, None, exc.code
        started = time.perf_counter()
        try:
            async with self._sem:
                r = await self.http.get(url, headers=headers, timeout=self.timeout)
        except httpx.TimeoutException:
            return False, None, "timeout"
        except httpx.HTTPError as exc:
            return False, None, f"transport: {type(exc).__name__}"
        latency = int((time.perf_counter() - started) * 1000)
        if r.status_code >= 500:
            return False, latency, f"http_{r.status_code}"
        return True, latency, None

    # ---- queue pressure (vLLM /metrics, docs/spec/04 §5.1) ---------------

    @staticmethod
    def metrics_url(d: Deployment) -> str | None:
        caps = d.capabilities or {}
        if caps.get("metrics_url"):
            return str(caps["metrics_url"])
        if caps.get("engine") == "vllm" and d.base_url:
            base = d.base_url.rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            return f"{base}/metrics"
        return None

    async def scrape(self, d: Deployment) -> Pressure | None:
        url = self.metrics_url(d)
        if not url:
            return None
        try:
            async with self._sem:
                r = await self.http.get(url, timeout=self.timeout)
            if r.status_code != 200:
                return None
            m = parse_vllm_metrics(r.text)
        except (httpx.HTTPError, ValueError):
            return None
        if m is None:
            return None
        return Pressure(m["waiting"], m["running"], m.get("kv_cache_usage"), time.time())

    # ---- one sweep ------------------------------------------------------

    async def run_once(self) -> dict[str, str]:
        """Probe every active deployment; return {deployment_id: status}."""
        async with self.db.session() as s:
            deployments = list((await s.execute(select(Deployment).where(Deployment.status == "active"))).scalars())
        results = await asyncio.gather(*(self.probe(d) for d in deployments))
        pressures = await asyncio.gather(*(self.scrape(d) for d in deployments))
        statuses: dict[str, str] = {}
        now = datetime.now(UTC)
        async with self.db.tx() as s:
            for d, (ok, latency, error), pressure in zip(deployments, results, pressures, strict=True):
                row = await s.get(DeploymentHealth, d.id)
                if row is None:
                    row = DeploymentHealth(deployment_id=d.id, consecutive_failures=0, status="unknown")
                    s.add(row)
                previous = row.status
                if ok:
                    row.consecutive_failures = 0
                    row.status = HEALTHY
                else:
                    row.consecutive_failures += 1
                    row.status = UNHEALTHY if row.consecutive_failures >= self.threshold else DEGRADED
                row.checked_at, row.latency_ms, row.error = now, latency, error
                if pressure is not None:
                    row.queue_waiting, row.queue_running = pressure.waiting, pressure.running
                    row.kv_cache_usage, row.metrics_at = pressure.kv_cache_usage, now
                    await self.pressure.publish(str(d.id), pressure, self.pressure_ttl)
                elif self.metrics_url(d) is not None:
                    row.queue_waiting = row.queue_running = row.kv_cache_usage = None  # scrape failed: unknown
                statuses[str(d.id)] = row.status
                await self._apply(d, row, previous)
        return statuses

    async def _apply(self, d: Deployment, row: DeploymentHealth, previous: str) -> None:
        did = str(d.id)
        if row.status == UNHEALTHY:
            await self.cooldowns.set(did, self.cooldown_seconds)  # refreshed every sweep while it stays down
            if previous != UNHEALTHY:
                log.warning(
                    "deployment %s (%s) unhealthy after %d failures: %s",
                    d.name,
                    did,
                    row.consecutive_failures,
                    row.error,
                )
        elif row.status == HEALTHY and previous == UNHEALTHY:
            await self.cooldowns.clear(did)
            log.info("deployment %s (%s) recovered (%s ms)", d.name, did, row.latency_ms)


_SAMPLE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eE]+|NaN|[+-]Inf)\s*$")


def parse_vllm_metrics(text: str) -> dict | None:
    """Sum vLLM queue gauges over their label sets (one deployment may serve several model names).

    Returns None when the text carries no vLLM queue metric at all (not a vLLM server)."""
    waiting = running = None
    kv: float | None = None
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE.match(line.strip())
        if not m:
            continue
        name, value = m.group(1), m.group(3)
        try:
            v = float(value)
        except ValueError:
            continue
        if name == "vllm:num_requests_waiting":
            waiting = (waiting or 0) + v
        elif name == "vllm:num_requests_running":
            running = (running or 0) + v
        elif name in ("vllm:gpu_cache_usage_perc", "vllm:kv_cache_usage_perc"):
            kv = max(kv or 0.0, v)
    if waiting is None and running is None:
        return None
    return {"waiting": int(waiting or 0), "running": int(running or 0), "kv_cache_usage": kv}

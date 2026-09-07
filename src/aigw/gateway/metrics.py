"""Prometheus metrics (docs/spec/04 §8)."""

from __future__ import annotations

from decimal import Decimal

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "aigw_requests_total", "Requests by model/provider/status", ["model", "provider", "status", "endpoint"]
)
LATENCY = Histogram(
    "aigw_request_latency_seconds",
    "End-to-end latency",
    ["model", "provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
TTFB = Histogram(
    "aigw_ttfb_seconds",
    "Time to first byte from upstream",
    ["model", "provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
TOKENS = Counter("aigw_tokens_total", "Tokens by direction", ["model", "provider", "direction"])
COST = Counter("aigw_cost_total", "Settled cost in ledger currency", ["model", "provider", "org_id"])
BUDGET_REJECTIONS = Counter("aigw_budget_rejections_total", "Requests rejected by budget", ["scope_type"])
RATELIMIT_REJECTIONS = Counter("aigw_ratelimit_rejections_total", "Requests rejected by rate limit", ["kind"])
COOLDOWNS = Counter("aigw_deployment_cooldowns_total", "Deployment cooldowns", ["deployment", "reason"])
RATELIMIT_DEGRADED = Gauge("aigw_ratelimit_degraded", "1 when Valkey rate limiting is unavailable")
CONFIG_VERSION = Gauge("aigw_config_version", "Loaded configuration version")


def observe(ctx, deployment, status: str, usage, cost: Decimal, latency_ms: int, ttfb: float | None) -> None:
    labels = dict(model=ctx.model.name, provider=deployment.provider)
    REQUESTS.labels(status=status, endpoint=ctx.endpoint, **labels).inc()
    LATENCY.labels(**labels).observe(latency_ms / 1000)
    if ttfb is not None:
        TTFB.labels(**labels).observe(ttfb)
    if usage:
        TOKENS.labels(direction="prompt", **labels).inc(usage.prompt_tokens)
        TOKENS.labels(direction="completion", **labels).inc(usage.completion_tokens)
    if cost:
        COST.labels(org_id=ctx.scope.org_id, **labels).inc(float(cost))

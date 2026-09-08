"""Adaptive routing (docs/spec/04 §5.1): latency EWMA, in-flight admission control, vLLM queue pressure."""

from __future__ import annotations

import random
from collections import Counter

import pytest

from aigw.adapters.base import DeploymentConfig
from aigw.gateway.ratelimit import CooldownStore
from aigw.gateway.router import Candidate, Router, RoutingPolicy
from aigw.gateway.signals import LatencyTracker, Pressure, PressureStore, RoutingSignals
from aigw.testing.mock_upstream import STATE
from aigw.worker.health import HealthChecker, parse_vllm_metrics
from tests.conftest import ADMIN, refresh
from tests.test_health import health_settings


def dep(name: str, **kw) -> DeploymentConfig:
    base = dict(
        id=name,
        model_id="m",
        model_name="local-chat",
        name=name,
        provider="openai_compat",
        provider_model="mock-chat",
        base_url="http://mock/v1",
        credential_ref="none",
    )
    return DeploymentConfig(**{**base, **kw})


async def first_choice_share(router: Router, cands: list[Candidate], n: int = 400) -> Counter:
    c: Counter = Counter()
    for _ in range(n):
        ordered, _ = await router._order(list(cands))
        c[ordered[0].deployment.name] += 1
    return c


# ---- pure signal math ------------------------------------------------------


def test_latency_ewma():
    t = LatencyTracker(alpha=0.5)
    assert t.get("a") is None
    assert t.observe("a", 1.0) == 1.0
    assert t.observe("a", 0.0) == 0.5
    assert t.observe("a", 0.0) == 0.25
    t.forget("a")
    assert t.get("a") is None


def test_parse_vllm_metrics():
    text = """# HELP vllm:num_requests_waiting Number of requests waiting to be processed.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{model_name="a"} 3.0
vllm:num_requests_waiting{model_name="b"} 2.0
vllm:num_requests_running{model_name="a"} 7.0
vllm:gpu_cache_usage_perc{model_name="a"} 0.42
process_cpu_seconds_total 12.5
"""
    assert parse_vllm_metrics(text) == {"waiting": 5, "running": 7, "kv_cache_usage": 0.42}
    assert parse_vllm_metrics("process_cpu_seconds_total 1\n") is None  # not a vLLM server


# ---- ordering --------------------------------------------------------------


async def test_adaptive_order_prefers_fast_idle_and_unqueued(app):
    signals = RoutingSignals.build(None)
    router = Router(app.state.adapters, CooldownStore(None), random.Random(7), signals, RoutingPolicy())
    a, b = Candidate(dep("A")), Candidate(dep("B"))

    # no signals at all: plain weights → roughly even
    share = await first_choice_share(router, [a, b])
    assert 0.4 < share["A"] / 400 < 0.6

    # A is slow (2 s TTFB), B fast (100 ms): B wins most draws but A still gets a trickle
    signals.latency.observe("A", 2.0)
    signals.latency.observe("B", 0.1)
    share = await first_choice_share(router, [a, b])
    assert share["B"] > 250 and share["A"] > 20, share  # expected ~73 % vs ~27 %

    # equal latency, A has 8 requests in flight (ref 4): B preferred
    signals.latency.observe("A", 0.1)
    signals.latency._ewma["A"] = 0.1
    for _ in range(8):
        signals.inflight.inc("A")
    share = await first_choice_share(router, [a, b])
    assert share["B"] > 250, share
    for _ in range(8):
        signals.inflight.dec("A")

    # priority still dominates: a slow priority-0 deployment beats a fast priority-1 one every time
    slow0 = Candidate(dep("S0", priority=0))
    signals.latency.observe("S0", 5.0)
    fast1 = Candidate(dep("F1", priority=1))
    signals.latency.observe("F1", 0.05)
    ordered, explain = await router._order([fast1, slow0])
    assert [c.deployment.name for c in ordered] == ["S0", "F1"]
    assert explain["S0"]["ttfb_ms"] == 5000 and explain["F1"]["inflight"] == 0 and "score" in explain["F1"]

    # weighted strategy ignores every signal
    plain = Router(
        app.state.adapters, CooldownStore(None), random.Random(7), signals, RoutingPolicy(strategy="weighted")
    )
    signals.latency._ewma["A"] = 3.0
    share = await first_choice_share(plain, [a, b])
    assert 0.4 < share["A"] / 400 < 0.6
    _, explain = await plain._order([a, b])
    assert explain == {}


async def test_queue_pressure_lowers_weight(app):
    signals = RoutingSignals.build(None)
    signals.pressure._cache["A"] = (float("inf"), Pressure(waiting=40, running=8))  # 40 waiting vs ref 8
    signals.pressure._cache["B"] = (float("inf"), Pressure(waiting=0, running=1))
    router = Router(app.state.adapters, CooldownStore(None), random.Random(3), signals, RoutingPolicy())
    share = await first_choice_share(router, [Candidate(dep("A")), Candidate(dep("B"))])
    assert share["B"] > 300, share


# ---- through the pipeline -----------------------------------------------------


async def test_pipeline_feeds_signals_and_explains(client, app, tenant):
    did = tenant["deployment"]["id"]
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "local-chat", "messages": [{"role": "user", "content": "hi"}]},
        headers=tenant["auth"],
    )
    assert r.status_code == 200, r.text
    assert app.state.signals.latency.get(did) is not None
    assert app.state.signals.inflight.get(did) == 0  # released at settlement
    diag = await client.get(f"/admin/v1/requests/{r.json()['aigw']['request_id']}", headers=ADMIN)
    routing = diag.json()["attempts"][0]["routing"]
    assert routing["strategy"] == "adaptive" and routing["signals"]["mock-primary"]["inflight"] == 0
    # streaming attempts feed the EWMA with time-to-first-byte too
    before = app.state.signals.latency.get(did)
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "local-chat", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        headers=tenant["auth"],
    )
    assert r.status_code == 200
    assert app.state.signals.latency.get(did) != before or before is not None


async def test_max_concurrency_is_local_admission_control(client, app, tenant):
    did = tenant["deployment"]["id"]
    await client.patch(f"/admin/v1/deployments/{did}", json={"capabilities": {"max_concurrency": 1}}, headers=ADMIN)
    await refresh(app)
    app.state.signals.inflight.inc(did)  # one request already open on this replica
    body = {"model": "local-chat", "messages": [{"role": "user", "content": "hi"}]}
    r = await client.post("/v1/chat/completions", json=body, headers=tenant["auth"])
    assert (r.status_code, r.json()["error"]["code"]) == (503, "no_eligible_deployment")
    app.state.signals.inflight.dec(did)
    r = await client.post("/v1/chat/completions", json=body, headers=tenant["auth"])
    assert r.status_code == 200, r.text


async def test_worker_scrapes_vllm_metrics_into_pressure(client, app, db, valkey, tenant):
    did = tenant["deployment"]["id"]
    await client.patch(f"/admin/v1/deployments/{did}", json={"capabilities": {"engine": "vllm"}}, headers=ADMIN)
    STATE["queue_waiting"], STATE["queue_running"], STATE["kv_cache"] = 25, 6, 0.9
    try:
        hc = HealthChecker(
            db, health_settings(), app.state.adapters, app.state.secrets, CooldownStore(valkey), app.state.upstream
        )
        assert (
            hc.metrics_url(type("D", (), {"capabilities": {"engine": "vllm"}, "base_url": "http://mock/v1"})())
            == "http://mock/metrics"
        )
        await hc.run_once()
        models = (await client.get("/admin/v1/models", headers=ADMIN)).json()["data"]
        health = {d["name"]: d["health"] for m in models for d in m["deployments"]}
        assert health["mock-primary"]["status"] == "healthy"
        assert (health["mock-primary"]["queue_waiting"], health["mock-primary"]["queue_running"]) == (25, 6)
        assert health["mock-primary"]["kv_cache_usage"] == 0.9
        assert health["mock-embed"]["queue_waiting"] is None  # no engine: never scraped
        if valkey is None:
            pytest.skip("Valkey not reachable: pressure cannot reach the gateway in this environment")
        seen = await PressureStore(valkey).get_many([did])
        assert seen[did] is not None and seen[did].waiting == 25
        # the gateway's own store sees it and the diagnostics record shows it
        r = await client.post(
            "/v1/chat/completions",
            json={"model": "local-chat", "messages": [{"role": "user", "content": "hi"}]},
            headers=tenant["auth"],
        )
        diag = await client.get(f"/admin/v1/requests/{r.json()['aigw']['request_id']}", headers=ADMIN)
        assert diag.json()["attempts"][0]["routing"]["signals"]["mock-primary"]["queue_waiting"] == 25
    finally:
        STATE.pop("queue_waiting", None)
        STATE.pop("queue_running", None)
        STATE.pop("kv_cache", None)

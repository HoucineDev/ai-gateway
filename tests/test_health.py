"""Active health checks (docs/spec/04 §6): probe → deployment_health → Valkey cooldown → router skips it."""

from __future__ import annotations

import pytest

from aigw.config import Settings
from aigw.gateway.ratelimit import CooldownStore
from aigw.worker.health import HealthChecker
from aigw.worker.main import Worker
from tests.conftest import ADMIN, TEST_DB, TEST_VALKEY, refresh


def health_settings(**kw) -> Settings:
    return Settings(
        database_url=TEST_DB,
        valkey_url=TEST_VALKEY,
        admin_key="test-admin",
        health_failure_threshold=2,
        health_cooldown_seconds=60,
        health_check_timeout_seconds=2,
        **kw,
    )


def checker(app, db, valkey, **kw) -> HealthChecker:
    # a separate CooldownStore, like the real worker process: the gateway must learn about it through Valkey
    return HealthChecker(
        db, health_settings(**kw), app.state.adapters, app.state.secrets, CooldownStore(valkey), app.state.upstream
    )


async def _add(client, model_id, name, base_url, **kw):
    r = await client.post(
        f"/admin/v1/models/{model_id}/deployments",
        json={"name": name, "provider": "openai_compat", "provider_model": "mock-chat", "base_url": base_url, **kw},
        headers=ADMIN,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _health(client) -> dict[str, dict | None]:
    r = await client.get("/admin/v1/models", headers=ADMIN)
    assert r.status_code == 200
    return {d["name"]: d["health"] for m in r.json()["data"] for d in m["deployments"]}


async def test_probe_classification(client, app, db, valkey, tenant):
    mid = tenant["model"]["id"]
    await _add(client, mid, "bad-500", "http://mock/always500/v1")
    await _add(client, mid, "no-models-route", "http://mock/nope/v1")  # 404: the server is up
    await _add(client, mid, "bad-credential", "http://mock/v1", credential_ref="env:DOES_NOT_EXIST")
    hc = checker(app, db, valkey)
    statuses = await hc.run_once()
    health = await _health(client)
    assert health["mock-primary"]["status"] == "healthy" and health["mock-primary"]["latency_ms"] is not None
    assert health["no-models-route"]["status"] == "healthy"
    assert health["bad-500"] == {
        **health["bad-500"],
        "status": "degraded",
        "consecutive_failures": 1,
        "error": "http_500",
    }
    assert (
        health["bad-credential"]["status"] == "degraded" and health["bad-credential"]["error"] == "credential_missing"
    )
    assert set(statuses.values()) == {"healthy", "degraded"}
    # anthropic probe shape
    url, headers = hc.probe_request(
        type(
            "D",
            (),
            {"provider": "anthropic", "base_url": None, "credential_ref": "env:ANTHROPIC_KEY", "extra_headers": {}},
        )()
    )
    assert url == "https://api.anthropic.com/v1/models" and headers["x-api-key"] == "test-anthropic-key"
    assert "anthropic-version" in headers


async def test_unhealthy_deployment_is_cooled_down_and_recovers(client, app, db, valkey, tenant):
    """Preferred deployment goes down → after the threshold the router skips it → recovery lifts the cooldown."""
    mid = tenant["model"]["id"]
    good = tenant["deployment"]
    await client.patch(f"/admin/v1/deployments/{good['id']}", json={"priority": 1}, headers=ADMIN)
    bad = await _add(client, mid, "bad-primary", "http://mock/always500/v1", priority=0)
    await refresh(app)
    hc = checker(app, db, valkey)

    assert (await hc.run_once())[bad["id"]] == "degraded"  # 1 failure < threshold: still routable
    assert not await app.state.cooldowns.is_cooling(bad["id"])
    assert (await hc.run_once())[bad["id"]] == "unhealthy"
    if valkey is None:
        pytest.skip("Valkey not reachable: cooldown signal cannot reach the gateway in this environment")
    assert await app.state.cooldowns.is_cooling(bad["id"])  # seen through Valkey by the gateway's own store

    r = await client.post(
        "/v1/chat/completions",
        json={"model": "local-chat", "messages": [{"role": "user", "content": "hi"}]},
        headers=tenant["auth"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["aigw"]["deployment"] == "mock-primary"  # bad-primary skipped without a failed attempt
    diag = await client.get(f"/admin/v1/requests/{r.json()['aigw']['request_id']}", headers=ADMIN)
    assert diag.json()["attempts"][0]["routing"]["rejected"] == {"bad-primary": "cooldown"}

    # upstream fixed: next sweep marks it healthy and lifts the cooldown immediately
    await client.patch(f"/admin/v1/deployments/{bad['id']}", json={"base_url": "http://mock/v1"}, headers=ADMIN)
    assert (await hc.run_once())[bad["id"]] == "healthy"
    assert not await app.state.cooldowns.is_cooling(bad["id"])
    assert (await _health(client))["bad-primary"]["consecutive_failures"] == 0


async def test_worker_runs_sweeps_on_interval(client, app, db, valkey, tenant):
    hc = checker(app, db, valkey)
    w = Worker(db, health_settings(health_check_interval_seconds=1000), hc)
    assert (await w.tick())["health_probed"] == 2  # first tick probes every active deployment
    assert (await w.tick())["health_probed"] == 0  # inside the interval: no probe
    w._health_last = float("-inf")
    assert (await w.tick())["health_probed"] == 2
    off = Worker(db, health_settings(health_check_interval_seconds=0), hc)
    assert (await off.tick())["health_probed"] == 0
    assert (await Worker(db, health_settings(), None).tick())["health_probed"] == 0

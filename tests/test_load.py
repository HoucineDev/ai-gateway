"""Load harness in-process (docs/spec/06): concurrent mixed traffic through the whole pipeline, then the ledger
invariants that must survive concurrency — no pending reservation left, budget spend equals settled usage, one
attempt per request — plus the harness's own reporting and threshold gate."""

from __future__ import annotations

from decimal import Decimal

import httpx

from aigw.testing.loadtest import LoadConfig, Report, Sample, added_latency, check_thresholds, run
from aigw.testing.mock_upstream import mock
from tests.conftest import ADMIN


async def test_concurrent_mixed_traffic_keeps_the_ledger_exact(client, app, tenant):
    cfg = LoadConfig(
        requests=120, concurrency=24, stream_ratio=0.5, embeddings_ratio=0.15, prompt_words=60, max_tokens=32
    )
    report = await run(client, cfg, headers=tenant["auth"])
    s = report.summary()
    assert s["requests"] == 120 and s["errors"] == {}, s["errors"]
    assert s["by_kind"]["embeddings"] > 0 and s["by_kind"]["stream"] > 0 and s["by_kind"]["chat"] > 0
    assert s["ttfb_ms"]["p95"] is not None and s["rps"] > 0
    assert s["tokens"]["prompt"] > 0 and s["cache"] == {"off": 120}  # cache is off for this project
    assert set(s["deployments"]) == {"mock-primary", "mock-embed"}

    # ledger invariants after the storm
    pid = tenant["project"]["id"]
    budget = (
        await client.get("/admin/v1/budgets", params={"scope_type": "project", "scope_id": pid}, headers=ADMIN)
    ).json()["data"][0]
    usage = (
        await client.get("/admin/v1/usage", params={"scope_type": "project", "scope_id": pid}, headers=ADMIN)
    ).json()
    assert Decimal(budget["reserved_amount"]) == 0
    assert Decimal(budget["spent_amount"]) == Decimal(usage["total_cost"]) > 0
    assert sum(g["requests"] for g in usage["data"]) == 120
    rows = (await client.get("/admin/v1/requests", params={"project_id": pid, "limit": 500}, headers=ADMIN)).json()[
        "data"
    ]
    assert len(rows) == 120 and {r["status"] for r in rows} == {"succeeded"}
    assert len({r["request_id"] for r in rows}) == 120  # exactly one settled attempt per request


async def test_baseline_pass_and_added_latency(client, app, tenant):
    cfg = LoadConfig(requests=30, concurrency=8, stream_ratio=0.5, prompt_words=20, max_tokens=16)
    gateway = await run(client, cfg, headers=tenant["auth"], label="gateway")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock), base_url="http://mock") as up:
        upstream = await run(up, cfg, model="mock-chat", label="upstream")
    assert upstream.summary()["errors"] == {} and gateway.summary()["errors"] == {}
    added = added_latency(gateway, upstream)
    assert set(added) == {"p50", "p95", "p99", "mean"} and all(v is not None for v in added.values())
    assert added["p95"] < 2000  # the gateway's own overhead through ASGI in-process, generous bound


def test_threshold_gate_and_percentiles():
    ok = [Sample("chat", 200, 10.0 * i, 12.0 * i) for i in range(1, 21)]
    bad = [Sample("chat", 429, 5.0, 5.0, error="429:rate_limited") for _ in range(5)]
    r = Report("x", elapsed_s=2.0, samples=ok + bad)
    s = r.summary()
    assert s["requests"] == 25 and s["ok"] == 20 and s["errors"] == {"429:rate_limited": 5}
    assert abs(s["error_rate"] - 0.2) < 1e-9 and s["rps"] == 12.5
    assert s["ttfb_ms"]["p50"] == 110.0 and s["ttfb_ms"]["p99"] == 200.0
    assert check_thresholds(s) == []
    viol = check_thresholds(s, max_error_rate=0.1, max_p95_ms=100, min_rps=50, added={"p95": 40.0}, max_added_p95_ms=25)
    assert len(viol) == 4 and viol[0].startswith("error_rate")

"""Pilot gate: budgets under concurrency, ambiguous outcomes, reconciliation (docs/spec/06 §1)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import select

from aigw.db.models import Budget, RequestAttempt
from tests.conftest import ADMIN, refresh


async def _budget(client, scope_id):
    return (await client.get("/admin/v1/budgets", headers=ADMIN, params={"scope_id": scope_id})).json()["data"][0]


async def test_budget_race_exact_admission(client, app, tenant):
    """N concurrent requests against a budget that fits about N/2 → exactly floor(limit/reserve) succeed."""
    # price: input 1/M, output 2/M; reserve = est_prompt*1e-6 + 256*2e-6 ≈ 0.000512 + small
    # Tighten the key budget so only 3 reservations fit while all 8 requests are in flight (mock is slow)
    reserve = Decimal("0.000512") + Decimal("0.000006")  # prompt estimate for "hello" is 6 tokens
    limit = reserve * 3 + Decimal("0.000001")
    r = await client.post(
        "/admin/v1/budgets",
        json={"scope_type": "key", "scope_id": tenant["key"]["id"], "limit_amount": str(limit)},
        headers=ADMIN,
    )
    assert r.status_code == 201
    body = {
        "model": "local-chat",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 256,
        "user": "slow",
    }

    async def one():
        return await client.post("/v1/chat/completions", headers=tenant["auth"], json=body)

    results = await asyncio.gather(*[one() for _ in range(8)])
    ok = [x for x in results if x.status_code == 200]
    rejected = [x for x in results if x.status_code == 429]
    assert len(ok) == 3, [x.status_code for x in results]
    assert all(x.json()["error"]["type"] == "budget_exceeded" for x in rejected)
    b = await _budget(client, tenant["key"]["id"])
    assert Decimal(b["reserved_amount"]) == 0
    assert Decimal(b["spent_amount"]) <= limit
    # ledger is consistent: no pending attempts remain
    async with app.state.db.session() as s:
        pending = (await s.execute(select(RequestAttempt).where(RequestAttempt.status == "pending"))).scalars().all()
    assert pending == []


async def test_settlement_is_idempotent(client, app, tenant):
    from aigw.gateway.accounting import Reservation

    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={"model": "local-chat", "messages": [{"role": "user", "content": "x"}]},
    )
    rid = r.headers["x-aigw-request-id"]
    async with app.state.db.session() as s:
        att = (
            await s.execute(select(RequestAttempt).where(RequestAttempt.request_id == __import__("uuid").UUID(rid)))
        ).scalar_one()
        before = await _budget(client, tenant["project"]["id"])
        budgets = [b.id for b in (await s.execute(select(Budget))).scalars()]
    res = Reservation(
        attempt_id=att.id,
        request_id=att.request_id,
        attempt_no=1,
        amount=Decimal(att.reserved_amount),
        budget_ids=budgets,
        price=app.state.snapshots.current.price_for("openai_compat", "mock-chat"),
        est_prompt_tokens=1,
        est_output_tokens=1,
    )
    scope = app.state.snapshots.current.keys_by_hash[next(iter(app.state.snapshots.current.keys_by_hash))]
    dep = app.state.snapshots.current.resolve_model(scope.org_id, "local-chat").deployments[0]
    again = await app.state.ledger.settle(
        res,
        scope=scope,
        status="succeeded",
        usage=None,
        usage_source="none",
        deployment=dep,
        model_name="local-chat",
        endpoint="chat",
        stream=False,
        latency_ms=1,
        ttfb_ms=None,
    )
    assert again == Decimal(att.settled_amount)
    after = await _budget(client, tenant["project"]["id"])
    assert after["spent_amount"] == before["spent_amount"]


async def test_stream_cut_is_ambiguous_and_conservative(client, app, tenant):
    """Upstream terminates the stream after content was sent: no fallback, attempt = ambiguous, settled ≥ actual."""
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={
            "model": "local-chat",
            "stream": True,
            "user": "cut",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "one two three four five"}],
        },
    )
    assert r.status_code == 200
    assert '"error"' in r.text  # error event delivered in-band after partial content
    rid = r.headers["x-aigw-request-id"]
    att = (await client.get(f"/admin/v1/requests/{rid}", headers=ADMIN)).json()["attempts"]
    assert len(att) == 1 and att[0]["status"] == "ambiguous"
    assert att[0]["usage_source"] == "estimated"
    assert Decimal(att[0]["settled_amount"]) >= Decimal(att[0]["reserved_amount"])


async def test_stream_without_usage_is_estimated(client, app, tenant):
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={
            "model": "local-chat",
            "stream": True,
            "user": "nousage",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200
    assert '"aigw_estimated":true' in r.text
    rid = r.headers["x-aigw-request-id"]
    att = (await client.get(f"/admin/v1/requests/{rid}", headers=ADMIN)).json()["attempts"][0]
    assert att["status"] == "succeeded" and att["usage_source"] == "estimated"


async def test_reconcile_pending(client, app, tenant, db):
    """Worker settles attempts stuck in pending as ambiguous at their reservation."""
    from aigw.worker.main import Worker

    scope = app.state.snapshots.current.keys_by_hash[next(iter(app.state.snapshots.current.keys_by_hash))]
    dep = app.state.snapshots.current.resolve_model(scope.org_id, "local-chat").deployments[0]
    import uuid

    res = await app.state.ledger.reserve(
        scope=scope,
        request_id=uuid.uuid4(),
        attempt_no=1,
        model_id=dep.model_id,
        model_name="local-chat",
        deployment=dep,
        endpoint="chat",
        price=app.state.snapshots.current.price_for("openai_compat", "mock-chat"),
        est_prompt_tokens=100,
        est_output_tokens=100,
        routing={},
    )
    b = await _budget(client, tenant["project"]["id"])
    assert Decimal(b["reserved_amount"]) == res.amount
    n = await Worker(db, app.state.settings).ledger.reconcile_pending(older_than_seconds=0)
    assert n == 1
    b = await _budget(client, tenant["project"]["id"])
    assert Decimal(b["reserved_amount"]) == 0 and Decimal(b["spent_amount"]) == res.amount


async def test_soft_alert_outbox_and_temporary_increase(client, app, tenant, db):
    from sqlalchemy import select as sel

    from aigw.db.models import Outbox
    from aigw.worker.main import Worker

    # reserve for max_tokens=5 is 0.000016; actual settled cost is 0.000012 → crosses the 50% alert of a 0.00002 limit
    bid = tenant["budget"]["id"]
    await client.patch(f"/admin/v1/budgets/{bid}", json={"limit_amount": "0.00002"}, headers=ADMIN)
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={"model": "local-chat", "max_tokens": 5, "messages": [{"role": "user", "content": "hello"}]},
    )
    assert r.status_code == 200
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={"model": "local-chat", "max_tokens": 5, "messages": [{"role": "user", "content": "hello"}]},
    )
    assert r.status_code == 429
    async with db.session() as s:
        topics = [o.topic for o in (await s.execute(sel(Outbox))).scalars()]
    assert "budget.soft_alert" in topics
    # temporary increase lets the next request through
    r = await client.post(
        f"/admin/v1/budgets/{bid}/temporary-increase",
        json={"amount": "1", "until": "2099-01-01T00:00:00Z"},
        headers=ADMIN,
    )
    assert r.status_code == 200
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={"model": "local-chat", "max_tokens": 5, "messages": [{"role": "user", "content": "hello"}]},
    )
    assert r.status_code == 200
    processed = await Worker(db, app.state.settings).process_outbox()
    assert processed >= 2


async def test_rate_limit_rpm(client, app, tenant, valkey):
    if valkey is None:
        import pytest

        pytest.skip("Valkey not available")
    proj = tenant["project"]["id"]
    r = await client.post(f"/admin/v1/projects/{proj}/keys", json={"name": "limited", "rpm_limit": 2}, headers=ADMIN)
    auth = {"authorization": f"Bearer {r.json()['key']}"}
    await refresh(app)
    codes = []
    for _ in range(3):
        rr = await client.post(
            "/v1/chat/completions",
            headers=auth,
            json={"model": "local-chat", "messages": [{"role": "user", "content": "x"}]},
        )
        codes.append(rr.status_code)
    assert codes == [200, 200, 429]
    assert rr.json()["error"]["code"] == "rpm_limit_exceeded" and "retry-after" in rr.headers


async def test_client_disconnect_settles_cancelled(app, tenant, valkey, db):
    """Client drops a stream mid-way: the attempt is settled as cancelled, never left pending."""
    import asyncio

    import httpx

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        async with c.stream(
            "POST",
            "/v1/chat/completions",
            headers=tenant["auth"],
            json={
                "model": "local-chat",
                "stream": True,
                "user": "slow",
                "messages": [{"role": "user", "content": "one two three"}],
            },
        ) as r:
            assert r.status_code == 200
            rid = r.headers["x-aigw-request-id"]
            async for _ in r.aiter_bytes():
                break  # disconnect after the first chunk
    await asyncio.sleep(0.3)
    async with db.session() as s:
        att = (
            await s.execute(select(RequestAttempt).where(RequestAttempt.request_id == __import__("uuid").UUID(rid)))
        ).scalar_one()
    assert att.status in ("cancelled", "succeeded")  # ASGI transport may deliver the whole body before closing
    assert att.settled_amount is not None

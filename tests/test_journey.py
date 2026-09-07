"""Alpha gate: the complete user journey (docs/spec/06 §1)."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from tests.conftest import ADMIN, refresh


def _chunks(text: str) -> list[dict]:
    out = []
    for line in text.split("\n"):
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(json.loads(line[6:]))
    return out


async def test_full_journey(client, app, tenant):
    auth = tenant["auth"]

    # developer discovers models
    r = await client.get("/v1/models", headers=auth)
    assert r.status_code == 200
    assert {m["id"] for m in r.json()["data"]} == {"local-chat", "local-embed"}

    # streaming chat
    r = await client.post(
        "/v1/chat/completions",
        headers=auth,
        json={
            "model": "local-chat",
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "hello world"}],
            "metadata": {"env": "test"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    chunks = _chunks(r.text)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "Echo: hello world"
    assert chunks[-1]["usage"]["completion_tokens"] > 0
    assert r.text.strip().endswith("data: [DONE]")
    request_id = r.headers["x-aigw-request-id"]

    # diagnostics show attempt, routing decision and cost
    r = await client.get(f"/admin/v1/requests/{request_id}", headers=ADMIN)
    assert r.status_code == 200
    att = r.json()["attempts"][0]
    assert att["status"] == "succeeded" and att["usage_source"] == "reported"
    assert att["routing"]["chosen"] == "mock-primary"
    assert Decimal(att["settled_amount"]) > 0
    assert r.json()["usage"][0]["tags"] == {"env": "test"}

    # usage aggregates and budget spend agree
    r = await client.get(
        "/admin/v1/usage", headers=ADMIN, params={"scope_type": "project", "scope_id": tenant["project"]["id"]}
    )
    total = Decimal(r.json()["total_cost"])
    b = (await client.get("/admin/v1/budgets", headers=ADMIN, params={"scope_id": tenant["project"]["id"]})).json()[
        "data"
    ][0]
    assert Decimal(b["spent_amount"]) == total and Decimal(b["reserved_amount"]) == 0

    # non-streaming chat + embeddings
    r = await client.post(
        "/v1/chat/completions",
        headers=auth,
        json={"model": "local-chat", "messages": [{"role": "user", "content": "ping"}]},
    )
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "Echo: ping"
    assert r.headers["x-aigw-deployment"] == "mock-primary"
    r = await client.post("/v1/embeddings", headers=auth, json={"model": "local-embed", "input": ["a", "b"]})
    assert r.status_code == 200 and len(r.json()["data"]) == 2

    # audit trail exists for every management write
    r = await client.get("/admin/v1/audit", headers=ADMIN, params={"org_id": tenant["org"]["id"]})
    actions = {a["action"] for a in r.json()["data"]}
    assert {"organization.create", "team.create", "project.create", "key.create", "budget.create"} <= actions

    # revocation blocks the next request within the refresh interval
    r = await client.post(f"/admin/v1/keys/{tenant['key']['id']}/revoke", headers=ADMIN)
    assert r.status_code == 200
    await asyncio.sleep(0.5)  # refresh interval is 0.2 s in tests; Valkey invalidation is faster when present
    r = await client.post(
        "/v1/chat/completions",
        headers=auth,
        json={"model": "local-chat", "messages": [{"role": "user", "content": "after revoke"}]},
    )
    assert r.status_code == 401 and r.json()["error"]["code"] == "key_revoked"


async def test_restore_verification(client, app, tenant, db):
    from aigw.bootstrap import verify

    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={"model": "local-chat", "messages": [{"role": "user", "content": "x"}]},
    )
    assert r.status_code == 200
    assert await verify(db) == []


async def test_key_rotation_grace(client, app, tenant):
    old_auth = tenant["auth"]
    r = await client.post(f"/admin/v1/keys/{tenant['key']['id']}/rotate", json={"grace_seconds": 60}, headers=ADMIN)
    assert r.status_code == 201
    new_auth = {"authorization": f"Bearer {r.json()['key']}"}
    await refresh(app)
    for auth in (old_auth, new_auth):  # both valid during grace
        r = await client.get("/v1/models", headers=auth)
        assert r.status_code == 200
    r = await client.get("/admin/v1/audit", headers=ADMIN, params={"target_type": "key"})
    assert any(a["action"] == "key.rotate" for a in r.json()["data"])

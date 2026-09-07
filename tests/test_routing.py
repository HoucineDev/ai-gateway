"""Routing, fallback, cooldown, capability validation, tenant isolation, Anthropic adapter."""

from __future__ import annotations

import json
from decimal import Decimal

from tests.conftest import ADMIN, STATE, refresh


async def _add_deployment(client, model_id, name, **kw):
    body = {
        "name": name,
        "provider": "openai_compat",
        "provider_model": "mock-chat",
        "base_url": "http://mock/v1",
        **kw,
    }
    r = await client.post(f"/admin/v1/models/{model_id}/deployments", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


async def test_fallback_on_429_then_cooldown(client, app, tenant):
    """Primary returns 429 → fallback to secondary before first byte; primary is cooled down for the next request."""
    mid = tenant["model"]["id"]
    # make the primary fail: mock keys off request.user, so use a dedicated failing deployment with higher priority
    await client.patch(f"/admin/v1/deployments/{tenant['deployment']['id']}", json={"priority": 1}, headers=ADMIN)
    bad = await _add_deployment(
        client, mid, "bad-primary", priority=0, extra_headers={}, capabilities={"provider_extensions_passthrough": True}
    )
    await refresh(app)
    # steer only the bad deployment: the mock reads `user`, both would fail... so instead use base_url trick:
    # give the bad deployment a base_url path the mock doesn't serve → 404 → invalid_request (no fallback).
    # Use 429 by pointing it at a route that always 429s.
    await client.patch(
        f"/admin/v1/deployments/{bad['id']}", json={"base_url": "http://mock/always429/v1"}, headers=ADMIN
    )
    await refresh(app)
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={"model": "local-chat", "messages": [{"role": "user", "content": "fb"}]},
    )
    assert r.status_code == 200, r.text
    assert r.headers["x-aigw-deployment"] == "mock-primary"
    rid = r.headers["x-aigw-request-id"]
    attempts = (await client.get(f"/admin/v1/requests/{rid}", headers=ADMIN)).json()["attempts"]
    assert [a["status"] for a in attempts] == ["failed", "succeeded"]
    assert attempts[0]["error_class"] == "rate_limited" and Decimal(attempts[0]["settled_amount"]) == 0
    # cooldown: next request skips bad-primary entirely
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={"model": "local-chat", "messages": [{"role": "user", "content": "fb2"}]},
    )
    rid = r.headers["x-aigw-request-id"]
    attempts = (await client.get(f"/admin/v1/requests/{rid}", headers=ADMIN)).json()["attempts"]
    assert len(attempts) == 1 and attempts[0]["routing"]["rejected"] == {"bad-primary": "cooldown"}


async def test_no_eligible_deployment_503(client, app, tenant):
    await client.post(
        f"/admin/v1/deployments/{tenant['deployment']['id']}/cooldown", json={"seconds": 600}, headers=ADMIN
    )
    await refresh(app)
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={"model": "local-chat", "messages": [{"role": "user", "content": "x"}]},
    )
    assert r.status_code == 503 and r.json()["error"]["code"] == "no_eligible_deployment"


async def test_unknown_and_unsupported_parameters(client, app, tenant):
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={"model": "local-chat", "messages": [{"role": "user", "content": "x"}], "foo": 1},
    )
    assert (
        r.status_code == 400
        and r.json()["error"]["code"] == "unknown_parameter"
        and r.json()["error"]["param"] == "foo"
    )
    # narrow the deployment: no tools → request with tools is rejected as unsupported by every deployment
    await client.patch(
        f"/admin/v1/deployments/{tenant['deployment']['id']}", json={"capabilities": {"tools": False}}, headers=ADMIN
    )
    await refresh(app)
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={
            "model": "local-chat",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
        },
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_parameter"


async def test_tool_calls_stream_roundtrip(client, app, tenant):
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={
            "model": "local-chat",
            "stream": True,
            "user": "tool",
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}],
        },
    )
    assert r.status_code == 200
    chunks = [json.loads(line[6:]) for line in r.text.split("\n") if line.startswith("data: {")]
    args = "".join(
        tc["function"]["arguments"]
        for c in chunks
        for tc in c["choices"][0]["delta"].get("tool_calls", [])
        if c["choices"] and tc.get("function")
    )
    assert json.loads(args) == {"city": "Lyon"}
    assert any(c["choices"] and c["choices"][0]["finish_reason"] == "tool_calls" for c in chunks)


async def test_tenant_isolation(client, app, tenant):
    """Org B's key cannot see or use org A's private model; global models remain visible."""
    org_b = (await client.post("/admin/v1/organizations", json={"name": "B", "slug": "org-b"}, headers=ADMIN)).json()
    team_b = (
        await client.post(f"/admin/v1/organizations/{org_b['id']}/teams", json={"name": "t"}, headers=ADMIN)
    ).json()
    proj_b = (await client.post(f"/admin/v1/teams/{team_b['id']}/projects", json={"name": "p"}, headers=ADMIN)).json()
    key_b = (await client.post(f"/admin/v1/projects/{proj_b['id']}/keys", json={"name": "k"}, headers=ADMIN)).json()
    private = (
        await client.post("/admin/v1/models", json={"name": "private-a", "org_id": tenant["org"]["id"]}, headers=ADMIN)
    ).json()
    await _add_deployment(client, private["id"], "priv")
    await refresh(app)
    auth_b = {"authorization": f"Bearer {key_b['key']}"}
    ids = {m["id"] for m in (await client.get("/v1/models", headers=auth_b)).json()["data"]}
    assert "private-a" not in ids and "local-chat" in ids
    r = await client.post(
        "/v1/chat/completions",
        headers=auth_b,
        json={"model": "private-a", "messages": [{"role": "user", "content": "x"}]},
    )
    assert r.status_code == 404
    ids_a = {m["id"] for m in (await client.get("/v1/models", headers=tenant["auth"])).json()["data"]}
    assert "private-a" in ids_a
    # allowed_models narrows visibility further
    r = await client.post(
        f"/admin/v1/projects/{tenant['project']['id']}/keys",
        json={"name": "narrow", "allowed_models": ["local-embed"]},
        headers=ADMIN,
    )
    await refresh(app)
    narrow = {"authorization": f"Bearer {r.json()['key']}"}
    assert {m["id"] for m in (await client.get("/v1/models", headers=narrow)).json()["data"]} == {"local-embed"}


async def test_invalid_tag_rejected(client, app, tenant):
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={"model": "local-chat", "messages": [{"role": "user", "content": "x"}], "metadata": {"nope": "1"}},
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_tag"


async def test_anthropic_adapter_translation(client, app, tenant):
    model = (
        await client.post("/admin/v1/models", json={"name": "claude", "supports_tools": True}, headers=ADMIN)
    ).json()
    r = await client.post(
        f"/admin/v1/models/{model['id']}/deployments",
        json={
            "name": "anthropic-main",
            "provider": "anthropic",
            "provider_model": "claude-mock",
            "base_url": "http://mock",
            "credential_ref": "env:ANTHROPIC_KEY",
        },
        headers=ADMIN,
    )
    assert r.status_code == 201
    await client.post(
        "/admin/v1/prices",
        json={
            "provider": "anthropic",
            "provider_model": "claude-mock",
            "input_per_million": "3",
            "output_per_million": "15",
        },
        headers=ADMIN,
    )
    await refresh(app)
    # unary with system + tools + tool result history
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={
            "model": "claude",
            "user": "tool",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "weather in Lyon?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "t1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city":"Lyon"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "t1", "content": "sunny"},
            ],
            "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object"}}}],
        },
    )
    assert r.status_code == 200, r.text
    sent = STATE["last_body"]
    assert sent["system"] == "be brief"
    assert sent["messages"][1]["content"][0]["type"] == "tool_use"
    assert sent["messages"][2]["content"][0]["type"] == "tool_result"
    assert sent["tools"][0]["input_schema"] == {"type": "object"}
    out = r.json()
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert json.loads(out["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]) == {"city": "Lyon"}
    assert out["usage"]["prompt_tokens"] == 12
    # streaming text
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={"model": "claude", "stream": True, "messages": [{"role": "user", "content": "hi there"}]},
    )
    assert r.status_code == 200
    chunks = [json.loads(line[6:]) for line in r.text.split("\n") if line.startswith("data: {")]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text.strip() == "Echo: hi there"
    rid = r.headers["x-aigw-request-id"]
    att = (await client.get(f"/admin/v1/requests/{rid}", headers=ADMIN)).json()["attempts"][0]
    assert att["usage_source"] == "reported" and att["provider"] == "anthropic"
    # unsupported param for anthropic (json_object) → 400
    r = await client.post(
        "/v1/chat/completions",
        headers=tenant["auth"],
        json={
            "model": "claude",
            "messages": [{"role": "user", "content": "x"}],
            "response_format": {"type": "json_object"},
        },
    )
    assert r.status_code == 400
    # embeddings against a chat-only model
    r = await client.post("/v1/embeddings", headers=tenant["auth"], json={"model": "claude", "input": "x"})
    assert r.status_code == 400


async def test_admin_auth_required(client, app):
    r = await client.get("/admin/v1/organizations")
    assert r.status_code == 401
    r = await client.get("/admin/v1/organizations", headers={"x-admin-key": "wrong"})
    assert r.status_code == 401


async def test_audit_is_append_only(client, app, tenant, db):
    import pytest
    from sqlalchemy import text

    async with db.session() as s:
        with pytest.raises(Exception, match="append-only"):
            await s.execute(text("DELETE FROM audit_events"))

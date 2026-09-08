"""Exact response cache (docs/spec/04 §9): per-project policy, tenant scoping, zero-cost hits, directives, streams."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from aigw.gateway.cache import CachePolicy, cache_key, is_deterministic
from aigw.testing.mock_upstream import STATE
from tests.conftest import ADMIN, refresh

CHAT = {"model": "local-chat", "messages": [{"role": "user", "content": "hello cache"}]}


async def enable_cache(client, app, project_id: str, **cache) -> None:
    r = await client.patch(
        f"/admin/v1/projects/{project_id}",
        json={"settings": {"allowed_tags": ["env", "feature"], "cache": {"enabled": True, **cache}}},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    await refresh(app)


async def chat(client, auth, body=CHAT, **headers):
    return await client.post("/v1/chat/completions", json=body, headers={**auth, **headers})


def test_policy_and_determinism():
    assert CachePolicy.from_project_settings({}) is None
    assert CachePolicy.from_project_settings({"cache": {"enabled": False}}) is None
    assert CachePolicy.from_project_settings({"cache": {"enabled": True}}) == CachePolicy(300, False)
    assert CachePolicy.from_project_settings({"cache": {"enabled": True, "ttl_seconds": 0}}) is None
    assert CachePolicy.from_project_settings(
        {"cache": {"enabled": True, "ttl_seconds": 60, "deterministic_only": 1}}
    ) == CachePolicy(60, True)
    from aigw.core.types import ChatRequest, EmbeddingRequest

    assert is_deterministic(EmbeddingRequest(model="e", input="x"))
    assert not is_deterministic(ChatRequest(**CHAT))
    assert is_deterministic(ChatRequest(**CHAT, temperature=0))
    assert is_deterministic(ChatRequest(**CHAT, seed=7))


def test_key_scoping():
    from types import SimpleNamespace as NS

    from aigw.core.types import ChatRequest

    scope_a = NS(org_id="o1", project_id="p1")
    scope_b = NS(org_id="o1", project_id="p2")
    dep = NS(provider="openai_compat", provider_model="mock-chat")
    other = NS(provider="openai", provider_model="gpt")
    req = ChatRequest(**CHAT)
    k = cache_key(scope_a, "local-chat", dep, req)
    assert k.startswith("rc:p1:")
    assert cache_key(scope_b, "local-chat", dep, req) != k  # another project
    assert cache_key(scope_a, "local-chat", other, req) != k  # another provider model
    assert cache_key(scope_a, "local-chat", dep, ChatRequest(**CHAT, temperature=0)) != k  # body differs
    # fields that do not change the answer do not fragment the cache
    same = ChatRequest(**CHAT, stream=True, user="someone", metadata={"env": "dev"})
    assert cache_key(scope_a, "local-chat", dep, same) == k


async def test_off_by_default(client, tenant):
    r = await chat(client, tenant["auth"])
    assert r.status_code == 200 and r.headers["x-aigw-cache"] == "off"
    assert "cached" not in r.json()["aigw"]


async def test_hit_is_free_and_tenant_scoped(client, app, valkey, tenant):
    if valkey is None:
        pytest.skip("Valkey not reachable: the response cache needs it")
    await enable_cache(client, app, tenant["project"]["id"])
    calls = STATE["calls"]
    r1 = await chat(client, tenant["auth"])
    assert r1.status_code == 200 and r1.headers["x-aigw-cache"] == "miss"
    assert STATE["calls"] == calls + 1
    r2 = await chat(client, tenant["auth"], **{"x-aigw-cache": ""})
    assert r2.headers["x-aigw-cache"] == "hit" and STATE["calls"] == calls + 1
    assert r2.json()["choices"] == r1.json()["choices"]
    assert r2.json()["aigw"]["cached"] is True and r2.json()["id"] != r1.json()["id"]
    assert r2.headers["x-aigw-deployment"] == "mock-primary"
    # the hit is a zero-cost cached attempt, visible in diagnostics and usage; spend did not move
    diag = (await client.get(f"/admin/v1/requests/{r2.json()['aigw']['request_id']}", headers=ADMIN)).json()
    assert diag["attempts"][0]["status"] == "cached" and Decimal(diag["attempts"][0]["reserved_amount"]) == 0
    assert diag["attempts"][0]["routing"]["cache"] == "hit"
    assert Decimal(diag["usage"][0]["cost"]) == 0 and diag["usage"][0]["usage_source"] == "cache"
    assert diag["usage"][0]["prompt_tokens"] == r1.json()["usage"]["prompt_tokens"]
    spend = (
        await client.get(
            "/admin/v1/usage", params={"scope_type": "project", "scope_id": tenant["project"]["id"]}, headers=ADMIN
        )
    ).json()
    assert spend["data"][0]["requests"] == 2 and Decimal(spend["total_cost"]) == await _cost_of(client, r1)

    # `user` and `metadata` do not fragment the cache; a different prompt does
    r3 = await chat(client, tenant["auth"], {**CHAT, "user": "alice", "metadata": {"env": "prod"}})
    assert r3.headers["x-aigw-cache"] == "hit"
    r4 = await chat(client, tenant["auth"], {**CHAT, "messages": [{"role": "user", "content": "other"}]})
    assert r4.headers["x-aigw-cache"] == "miss"

    # another project in the same org never sees the entry
    team = tenant["team"]["id"]
    p2 = (await client.post(f"/admin/v1/teams/{team}/projects", json={"name": "p2"}, headers=ADMIN)).json()
    await enable_cache(client, app, p2["id"])
    k2 = (await client.post(f"/admin/v1/projects/{p2['id']}/keys", json={"name": "k2"}, headers=ADMIN)).json()
    await refresh(app)
    r5 = await chat(client, {"authorization": f"Bearer {k2['key']}"})
    assert r5.headers["x-aigw-cache"] == "miss"


async def _cost_of(client, r):
    diag = (await client.get(f"/admin/v1/requests/{r.json()['aigw']['request_id']}", headers=ADMIN)).json()
    return Decimal(diag["usage"][0]["cost"])


async def test_directives(client, app, valkey, tenant):
    if valkey is None:
        pytest.skip("Valkey not reachable")
    await enable_cache(client, app, tenant["project"]["id"])
    await chat(client, tenant["auth"])
    calls = STATE["calls"]
    r = await chat(client, tenant["auth"], **{"x-aigw-cache": "no-store"})
    assert r.headers["x-aigw-cache"] == "bypass" and STATE["calls"] == calls + 1
    r = await chat(client, tenant["auth"], **{"x-aigw-cache": "no-cache"})
    assert r.headers["x-aigw-cache"] == "refresh" and STATE["calls"] == calls + 2
    r = await chat(client, tenant["auth"])
    assert r.headers["x-aigw-cache"] == "hit" and STATE["calls"] == calls + 2


async def test_deterministic_only(client, app, valkey, tenant):
    if valkey is None:
        pytest.skip("Valkey not reachable")
    await enable_cache(client, app, tenant["project"]["id"], deterministic_only=True)
    r = await chat(client, tenant["auth"])
    assert r.headers["x-aigw-cache"] == "off"
    pinned = {**CHAT, "temperature": 0}
    assert (await chat(client, tenant["auth"], pinned)).headers["x-aigw-cache"] == "miss"
    assert (await chat(client, tenant["auth"], pinned)).headers["x-aigw-cache"] == "hit"


async def test_streams_populate_and_replay(client, app, valkey, tenant):
    if valkey is None:
        pytest.skip("Valkey not reachable")
    await enable_cache(client, app, tenant["project"]["id"])
    body = {**CHAT, "stream": True, "stream_options": {"include_usage": True}}
    calls = STATE["calls"]
    r = await chat(client, tenant["auth"], body)
    assert r.status_code == 200 and r.headers["x-aigw-cache"] == "miss" and STATE["calls"] == calls + 1
    text = "".join(_content(r.text))
    # replayed to a streaming client without touching the upstream
    r2 = await chat(client, tenant["auth"], body)
    assert r2.headers["x-aigw-cache"] == "hit" and STATE["calls"] == calls + 1
    chunks = _chunks(r2.text)
    assert "".join(_content(r2.text)) == text
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert any(c["choices"] and c["choices"][0]["finish_reason"] == "stop" for c in chunks)
    assert chunks[-1]["usage"]["completion_tokens"] > 0 and chunks[-1]["choices"] == []
    assert r2.text.rstrip().endswith("data: [DONE]")
    # and to a unary client
    r3 = await chat(client, tenant["auth"])
    assert r3.headers["x-aigw-cache"] == "hit" and r3.json()["choices"][0]["message"]["content"] == text
    # tool-call answers are streamed but never stored
    tools = {
        **body,
        "user": "tool",
        "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
    }
    r4 = await chat(client, tenant["auth"], tools)
    assert r4.status_code == 200 and r4.headers["x-aigw-cache"] == "miss"
    r5 = await chat(client, tenant["auth"], tools)
    assert r5.headers["x-aigw-cache"] == "miss"


async def test_embeddings_cached(client, app, valkey, tenant):
    if valkey is None:
        pytest.skip("Valkey not reachable")
    await enable_cache(client, app, tenant["project"]["id"])
    body = {"model": "local-embed", "input": ["a", "b"]}
    r1 = await client.post("/v1/embeddings", json=body, headers=tenant["auth"])
    assert r1.status_code == 200 and r1.headers["x-aigw-cache"] == "miss"
    r2 = await client.post("/v1/embeddings", json=body, headers=tenant["auth"])
    assert r2.headers["x-aigw-cache"] == "hit" and r2.json()["data"] == r1.json()["data"]
    assert r2.json()["aigw"]["cached"] is True


def _chunks(sse: str) -> list[dict]:
    out = []
    for line in sse.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            out.append(json.loads(line[6:]))
    return out


def _content(sse: str) -> list[str]:
    return [c["choices"][0]["delta"].get("content", "") for c in _chunks(sse) if c.get("choices")]

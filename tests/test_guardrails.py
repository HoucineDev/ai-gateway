"""Guardrail pipeline (docs/spec/04 §11): detectors, policy validation, pre block/redact at zero cost, post redact
and block on unary and streamed answers (tail and buffer), timeouts with fail-open/closed, audit trail and scopes."""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from aigw.config import Settings
from aigw.gateway.guardrails import DETECTORS, GuardrailPolicy, GuardrailRunner, Rule
from aigw.testing.mock_upstream import STATE
from tests.conftest import ADMIN, TEST_DB, TEST_VALKEY, refresh
from tests.test_admin_delegation import bind, tok
from tests.test_admin_oidc import AUDIENCE, ISSUER, FakeKeycloak

# ---- detectors and policy (no database) ----------------------------------------


async def test_builtin_detectors():
    pii = DETECTORS["pii"]
    v = await pii.check(
        "mail bob@example.com, call +33 6 12 34 56 78, IBAN FR76 3000 6000 0112 3456 7890 189", {}, {}, None
    )
    assert v.flagged and set(v.categories) == {"pii:email", "pii:phone", "pii:iban"}
    assert "[EMAIL]" in v.text and "[PHONE]" in v.text and "[IBAN]" in v.text and "bob@" not in v.text
    ok = await pii.check("card 4111 1111 1111 1111", {"kinds": ["card"]}, {}, None)
    assert ok.categories == ["pii:card"] and ok.text == "card [CARD]"
    bad = await pii.check("order 1234 5678 9012 3456 shipped", {"kinds": ["card"]}, {}, None)
    assert not bad.flagged  # fails Luhn: not a card
    assert not (await pii.check("nothing here", {}, {}, None)).flagged

    rx = await DETECTORS["regex"].check(
        "ticket ACME-1234 is internal",
        {"patterns": [{"pattern": r"ACME-\d+", "category": "ticket", "replacement": "<id>"}]},
        {},
        None,
    )
    assert rx.flagged and rx.categories == ["ticket"] and rx.text == "ticket <id> is internal"
    kw = await DETECTORS["keyword"].check("The Secret plan", {"words": ["secret"]}, {}, None)
    assert kw.categories == ["keyword:secret"] and kw.text == "The *** plan"
    assert not (await DETECTORS["keyword"].check("secretive", {"words": ["secret"]}, {}, None)).flagged  # whole words


def test_policy_validation():
    assert GuardrailPolicy.from_project_settings({}) is None
    assert GuardrailPolicy.from_project_settings({"guardrails": {"pre": [], "post": []}}) is None
    p = GuardrailPolicy.from_project_settings(
        {"guardrails": {"pre": [{"detector": "pii", "action": "redact", "kinds": ["email"]}], "post_stream": "buffer"}}
    )
    assert (
        p.pre == (Rule("pii", "redact", "closed", 1000, {"kinds": ["email"]}),)
        and p.post == ()
        and p.post_stream == "buffer"
    )
    for bad in (
        {"guardrails": {"pre": [{"detector": "nope"}]}},
        {"guardrails": {"pre": [{"detector": "pii", "action": "delete"}]}},
        {"guardrails": {"pre": [{"detector": "pii", "kinds": ["ssn"]}]}},
        {"guardrails": {"pre": [{"detector": "regex", "patterns": [{"pattern": "("}]}]}},
        {"guardrails": {"pre": [{"detector": "http", "url": "ftp://x"}]}},
        {"guardrails": {"post_stream": "sometimes", "pre": [{"detector": "keyword", "words": ["x"]}]}},
    ):
        with pytest.raises(ValueError):
            GuardrailPolicy.from_project_settings(bad)


async def test_runner_chains_redactions_and_stops_on_block():
    rules = (
        Rule("keyword", "redact", "closed", 1000, {"words": ["alpha"]}),
        Rule("keyword", "block", "closed", 1000, {"words": ["beta"]}),
        Rule("keyword", "flag", "closed", 1000, {"words": ["gamma"]}),
    )
    text, outcomes = await GuardrailRunner().run(rules, "alpha beta gamma", {"direction": "pre"})
    assert text == "*** beta gamma" and [o.action for o in outcomes] == ["redact", "block"]  # gamma never evaluated
    text, outcomes = await GuardrailRunner().run(rules, "alpha gamma", {"direction": "pre"})
    assert text == "*** gamma" and [o.action for o in outcomes] == ["redact", "flag"]


# ---- through the gateway ---------------------------------------------------------


async def enable(client, app, project_id: str, **guardrails) -> None:
    r = await client.patch(
        f"/admin/v1/projects/{project_id}",
        json={"settings": {"allowed_tags": ["env", "feature"], "guardrails": guardrails}},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    await refresh(app)


def chat(content: str, **extra):
    return {"model": "local-chat", "messages": [{"role": "user", "content": content}], **extra}


async def events(client, **params):
    r = await client.get("/admin/v1/guardrail-events", params=params, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def test_pre_block_costs_nothing_and_is_audited(client, app, tenant):
    pid = tenant["project"]["id"]
    await enable(client, app, pid, pre=[{"detector": "pii", "action": "block", "kinds": ["email"]}])
    calls = STATE["calls"]
    r = await client.post("/v1/chat/completions", json=chat("write to ceo@example.com"), headers=tenant["auth"])
    assert (r.status_code, r.json()["error"]["code"]) == (400, "guardrail_blocked")
    assert "pii" in r.json()["error"]["message"] and "pii:email" in r.json()["error"]["message"]
    assert STATE["calls"] == calls  # never reached the provider
    ev = await events(client, project_id=pid)
    assert len(ev) == 1 and (ev[0]["direction"], ev[0]["detector"], ev[0]["action"]) == ("pre", "pii", "block")
    assert ev[0]["categories"] == ["pii:email"] and ev[0]["request_id"] == r.headers.get(
        "x-aigw-request-id", ev[0]["request_id"]
    )
    budget = (
        await client.get("/admin/v1/budgets", params={"scope_type": "project", "scope_id": pid}, headers=ADMIN)
    ).json()["data"][0]
    assert budget["spent_amount"].startswith("0") and budget["reserved_amount"].startswith("0")
    # embeddings inputs are screened too
    r = await client.post(
        "/v1/embeddings", json={"model": "local-embed", "input": ["fine", "x@y.io"]}, headers=tenant["auth"]
    )
    assert (r.status_code, r.json()["error"]["code"]) == (400, "guardrail_blocked")
    # clean traffic is untouched and records nothing
    r = await client.post("/v1/chat/completions", json=chat("hello"), headers=tenant["auth"])
    assert r.status_code == 200 and len(await events(client, project_id=pid)) == 2


async def test_pre_redact_rewrites_what_the_provider_sees(client, app, tenant):
    pid = tenant["project"]["id"]
    await enable(
        client,
        app,
        pid,
        pre=[{"detector": "pii", "action": "redact"}, {"detector": "keyword", "action": "flag", "words": ["urgent"]}],
    )
    r = await client.post(
        "/v1/chat/completions",
        json={
            "model": "local-chat",
            "messages": [
                {"role": "system", "content": "reply to alice@corp.io"},
                {"role": "user", "content": [{"type": "text", "text": "urgent: card 4111 1111 1111 1111"}]},
            ],
        },
        headers=tenant["auth"],
    )
    assert r.status_code == 200, r.text
    sent = STATE["last_body"]["messages"]
    assert sent[0]["content"] == "reply to [EMAIL]" and sent[1]["content"][0]["text"] == "urgent: card [CARD]"
    assert (
        r.json()["choices"][0]["message"]["content"] == "Echo: urgent: card [CARD]"
    )  # the mock echoes the redacted text
    ev = await events(client, project_id=pid)
    assert sorted((e["detector"], e["action"]) for e in ev) == [
        ("keyword", "flag"),
        ("pii", "redact"),
        ("pii", "redact"),
    ]
    diag = (await client.get(f"/admin/v1/requests/{r.json()['aigw']['request_id']}", headers=ADMIN)).json()
    assert len(diag["guardrails"]) == 3


async def test_post_redact_and_block_unary(client, app, tenant):
    pid = tenant["project"]["id"]
    await enable(client, app, pid, post=[{"detector": "keyword", "action": "redact", "words": ["secret"]}])
    r = await client.post("/v1/chat/completions", json=chat("the secret sauce"), headers=tenant["auth"])
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "Echo: the *** sauce"
    assert (await events(client, project_id=pid, direction="post"))[0]["action"] == "redact"
    # block after the provider answered: the attempt is settled (paid) and the client gets 400
    await enable(client, app, pid, post=[{"detector": "http", "url": "http://mock/guardrail", "action": "block"}])
    r = await client.post("/v1/chat/completions", json=chat("say [unsafe] things"), headers=tenant["auth"])
    assert (r.status_code, r.json()["error"]["code"]) == (400, "guardrail_blocked")
    assert STATE["last_guardrail"]["direction"] == "post" and STATE["last_guardrail"]["project_id"] == pid
    usage = (
        await client.get("/admin/v1/usage", params={"scope_type": "project", "scope_id": pid}, headers=ADMIN)
    ).json()
    assert sum(g["requests"] for g in usage["data"]) == 2  # both answers were paid for
    assert (await events(client, project_id=pid, action="block"))[0]["categories"] == ["violence"]


async def test_post_stream_tail_and_buffer(client, app, tenant):
    pid = tenant["project"]["id"]
    rule = {"detector": "http", "url": "http://mock/guardrail", "action": "block"}
    await enable(client, app, pid, post=[rule], post_stream="tail")
    r = await client.post("/v1/chat/completions", json=chat("[unsafe] tail", stream=True), headers=tenant["auth"])
    assert r.status_code == 200
    chunks = [
        json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert any(
        c.get("choices") and c["choices"][0]["delta"].get("content") for c in chunks
    )  # content was already delivered
    assert chunks[-1]["error"]["code"] == "guardrail_blocked" and r.text.rstrip().endswith("data: [DONE]")

    await enable(client, app, pid, post=[rule], post_stream="buffer")
    r = await client.post("/v1/chat/completions", json=chat("[unsafe] buffered", stream=True), headers=tenant["auth"])
    chunks = [
        json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert len(chunks) == 1 and chunks[0]["error"]["code"] == "guardrail_blocked"  # nothing leaked

    # buffered redaction rebuilds the stream from the redacted text; clean streams pass through untouched
    await enable(client, app, pid, post=[{**rule, "action": "redact"}], post_stream="buffer")
    r = await client.post(
        "/v1/chat/completions",
        json=chat("[unsafe] again", stream=True, stream_options={"include_usage": True}),
        headers=tenant["auth"],
    )
    chunks = [
        json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"
    ]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices"))
    assert text == "Echo: [REDACTED] again" and chunks[-1]["usage"]["prompt_tokens"] > 0
    r = await client.post("/v1/chat/completions", json=chat("all good", stream=True), headers=tenant["auth"])
    chunks = [
        json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices")) == "Echo: all good"
    assert len(await events(client, project_id=pid, action="block")) == 2


async def test_detector_failures_fail_closed_or_open(client, app, tenant):
    pid = tenant["project"]["id"]
    await enable(
        client,
        app,
        pid,
        pre=[{"detector": "http", "url": "http://mock/guardrail", "action": "block", "timeout_ms": 200}],
    )
    r = await client.post("/v1/chat/completions", json=chat("[slowguard] hi"), headers=tenant["auth"])
    assert (r.status_code, r.json()["error"]["code"]) == (400, "guardrail_blocked")
    ev = (await events(client, project_id=pid))[0]
    assert ev["action"] == "block" and ev["categories"] == ["detector_error"] and ev["detail"]["error"] == "timeout"
    await enable(
        client, app, pid, pre=[{"detector": "http", "url": "http://mock/guardrail", "action": "block", "fail": "open"}]
    )
    r = await client.post("/v1/chat/completions", json=chat("[guarderr] hi"), headers=tenant["auth"])
    assert r.status_code == 200, r.text
    ev = (await events(client, project_id=pid, action="error"))[0]
    assert ev["detector"] == "http" and "500" in ev["detail"]["error"]
    # an invalid policy fails the project closed rather than silently disabling protection
    r = await client.patch(
        f"/admin/v1/projects/{pid}", json={"settings": {"guardrails": {"pre": [{"detector": "nope"}]}}}, headers=ADMIN
    )
    assert r.status_code == 200
    await refresh(app)
    r = await client.post("/v1/chat/completions", json=chat("hi"), headers=tenant["auth"])
    assert (r.status_code, r.json()["error"]["code"]) == (503, "guardrail_config_invalid")


async def test_delegate_sees_only_its_tenant_events(client, app, tenant):
    pid = tenant["project"]["id"]
    await enable(client, app, pid, pre=[{"detector": "keyword", "action": "flag", "words": ["flagme"]}])
    await client.post("/v1/chat/completions", json=chat("flagme please"), headers=tenant["auth"])
    org2 = (await client.post("/admin/v1/organizations", json={"name": "Other", "slug": "other"}, headers=ADMIN)).json()
    assert (await bind(client, "dan", "project_member", "project", pid)).status_code == 201
    r = await client.get("/admin/v1/guardrail-events", headers=tok("dan"))
    assert r.status_code == 200 and [e["project_id"] for e in r.json()["data"]] == [pid]
    assert (await bind(client, "eve", "org_owner", "organization", org2["id"])).status_code == 201
    r = await client.get("/admin/v1/guardrail-events", headers=tok("eve"))
    assert r.status_code == 200 and r.json()["data"] == []


@pytest.fixture
def keycloak():
    return FakeKeycloak()


@pytest_asyncio.fixture
async def oidc_http_client(keycloak):
    client = keycloak.client()
    yield client
    await client.aclose()


@pytest.fixture
def settings():
    return Settings(
        role="all",
        database_url=TEST_DB,
        valkey_url=TEST_VALKEY,
        admin_key="test-admin",
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        config_refresh_seconds=0.2,
        default_max_output_tokens=256,
    )

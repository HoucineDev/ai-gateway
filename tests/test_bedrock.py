"""Bedrock adapter (docs/spec/03 §4.3): SigV4 (reference vector + live verification), Converse translation,
event-stream streaming, Titan/Cohere embeddings, bearer auth, health probe."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from aigw.adapters import eventstream, sigv4
from aigw.testing.mock_upstream import STATE
from tests.conftest import ADMIN, refresh

CREDS = "literal:AKIATESTACCESSKEY:test-secret-key"


async def _bedrock_model(
    client,
    app,
    name="claude-br",
    model="anthropic.claude-3-5-sonnet-20241022-v2:0",
    credential=CREDS,
    modalities=None,
    **caps,
):
    body = {"name": name, "supports_tools": True, "supports_vision": True}
    if modalities:
        body["modalities"] = modalities
    m = (await client.post("/admin/v1/models", json=body, headers=ADMIN)).json()
    r = await client.post(
        f"/admin/v1/models/{m['id']}/deployments",
        json={
            "name": f"br-{name}",
            "provider": "bedrock",
            "provider_model": model,
            "base_url": "http://mock",
            "credential_ref": credential,
            "capabilities": {"region": "eu-west-1", **caps},
        },
        headers=ADMIN,
    )
    assert r.status_code == 201, r.text
    await client.post(
        "/admin/v1/prices",
        json={"provider": "bedrock", "provider_model": model, "input_per_million": "3", "output_per_million": "15"},
        headers=ADMIN,
    )
    await refresh(app)
    return m, r.json()


def _chat(model="claude-br", **extra):
    return {"model": model, "messages": [{"role": "user", "content": "salut"}], **extra}


# ---- owned primitives ------------------------------------------------------------


def test_sigv4_matches_aws_reference_vector():
    """AWS documentation example (GET iam ListUsers, AKIDEXAMPLE, 2015-08-30T12:36:00Z)."""
    creds = sigv4.AwsCredentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    url = "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08"
    headers = {
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
        "host": "iam.amazonaws.com",
        "x-amz-date": "20150830T123600Z",
    }
    creq = sigv4.canonical_request("GET", url, headers, ["content-type", "host", "x-amz-date"], sigv4.sha256_hex(b""))
    assert sigv4.sha256_hex(creq.encode()) == "f536975d06c0309214f805bb90ccff089219ecd68b2577efef23edd43b7e1a59"
    sts = "\n".join(
        [sigv4.ALGORITHM, "20150830T123600Z", "20150830/us-east-1/iam/aws4_request", sigv4.sha256_hex(creq.encode())]
    )
    import hashlib
    import hmac

    sig = hmac.new(
        sigv4.signing_key(creds.secret_access_key, "20150830", "us-east-1", "iam"), sts.encode(), hashlib.sha256
    ).hexdigest()
    assert sig == "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    # the full signer adds x-amz-content-sha256 and the authorization header shape
    signed = sigv4.sign(
        "GET",
        url,
        {"content-type": headers["content-type"]},
        b"",
        creds,
        "us-east-1",
        "iam",
        now=datetime(2015, 8, 30, 12, 36, tzinfo=UTC),
    )
    assert signed["x-amz-date"] == "20150830T123600Z" and signed["host"] == "iam.amazonaws.com"
    assert signed["authorization"].startswith(
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, SignedHeaders=content-type;host;x-amz-content-sha256;x-amz-date, Signature="
    )
    assert sigv4.canonical_uri("/model/anthropic.claude-3%3A0/converse") == "/model/anthropic.claude-3%253A0/converse"
    assert sigv4.AwsCredentials.parse(
        '{"aws_access_key_id": "A", "aws_secret_access_key": "S", "aws_session_token": "T"}'
    ) == sigv4.AwsCredentials("A", "S", "T")
    assert sigv4.AwsCredentials.parse("A:S") == sigv4.AwsCredentials("A", "S", None)


def test_event_stream_roundtrip():
    msgs = [
        ({":message-type": "event", ":event-type": "messageStart"}, b'{"role":"assistant"}'),
        ({":message-type": "event", ":event-type": "metadata"}, b"{}"),
    ]
    blob = b"".join(eventstream.encode(h, p) for h, p in msgs)
    assert eventstream.decode_all(blob) == msgs
    # arbitrary chunking reassembles; corrupted CRC is rejected
    import asyncio

    async def collect():
        chunks = [blob[i : i + 7] for i in range(0, len(blob), 7)]
        return [m async for m in eventstream.iter_messages(chunks)]

    assert asyncio.run(collect()) == msgs
    bad = bytearray(blob)
    bad[-1] ^= 0xFF
    try:
        eventstream.decode_all(bytes(bad))
    except eventstream.EventStreamError:
        pass
    else:
        raise AssertionError("corrupted CRC accepted")


# ---- through the gateway ---------------------------------------------------------


async def test_converse_translation_and_signature(client, app, tenant):
    await _bedrock_model(client, app)
    r = await client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-br",
            "max_tokens": 50,
            "temperature": 0.1,
            "stop": "END",
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "w",
                        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                    },
                }
            ],
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "weather in Lyon?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "tooluse_0",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city": "Lyon"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "tooluse_0", "content": '{"temp": 21}'},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "and Paris?"},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
                    ],
                },
            ],
        },
        headers=tenant["auth"],
    )
    assert r.status_code == 200, r.text
    assert STATE["sig_ok"] and STATE["sig_region"] == "eu-west-1"
    assert STATE["last_model"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert STATE["last_headers"]["x-amz-content-sha256"] and "x-amz-security-token" not in STATE["last_headers"]
    b = STATE["last_body"]
    assert b["system"] == [{"text": "be brief"}]
    assert [m["role"] for m in b["messages"]] == ["user", "assistant", "user", "user"]
    assert b["messages"][1]["content"] == [
        {"toolUse": {"toolUseId": "tooluse_0", "name": "get_weather", "input": {"city": "Lyon"}}}
    ]
    assert b["messages"][2]["content"] == [
        {"toolResult": {"toolUseId": "tooluse_0", "content": [{"json": {"temp": 21}}]}}
    ]
    assert b["messages"][3]["content"] == [
        {"text": "and Paris?"},
        {"image": {"format": "jpeg", "source": {"bytes": "QUJD"}}},
    ]
    assert b["inferenceConfig"] == {"maxTokens": 50, "temperature": 0.1, "stopSequences": ["END"]}
    assert b["toolConfig"]["toolChoice"] == {"tool": {"name": "get_weather"}}
    assert b["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]["properties"]["city"] == {"type": "string"}
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "Echo: and Paris?" and body["usage"]["prompt_tokens"] == 12
    diag = (await client.get(f"/admin/v1/requests/{body['aigw']['request_id']}", headers=ADMIN)).json()
    assert float(diag["usage"][0]["cost"]) > 0 and diag["usage"][0]["provider"] == "bedrock"


async def test_bad_secret_is_refused_by_signature_check(client, app, tenant):
    await _bedrock_model(client, app, credential="literal:AKIATESTACCESSKEY:wrong-secret")
    r = await client.post("/v1/chat/completions", json=_chat(), headers=tenant["auth"])
    assert r.status_code >= 400 and STATE["sig_ok"] is False
    # session tokens are signed too
    await _bedrock_model(
        client, app, name="claude-sts", credential="literal:AKIATESTACCESSKEY:test-secret-key:SESSIONTOKEN"
    )
    r = await client.post("/v1/chat/completions", json=_chat("claude-sts"), headers=tenant["auth"])
    assert r.status_code == 200, r.text
    assert (
        STATE["last_headers"]["x-amz-security-token"] == "SESSIONTOKEN"
        and "x-amz-security-token" in STATE["last_headers"]["authorization"]
    )


async def test_tool_call_streaming_and_unsupported(client, app, tenant):
    await _bedrock_model(client, app)
    r = await client.post(
        "/v1/chat/completions",
        json=_chat(
            messages=[{"role": "user", "content": "[tool] w"}],
            tools=[{"type": "function", "function": {"name": "get_weather"}}],
        ),
        headers=tenant["auth"],
    )
    assert r.status_code == 200, r.text
    assert r.json()["choices"][0]["message"]["tool_calls"][0] == {
        "id": "tooluse_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "Lyon"}'},
    }
    assert r.json()["choices"][0]["finish_reason"] == "tool_calls"

    r = await client.post(
        "/v1/chat/completions", json=_chat(stream=True, stream_options={"include_usage": True}), headers=tenant["auth"]
    )
    assert r.status_code == 200, r.text
    chunks = [
        json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices")) == "Echo: salut"
    assert chunks[-1]["usage"]["prompt_tokens"] == 12 and any(
        c["choices"] and c["choices"][0]["finish_reason"] == "stop" for c in chunks
    )

    r = await client.post(
        "/v1/chat/completions",
        json=_chat(
            stream=True,
            messages=[{"role": "user", "content": "[tool] w"}],
            tools=[{"type": "function", "function": {"name": "get_weather"}}],
        ),
        headers=tenant["auth"],
    )
    chunks = [
        json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"
    ]
    args = "".join(
        tc["function"].get("arguments", "")
        for c in chunks
        if c.get("choices")
        for tc in c["choices"][0]["delta"].get("tool_calls", [])
    )
    assert args == '{"city": "Lyon"}' and any(
        c["choices"] and c["choices"][0]["finish_reason"] == "tool_calls" for c in chunks
    )

    r = await client.post(
        "/v1/chat/completions", json=_chat(response_format={"type": "json_object"}), headers=tenant["auth"]
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_parameter"
    r = await client.post(
        "/v1/chat/completions",
        json=_chat(
            messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}]}]
        ),
        headers=tenant["auth"],
    )
    assert r.status_code == 400


async def test_embeddings_titan_and_cohere(client, app, tenant):
    await _bedrock_model(client, app, name="titan", model="amazon.titan-embed-text-v2:0", modalities=["embedding"])
    await _bedrock_model(client, app, name="cohere", model="cohere.embed-multilingual-v3", modalities=["embedding"])
    calls = STATE["calls"]
    r = await client.post(
        "/v1/embeddings", json={"model": "titan", "input": ["a", "bb"], "dimensions": 4}, headers=tenant["auth"]
    )
    assert r.status_code == 200, r.text
    assert STATE["calls"] == calls + 2 and STATE["last_body"]["dimensions"] == 4  # one Titan invocation per input
    assert [len(e["embedding"]) for e in r.json()["data"]] == [4, 4] and r.json()["usage"]["prompt_tokens"] == 2
    r = await client.post("/v1/embeddings", json={"model": "cohere", "input": "x"}, headers=tenant["auth"])
    assert (
        r.status_code == 200
        and STATE["last_body"]["texts"] == ["x"]
        and r.json()["data"][0]["embedding"] == [0.0, 0.2, 0.3]
    )


async def test_bearer_api_key_and_health_probe(client, app, db, valkey, tenant):
    from aigw.gateway.ratelimit import CooldownStore
    from aigw.worker.health import HealthChecker
    from tests.test_health import health_settings

    _, dep = await _bedrock_model(client, app, credential="literal:bedrock-api-key", auth="bearer")
    r = await client.post("/v1/chat/completions", json=_chat(), headers=tenant["auth"])
    assert r.status_code == 200, r.text
    assert STATE["last_headers"]["authorization"] == "Bearer bedrock-api-key"
    _, dep2 = await _bedrock_model(client, app, name="claude-sig")
    hc = HealthChecker(
        db, health_settings(), app.state.adapters, app.state.secrets, CooldownStore(valkey), app.state.upstream
    )
    statuses = await hc.run_once()
    assert statuses[dep["id"]] == "healthy" and statuses[dep2["id"]] == "healthy"  # signed GET on the control plane

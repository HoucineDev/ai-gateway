"""Gemini adapter (docs/spec/03 §4.2): Vertex + Google AI addressing, translation, streaming, embeddings, auth."""

from __future__ import annotations

import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from aigw.testing.mock_upstream import STATE
from tests.conftest import ADMIN, refresh

VERTEX = {"project": "proj-1", "location": "europe-west1"}


async def _gemini_model(
    client, app, name="gem", model="gemini-mock", credential="literal:gemini-test-token", modalities=None, **caps
):
    body = {"name": name, "supports_tools": True, "supports_json_schema": True, "supports_vision": True}
    if modalities:
        body["modalities"] = modalities
    m = (await client.post("/admin/v1/models", json=body, headers=ADMIN)).json()
    r = await client.post(
        f"/admin/v1/models/{m['id']}/deployments",
        json={
            "name": f"gemini-{name}",
            "provider": "gemini",
            "provider_model": model,
            "base_url": "http://mock",
            "credential_ref": credential,
            "capabilities": caps,
        },
        headers=ADMIN,
    )
    assert r.status_code == 201, r.text
    await client.post(
        "/admin/v1/prices",
        json={"provider": "gemini", "provider_model": model, "input_per_million": "1.25", "output_per_million": "10"},
        headers=ADMIN,
    )
    await refresh(app)
    return m, r.json()


def _chat(model="gem", **extra):
    return {"model": model, "messages": [{"role": "user", "content": "bonjour"}], **extra}


async def test_vertex_translation_roundtrip(client, app, tenant):
    await _gemini_model(client, app, **VERTEX)
    r = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gem",
            "max_tokens": 64,
            "temperature": 0.2,
            "stop": ["END"],
            "response_format": {"type": "json_object"},
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "w",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "additionalProperties": False,
                        },
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
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": '{"city": "Lyon"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": '{"temp": 21}'},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "and in Paris?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                    ],
                },
            ],
        },
        headers=tenant["auth"],
    )
    assert r.status_code == 200, r.text
    assert (
        "projects/proj-1/locations/europe-west1/publishers/google/models/gemini-mock:generateContent"
        in STATE["last_url"]
    )
    assert STATE["last_headers"] == {"authorization": "Bearer gemini-test-token"}
    b = STATE["last_body"]
    assert b["systemInstruction"] == {"parts": [{"text": "be brief"}]}
    assert [c["role"] for c in b["contents"]] == ["user", "model", "user", "user"]
    assert b["contents"][1]["parts"] == [{"functionCall": {"name": "get_weather", "args": {"city": "Lyon"}}}]
    assert b["contents"][2]["parts"] == [{"functionResponse": {"name": "get_weather", "response": {"temp": 21}}}]
    assert b["contents"][3]["parts"] == [
        {"text": "and in Paris?"},
        {"inlineData": {"mimeType": "image/png", "data": "QUJD"}},
    ]
    decl = b["tools"][0]["functionDeclarations"][0]
    assert decl["name"] == "get_weather" and "additionalProperties" not in decl["parameters"]
    assert b["toolConfig"] == {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["get_weather"]}}
    assert b["generationConfig"] == {
        "maxOutputTokens": 64,
        "temperature": 0.2,
        "stopSequences": ["END"],
        "responseMimeType": "application/json",
    }
    body = r.json()
    assert (
        body["choices"][0]["message"]["content"] == "Echo: and in Paris?"
        and body["choices"][0]["finish_reason"] == "stop"
    )
    assert body["usage"]["prompt_tokens"] == 11 and body["aigw"]["provider"] == "gemini"
    diag = (await client.get(f"/admin/v1/requests/{body['aigw']['request_id']}", headers=ADMIN)).json()
    assert float(diag["usage"][0]["cost"]) > 0 and diag["usage"][0]["usage_source"] == "reported"


async def test_tool_call_response_and_content_filter(client, app, tenant):
    await _gemini_model(client, app, **VERTEX)
    r = await client.post(
        "/v1/chat/completions",
        json=_chat(
            messages=[{"role": "user", "content": "[tool] weather"}],
            tools=[{"type": "function", "function": {"name": "get_weather"}}],
        ),
        headers=tenant["auth"],
    )
    assert r.status_code == 200, r.text
    msg = r.json()["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"] == {"name": "get_weather", "arguments": '{"city": "Lyon"}'}
    assert r.json()["choices"][0]["finish_reason"] == "tool_calls"
    r = await client.post(
        "/v1/chat/completions",
        json=_chat(messages=[{"role": "user", "content": "[blocked] x"}]),
        headers=tenant["auth"],
    )
    assert r.status_code == 400 and "block" in r.json()["error"]["message"].lower()
    r = await client.post("/v1/chat/completions", json=_chat(logprobs=True), headers=tenant["auth"])
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_parameter"


async def test_streaming(client, app, tenant):
    await _gemini_model(client, app, **VERTEX)
    r = await client.post(
        "/v1/chat/completions", json=_chat(stream=True, stream_options={"include_usage": True}), headers=tenant["auth"]
    )
    assert r.status_code == 200, r.text
    chunks = [
        json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert ":streamGenerateContent" in STATE["last_url"] and "alt=sse" in STATE["last_url"]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices"))
    assert text == "Echo: bonjour"
    assert any(c["choices"] and c["choices"][0]["finish_reason"] == "stop" for c in chunks)
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["prompt_tokens"] == 11
    # streamed tool call arrives as one delta with full arguments
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
    tool_deltas = [c for c in chunks if c.get("choices") and c["choices"][0]["delta"].get("tool_calls")]
    assert tool_deltas[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] == '{"city": "Lyon"}'
    assert any(c["choices"] and c["choices"][0]["finish_reason"] == "tool_calls" for c in chunks)


async def test_google_ai_api_key(client, app, tenant):
    await _gemini_model(client, app, credential="literal:gemini-test-key", api="google_ai")
    r = await client.post("/v1/chat/completions", json=_chat(), headers=tenant["auth"])
    assert r.status_code == 200, r.text
    assert "/v1beta/models/gemini-mock:generateContent" in STATE["last_url"]
    assert STATE["last_headers"] == {"x-goog-api-key": "gemini-test-key"}


async def test_service_account_token_exchange(client, app, tenant):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = (
        key.private_key_bytes
        if False
        else key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
    )
    sa = {
        "type": "service_account",
        "client_email": "aigw@proj-1.iam.gserviceaccount.com",
        "private_key": pem,
        "token_uri": "http://mock/token",
    }
    STATE["token_calls"] = 0
    await _gemini_model(client, app, credential="literal:" + json.dumps(sa), auth="service_account", **VERTEX)
    for _ in range(2):
        r = await client.post("/v1/chat/completions", json=_chat(), headers=tenant["auth"])
        assert r.status_code == 200, r.text
        assert STATE["last_headers"] == {"authorization": "Bearer mock-sa-token"}
    assert STATE["token_calls"] == 1  # cached until shortly before expiry
    claims = jwt.decode(STATE["last_assertion"], key.public_key(), algorithms=["RS256"], audience="http://mock/token")
    assert claims["iss"] == sa["client_email"] and claims["scope"] == "https://www.googleapis.com/auth/cloud-platform"


async def test_embeddings_vertex_and_google_ai(client, app, tenant):
    await _gemini_model(client, app, name="emb-v", model="text-embedding-005", modalities=["embedding"], **VERTEX)
    await _gemini_model(
        client,
        app,
        name="emb-g",
        model="text-embedding-004",
        modalities=["embedding"],
        credential="literal:gemini-test-key",
        api="google_ai",
    )
    r = await client.post(
        "/v1/embeddings", json={"model": "emb-v", "input": ["a", "bb"], "dimensions": 4}, headers=tenant["auth"]
    )
    assert r.status_code == 200, r.text
    assert ":predict" in STATE["last_url"] and STATE["last_body"] == {
        "instances": [{"content": "a"}, {"content": "bb"}],
        "parameters": {"outputDimensionality": 4},
    }
    assert [len(e["embedding"]) for e in r.json()["data"]] == [4, 4] and r.json()["usage"]["prompt_tokens"] == 2
    r = await client.post("/v1/embeddings", json={"model": "emb-g", "input": "x"}, headers=tenant["auth"])
    assert r.status_code == 200, r.text
    assert STATE["last_body"]["requests"][0]["model"] == "models/text-embedding-004"
    assert r.json()["data"][0]["embedding"] == [0.0, 0.2, 0.3]


async def test_missing_project_and_health_probe(client, app, db, valkey, tenant):
    from aigw.gateway.ratelimit import CooldownStore
    from aigw.worker.health import HealthChecker
    from tests.test_health import health_settings

    _, dep = await _gemini_model(client, app, location="us-central1")  # no project
    r = await client.post("/v1/chat/completions", json=_chat(), headers=tenant["auth"])
    assert (r.status_code, r.json()["error"]["code"]) == (503, "gemini_project_required")
    await client.patch(f"/admin/v1/deployments/{dep['id']}", json={"capabilities": VERTEX}, headers=ADMIN)
    await refresh(app)
    hc = HealthChecker(
        db, health_settings(), app.state.adapters, app.state.secrets, CooldownStore(valkey), app.state.upstream
    )
    assert (await hc.run_once())[dep["id"]] == "healthy"
    if valkey is None:
        pytest.skip("no Valkey")

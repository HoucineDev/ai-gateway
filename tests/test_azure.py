"""Azure OpenAI adapter (docs/spec/03 §4.1): deployment addressing, api-version, api-key / Entra auth, health probe."""

from __future__ import annotations

import json

from aigw.testing.mock_upstream import STATE
from tests.conftest import ADMIN, refresh

AZURE = "http://mock"  # the mock serves /openai/deployments/{name}/... like an Azure resource endpoint


async def _azure_model(client, app, name="gpt-azure", deployment="gpt4o-prod", modalities=None, **caps):
    body = {"name": name, "supports_tools": True, "supports_json_schema": True}
    if modalities:
        body["modalities"] = modalities
    model = (await client.post("/admin/v1/models", json=body, headers=ADMIN)).json()
    r = await client.post(
        f"/admin/v1/models/{model['id']}/deployments",
        json={
            "name": f"azure-{deployment}",
            "provider": "azure_openai",
            "provider_model": deployment,
            "base_url": AZURE,
            "credential_ref": "literal:azure-test-key",
            "capabilities": caps,
        },
        headers=ADMIN,
    )
    assert r.status_code == 201, r.text
    await client.post(
        "/admin/v1/prices",
        json={
            "provider": "azure_openai",
            "provider_model": deployment,
            "input_per_million": "2.5",
            "output_per_million": "10",
        },
        headers=ADMIN,
    )
    await refresh(app)
    return model, r.json()


async def test_chat_addressing_auth_and_body(client, app, tenant):
    await _azure_model(client, app)
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-azure", "messages": [{"role": "user", "content": "hello azure"}], "max_tokens": 32},
        headers=tenant["auth"],
    )
    assert r.status_code == 200, r.text
    assert STATE["last_deployment"] == "gpt4o-prod"
    assert STATE["last_query"] == {"api-version": "2024-10-21"}
    assert STATE["last_headers"] == {"api-key": "azure-test-key"}
    assert "model" not in STATE["last_body"] and STATE["last_body"]["max_tokens"] == 32
    body = r.json()
    assert body["model"] == "gpt-azure" and body["aigw"]["provider"] == "azure_openai"
    assert body["choices"][0]["message"]["content"].startswith("Echo:")
    diag = (await client.get(f"/admin/v1/requests/{body['aigw']['request_id']}", headers=ADMIN)).json()
    assert diag["usage"][0]["provider"] == "azure_openai" and float(diag["usage"][0]["cost"]) > 0


async def test_stream_and_embeddings(client, app, tenant):
    await _azure_model(client, app)
    await _azure_model(client, app, name="emb-azure", deployment="text-embedding-3-small", modalities=["embedding"])
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-azure", "stream": True, "messages": [{"role": "user", "content": "stream me"}]},
        headers=tenant["auth"],
    )
    assert r.status_code == 200, r.text
    chunks = [
        json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"
    ]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices"))
    assert text.startswith("Echo:") and STATE["last_deployment"] == "gpt4o-prod"
    r = await client.post("/v1/embeddings", json={"model": "emb-azure", "input": ["a", "b"]}, headers=tenant["auth"])
    assert r.status_code == 200, r.text
    assert len(r.json()["data"]) == 2 and STATE["last_deployment"] == "text-embedding-3-small"
    assert r.json()["aigw"]["provider"] == "azure_openai"


async def test_api_version_bearer_auth_and_max_tokens_field(client, app, tenant):
    await _azure_model(
        client,
        app,
        deployment="o1-prod",
        api_version="2025-01-01-preview",
        auth="bearer",
        max_tokens_field="max_completion_tokens",
    )
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-azure", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 7},
        headers=tenant["auth"],
    )
    assert r.status_code == 200, r.text
    assert STATE["last_query"] == {"api-version": "2025-01-01-preview"}
    assert STATE["last_headers"] == {"authorization": "Bearer azure-test-key"}
    assert STATE["last_body"]["max_completion_tokens"] == 7 and "max_tokens" not in STATE["last_body"]


async def test_missing_base_url_and_unsupported_parameter(client, app, tenant):
    model, dep = await _azure_model(client, app)
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-azure", "messages": [{"role": "user", "content": "hi"}], "top_k": 5},
        headers=tenant["auth"],
    )
    assert r.status_code == 400, r.text  # no provider-extension passthrough on Azure
    await client.patch(f"/admin/v1/deployments/{dep['id']}", json={"base_url": ""}, headers=ADMIN)
    await refresh(app)
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-azure", "messages": [{"role": "user", "content": "hi"}]},
        headers=tenant["auth"],
    )
    assert (r.status_code, r.json()["error"]["code"]) == (503, "azure_base_url_required")


async def test_health_probe_targets_models_endpoint(client, app, db, valkey, tenant):
    from aigw.gateway.ratelimit import CooldownStore
    from aigw.worker.health import HealthChecker
    from tests.test_health import health_settings

    _, dep = await _azure_model(client, app)
    hc = HealthChecker(
        db, health_settings(), app.state.adapters, app.state.secrets, CooldownStore(valkey), app.state.upstream
    )
    statuses = await hc.run_once()
    assert statuses[dep["id"]] == "healthy"
    models = (await client.get("/admin/v1/models", headers=ADMIN)).json()["data"]
    health = {d["name"]: d["health"] for m in models for d in m["deployments"]}
    assert health["azure-gpt4o-prod"]["status"] == "healthy" and health["azure-gpt4o-prod"]["error"] is None

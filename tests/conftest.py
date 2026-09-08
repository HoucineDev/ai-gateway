"""Test harness: real PostgreSQL (aigw_test) + real Valkey/Redis if reachable, mock upstream via ASGI transport."""

from __future__ import annotations

import os

import httpx
import pytest
import pytest_asyncio
import redis.asyncio as redis
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from aigw.config import Settings
from aigw.core.secrets import SecretResolver
from aigw.db.session import Database
from aigw.main import create_app
from aigw.testing.mock_upstream import STATE, mock

TEST_DB = os.environ.get("AIGW_TEST_DATABASE_URL", "postgresql+asyncpg://aigw:aigw@localhost:5432/aigw_test")
TEST_VALKEY = os.environ.get("AIGW_TEST_VALKEY_URL", "redis://localhost:6379/9")
ADMIN = {"x-admin-key": "test-admin"}


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    cfg = Config()
    cfg.set_main_option("script_location", "src/aigw/migrations")
    cfg.set_main_option("sqlalchemy.url", TEST_DB)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture
async def db():
    d = Database(TEST_DB)
    async with d.tx() as s:
        await s.execute(
            text(
                "TRUNCATE usage_events, request_attempts, budgets, virtual_keys, deployments, models, prices, projects, "
                "teams, organizations, audit_events, outbox, invoices RESTART IDENTITY CASCADE"
            )
        )
    yield d
    await d.dispose()


@pytest_asyncio.fixture
async def valkey():
    client = redis.from_url(TEST_VALKEY, socket_connect_timeout=0.5, socket_timeout=0.5)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        yield None
        return
    await client.flushdb()
    yield client
    await client.aclose()


@pytest.fixture
def settings():
    return Settings(
        role="all",
        database_url=TEST_DB,
        valkey_url=TEST_VALKEY,
        admin_key="test-admin",
        config_refresh_seconds=0.2,
        max_attempts=3,
        default_max_output_tokens=256,
    )


@pytest.fixture
def oidc_http_client():
    """HTTP client the OIDC verifier fetches JWKS with; test modules override it with a fake Keycloak ASGI app."""
    return None


@pytest_asyncio.fixture
async def app(db, valkey, settings, oidc_http_client):
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock), base_url="http://mock")
    STATE["calls"] = 0
    application = create_app(
        settings,
        db=db,
        upstream_client=upstream,
        valkey=valkey,
        secrets=SecretResolver({"ANTHROPIC_KEY": "test-anthropic-key", "OPENAI_KEY": "sk-test"}),
        oidc_http_client=oidc_http_client,
    )
    async with application.router.lifespan_context(application):
        yield application
    await upstream.aclose()


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        yield c


async def refresh(app):
    await app.state.snapshots.refresh(force=True)


@pytest_asyncio.fixture
async def tenant(client, app):
    """Admin-provisioned tenant: org/team/project/budget/key + one chat model with a mock deployment + embeddings model."""
    org = (
        await client.post("/admin/v1/organizations", json={"name": "Exaion", "slug": "exaion"}, headers=ADMIN)
    ).json()
    team = (
        await client.post(f"/admin/v1/organizations/{org['id']}/teams", json={"name": "platform"}, headers=ADMIN)
    ).json()
    proj = (
        await client.post(
            f"/admin/v1/teams/{team['id']}/projects",
            json={"name": "prisme", "settings": {"allowed_tags": ["env", "feature"]}},
            headers=ADMIN,
        )
    ).json()
    budget = (
        await client.post(
            "/admin/v1/budgets",
            json={"scope_type": "project", "scope_id": proj["id"], "limit_amount": "10", "soft_alert_pct": 50},
            headers=ADMIN,
        )
    ).json()
    await client.post(
        "/admin/v1/prices",
        json={
            "provider": "openai_compat",
            "provider_model": "mock-chat",
            "input_per_million": "1",
            "output_per_million": "2",
        },
        headers=ADMIN,
    )
    await client.post(
        "/admin/v1/prices",
        json={"provider": "openai_compat", "provider_model": "mock-embed", "input_per_million": "0.1"},
        headers=ADMIN,
    )
    model = (
        await client.post(
            "/admin/v1/models",
            json={"name": "local-chat", "supports_tools": True, "supports_json_schema": True, "context_window": 8192},
            headers=ADMIN,
        )
    ).json()
    dep = (
        await client.post(
            f"/admin/v1/models/{model['id']}/deployments",
            json={
                "name": "mock-primary",
                "provider": "openai_compat",
                "provider_model": "mock-chat",
                "base_url": "http://mock/v1",
                "credential_ref": "none",
            },
            headers=ADMIN,
        )
    ).json()
    emb = (
        await client.post("/admin/v1/models", json={"name": "local-embed", "modalities": ["embedding"]}, headers=ADMIN)
    ).json()
    await client.post(
        f"/admin/v1/models/{emb['id']}/deployments",
        json={
            "name": "mock-embed",
            "provider": "openai_compat",
            "provider_model": "mock-embed",
            "base_url": "http://mock/v1",
        },
        headers=ADMIN,
    )
    key = (await client.post(f"/admin/v1/projects/{proj['id']}/keys", json={"name": "dev"}, headers=ADMIN)).json()
    await refresh(app)
    return {
        "org": org,
        "team": team,
        "project": proj,
        "budget": budget,
        "model": model,
        "deployment": dep,
        "embed_model": emb,
        "key": key,
        "auth": {"authorization": f"Bearer {key['key']}"},
    }

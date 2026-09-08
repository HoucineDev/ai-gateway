"""Keycloak OIDC on the control API (docs/spec/01 §3.1, docs/spec/07 "Keycloak OIDC JWT on control API").

JWKS validation, Keycloak roles → scopes, per-route scope enforcement, audit actor identity.
The first half exercises ``OIDCVerifier`` directly against a fake Keycloak (no database); ``test_api_*`` go through
the control API with the shared fixtures.
"""

from __future__ import annotations

import base64
import json
import time

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.routing import Route

from aigw.admin.auth import ALL_SCOPES, Actor, OIDCVerifier, validate_scope_pattern
from aigw.config import Settings
from aigw.core.errors import GatewayError
from tests.conftest import ADMIN, TEST_DB, TEST_VALKEY

ISSUER = "http://keycloak/realms/aigw"
AUDIENCE = "aigw-portal"
SUBJECT = "6f1a4a6e-3f5e-4b0e-9d5a-1c2b3a4d5e6f"

# k1/k2 are published by the fake Keycloak (k2 only after "rotation"); rogue never is.
_KEYS = {kid: rsa.generate_private_key(public_exponent=65537, key_size=2048) for kid in ("k1", "k2", "rogue")}


def _jwk(kid: str) -> dict:
    d = jwt.algorithms.RSAAlgorithm.to_jwk(_KEYS[kid].public_key(), as_dict=True)
    return {**d, "kid": kid, "use": "sig", "alg": "RS256"}


class FakeKeycloak:
    """Serves the realm JWKS endpoint; counts fetches and can be switched off."""

    def __init__(self, kids=("k1",)):
        self.kids = list(kids)
        self.fetches = 0
        self.down = False
        self.app = FastAPI()
        self.app.get("/realms/aigw/protocol/openid-connect/certs")(self.certs)

    async def certs(self):
        self.fetches += 1
        if self.down:
            return JSONResponse({"error": "down"}, status_code=503)
        return {"keys": [_jwk(k) for k in self.kids]}

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://keycloak")


def mint(realm_roles=(), *, kid="k1", client_roles=None, headers=None, **claims) -> str:
    """Keycloak-shaped access token: realm roles in realm_access, client roles in resource_access, typ=Bearer.
    A flat top-level claim (Keycloak "realm roles" mapper without nesting) is passed as ``roles=...``."""
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": SUBJECT,
        "preferred_username": "alice",
        "iat": now,
        "exp": now + 300,
        "typ": "Bearer",
        "realm_access": {"roles": list(realm_roles)},
    }
    if client_roles is not None:
        payload["resource_access"] = client_roles
    payload.update(claims)
    return jwt.encode(payload, _KEYS[kid], algorithm="RS256", headers={"kid": kid, **(headers or {})})


def _raw_token(header: dict, payload: dict, signature: bytes = b"sig") -> str:
    """Assemble a token PyJWT refuses to produce (alg none / HMAC with an RSA key) to test rejection paths."""
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    return ".".join([b64(json.dumps(header).encode()), b64(json.dumps(payload).encode()), b64(signature)])


def oidc_settings(**overrides) -> Settings:
    base = dict(admin_key="test-admin", oidc_issuer=ISSUER, oidc_audience=AUDIENCE, oidc_jwks_min_refresh_seconds=60)
    return Settings(**{**base, **overrides})


def bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


async def _expect(verifier: OIDCVerifier, token: str, status: int, code: str) -> None:
    with pytest.raises(GatewayError) as ei:
        await verifier.verify(token)
    assert (ei.value.status_code, ei.value.code) == (status, code), ei.value.message


# ---- verifier (no database) ------------------------------------------------


def test_scope_patterns_and_actor_matching():
    for ok in ("*", "*:read", "*:write", "keys:*", "keys:write", "usage:read"):
        assert validate_scope_pattern(ok) == ok
    for bad in ("", "keys", "keyz:write", "keys:delete", "*:*", "read:keys"):
        with pytest.raises(ValueError):
            validate_scope_pattern(bad)
    viewer = Actor("user", "v", frozenset({"*:read"}))
    assert viewer.allows("keys:read") and not viewer.allows("keys:write")
    keys_only = Actor("user", "k", frozenset({"keys:*"}))
    assert keys_only.allows("keys:write") and not keys_only.allows("models:read")
    assert Actor("admin_key", "admin").effective_scopes() == list(ALL_SCOPES)
    assert viewer.effective_scopes() == [s for s in ALL_SCOPES if s.endswith(":read")]


def test_role_scope_config_is_validated_at_startup():
    with pytest.raises(ValueError, match="keyz:write"):
        OIDCVerifier(oidc_settings(oidc_role_scopes={"aigw-admin": ["keyz:write"]}))
    with pytest.raises(ValueError, match="AIGW_OIDC_ISSUER"):
        OIDCVerifier(Settings(admin_key="x"))


async def test_default_role_mapping():
    kc = FakeKeycloak()
    v = OIDCVerifier(oidc_settings(), kc.client())
    admin = await v.verify(mint(["aigw-admin", "offline_access"]))
    assert (admin.type, admin.id) == ("user", "alice")
    assert admin.roles == {"aigw-admin", "offline_access"}
    assert admin.effective_scopes() == list(ALL_SCOPES)

    viewer = await v.verify(mint(["aigw-viewer"]))
    assert viewer.allows("audit:read") and not viewer.allows("prices:write")

    operator = await v.verify(mint(["aigw-operator"]))
    for allowed in ("models:write", "deployments:write", "prices:write", "budgets:write", "keys:read"):
        assert operator.allows(allowed), allowed
    for denied in ("organizations:write", "teams:write", "projects:write", "keys:write"):
        assert not operator.allows(denied), denied

    both = await v.verify(mint(["aigw-viewer", "aigw-operator"]))
    assert both.allows("models:write")
    assert kc.fetches == 1  # keys cached across verifications


async def test_username_falls_back_to_subject():
    v = OIDCVerifier(oidc_settings(), FakeKeycloak().client())
    actor = await v.verify(mint(["aigw-admin"], preferred_username=None))
    assert actor.id == SUBJECT


async def test_unmapped_roles_are_refused():
    v = OIDCVerifier(oidc_settings(), FakeKeycloak().client())
    await _expect(v, mint([]), 403, "missing_role")
    await _expect(v, mint(["default-roles-aigw", "uma_authorization"]), 403, "missing_role")


async def test_client_roles_count_only_for_the_configured_client():
    v = OIDCVerifier(oidc_settings(), FakeKeycloak().client())
    ok = await v.verify(mint(client_roles={AUDIENCE: {"roles": ["aigw-admin"]}}))
    assert ok.allows("keys:write")
    # a role granted on another Keycloak client (e.g. `account`) must not leak in
    await _expect(v, mint(client_roles={"account": {"roles": ["aigw-admin"]}}), 403, "missing_role")

    v2 = OIDCVerifier(oidc_settings(oidc_client_id="aigw-cli"), FakeKeycloak().client())
    ok2 = await v2.verify(mint(client_roles={"aigw-cli": {"roles": ["aigw-viewer"]}}))
    assert ok2.allows("keys:read") and not ok2.allows("keys:write")
    await _expect(v2, mint(client_roles={AUDIENCE: {"roles": ["aigw-viewer"]}}), 403, "missing_role")


async def test_flat_roles_claim_and_custom_mapping():
    settings = oidc_settings(oidc_role_scopes={"gateway-keys": ["keys:*", "projects:read"]})
    v = OIDCVerifier(settings, FakeKeycloak().client())
    actor = await v.verify(mint(roles="gateway-keys"))
    assert actor.effective_scopes() == ["projects:read", "keys:read", "keys:write"]
    await _expect(v, mint(["aigw-admin"]), 403, "missing_role")  # default mapping replaced, not merged


async def test_claim_validation():
    v = OIDCVerifier(oidc_settings(), FakeKeycloak().client())
    now = int(time.time())
    await _expect(v, mint(["aigw-admin"], exp=now - 120), 401, "invalid_token")
    await _expect(v, mint(["aigw-admin"], iss="http://keycloak/realms/other"), 401, "invalid_token")
    await _expect(v, mint(["aigw-admin"], aud="account"), 401, "invalid_token")
    await _expect(v, mint(["aigw-admin"], typ="ID"), 401, "invalid_token")
    await _expect(v, mint(["aigw-admin"], typ="Refresh"), 401, "invalid_token")
    await _expect(v, mint(["aigw-admin"], headers={"typ": "logout+jwt"}), 401, "invalid_token")
    await _expect(v, "not.a.jwt", 401, "invalid_token")
    # multi-valued aud containing ours is fine; leeway tolerates small clock skew
    ok = await v.verify(mint(["aigw-admin"], aud=["account", AUDIENCE], exp=now - 5))
    assert ok.allows("keys:write")


async def test_audience_check_is_optional():
    v = OIDCVerifier(oidc_settings(oidc_audience=None), FakeKeycloak().client())
    actor = await v.verify(mint(["aigw-admin"], aud="account"))
    assert actor.allows("keys:write")
    assert v.client_id is None  # no client id → only realm roles / flat roles are read


async def test_signature_and_algorithm_rejection():
    v = OIDCVerifier(oidc_settings(), FakeKeycloak().client())
    # signed by a key the issuer never published
    await _expect(v, mint(["aigw-admin"], kid="rogue"), 401, "unknown_signing_key")
    # kid of a published key but signed by another private key
    forged = jwt.encode(
        jwt.decode(mint(["aigw-admin"]), options={"verify_signature": False}),
        _KEYS["rogue"],
        "RS256",
        headers={"kid": "k1"},
    )
    await _expect(v, forged, 401, "invalid_token")
    payload = jwt.decode(mint(["aigw-admin"]), options={"verify_signature": False})
    await _expect(v, _raw_token({"alg": "none", "kid": "k1"}, payload, b""), 401, "invalid_token")
    await _expect(v, _raw_token({"alg": "HS256", "kid": "k1"}, payload), 401, "invalid_token")
    await _expect(v, _raw_token({"alg": "RS256"}, payload), 401, "invalid_token")  # no kid


async def test_jwks_rotation_and_refetch_throttle():
    kc = FakeKeycloak(["k1"])
    v = OIDCVerifier(oidc_settings(oidc_jwks_min_refresh_seconds=60), kc.client())
    await v.verify(mint(["aigw-admin"]))
    assert kc.fetches == 1
    # an unknown kid inside the cooldown opened by the last fetch does not hit the IdP
    await _expect(v, mint(["aigw-admin"], kid="k2"), 401, "unknown_signing_key")
    assert kc.fetches == 1
    # cooldown elapsed: exactly one refetch, then garbage kids are throttled again
    v.min_refresh = 0
    await _expect(v, mint(["aigw-admin"], kid="k2"), 401, "unknown_signing_key")
    assert kc.fetches == 2
    v.min_refresh = 60
    for _ in range(5):
        await _expect(v, mint(["aigw-admin"], kid="k2"), 401, "unknown_signing_key")
    assert kc.fetches == 2

    # the realm rotates to k2: once the throttle window passes, the new key is picked up
    kc.kids = ["k1", "k2"]
    v.min_refresh = 0
    actor = await v.verify(mint(["aigw-admin"], kid="k2"))
    assert actor.allows("keys:write") and kc.fetches == 3
    # k1 still cached, no extra fetch
    await v.verify(mint(["aigw-admin"], kid="k1"))
    assert kc.fetches == 3


async def test_jwks_outage_handling():
    kc = FakeKeycloak()
    kc.down = True
    v = OIDCVerifier(oidc_settings(oidc_jwks_min_refresh_seconds=0), kc.client())
    await _expect(v, mint(["aigw-admin"]), 503, "oidc_unavailable")  # nothing cached → cannot authenticate
    kc.down = False
    await v.verify(mint(["aigw-admin"]))
    # cache expired and the IdP is down again: previous keys keep working
    kc.down = True
    v.cache_ttl = 0
    actor = await v.verify(mint(["aigw-admin"]))
    assert actor.allows("keys:write")
    assert kc.fetches == 3


def test_every_admin_route_declares_a_scope():
    """Fail-closed: adding a control-API route without require_scope is a test failure (docs/spec/01 §3.1)."""
    from aigw.admin.routes import router

    missing = []
    for route in router.routes:
        if not isinstance(route, Route) or route.path == "/admin/v1/me":
            continue
        scopes = [getattr(d.call, "scope", None) for d in route.dependant.dependencies]
        if not any(scopes):
            missing.append(f"{sorted(route.methods)} {route.path}")
    assert not missing, f"routes without require_scope: {missing}"


# ---- control API (database) ---------------------------------------------------


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


async def test_api_admin_key_and_bearer_coexist(client):
    r = await client.get("/admin/v1/me", headers=ADMIN)
    assert r.status_code == 200 and r.json() == {
        "actor_type": "admin_key",
        "actor_id": "admin",
        "roles": [],
        "scopes": list(ALL_SCOPES),
        "grants": [],
    }
    r = await client.get("/admin/v1/me", headers=bearer(mint(["aigw-viewer"])))
    assert r.status_code == 200
    assert r.json()["actor_type"] == "user" and r.json()["roles"] == ["aigw-viewer"]
    assert r.json()["scopes"] == [s for s in ALL_SCOPES if s.endswith(":read")]
    # wrong admin key does not fall through to the bearer path; missing credentials still 401
    r = await client.get("/admin/v1/organizations", headers={"x-admin-key": "wrong"})
    assert (r.status_code, r.json()["error"]["code"]) == (401, "invalid_admin_key")
    r = await client.get("/admin/v1/organizations")
    assert (r.status_code, r.json()["error"]["code"]) == (401, "admin_auth_required")


async def test_api_admin_role_writes_and_is_the_audit_actor(client):
    tok = bearer(mint(["aigw-admin"]))
    r = await client.post("/admin/v1/organizations", json={"name": "Exaion", "slug": "exaion"}, headers=tok)
    assert r.status_code == 201, r.text
    org = r.json()
    r = await client.get("/admin/v1/audit", params={"target_type": "organization", "target_id": org["id"]}, headers=tok)
    assert r.status_code == 200
    events = r.json()["data"]
    assert [(e["actor_type"], e["actor_id"], e["action"]) for e in events] == [("user", "alice", "organization.create")]


async def test_api_viewer_is_read_only(client, tenant):
    tok = bearer(mint(["aigw-viewer"]))
    org = tenant["org"]
    for path in (
        "/admin/v1/organizations",
        f"/admin/v1/organizations/{org['id']}/teams",
        f"/admin/v1/projects/{tenant['project']['id']}/keys",
        "/admin/v1/models",
        "/admin/v1/prices",
        f"/admin/v1/budgets?org_id={org['id']}",
        f"/admin/v1/usage?scope_type=project&scope_id={tenant['project']['id']}",
        "/admin/v1/requests",
        "/admin/v1/audit",
        "/admin/v1/config/version",
        "/admin/v1/overview",
    ):
        r = await client.get(path, headers=tok)
        assert r.status_code == 200, (path, r.text)
    denied = [
        ("POST", "/admin/v1/organizations", {"name": "X", "slug": "x"}),
        ("PATCH", f"/admin/v1/organizations/{org['id']}", {"name": "Y"}),
        ("POST", f"/admin/v1/projects/{tenant['project']['id']}/keys", {"name": "k"}),
        ("POST", f"/admin/v1/keys/{tenant['key']['id']}/revoke", None),
        ("POST", "/admin/v1/models", {"name": "m"}),
        ("POST", f"/admin/v1/deployments/{tenant['deployment']['id']}/cooldown", {"seconds": 10}),
        ("POST", "/admin/v1/prices", {"provider": "openai", "provider_model": "x", "input_per_million": "1"}),
        ("PATCH", f"/admin/v1/budgets/{tenant['budget']['id']}", {"limit_amount": "1"}),
    ]
    for method, path, body in denied:
        r = await client.request(method, path, json=body, headers=tok)
        assert (r.status_code, r.json()["error"]["code"]) == (403, "insufficient_scope"), (method, path, r.text)
    # nothing was written and the key is still active
    r = await client.get(f"/admin/v1/keys/{tenant['key']['id']}", headers=tok)
    assert r.json()["status"] == "active"
    r = await client.get("/admin/v1/audit", params={"target_type": "organization"}, headers=ADMIN)
    assert all(e["actor_type"] == "admin_key" for e in r.json()["data"])


async def test_api_operator_manages_catalog_but_not_tenancy(client, tenant):
    tok = bearer(mint(["aigw-operator"]))
    r = await client.post("/admin/v1/models", json={"name": "op-model"}, headers=tok)
    assert r.status_code == 201, r.text
    r = await client.post(
        f"/admin/v1/models/{r.json()['id']}/deployments",
        json={"name": "d", "provider": "openai_compat", "provider_model": "mock-chat", "base_url": "http://mock/v1"},
        headers=tok,
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/admin/v1/prices",
        json={"provider": "openai_compat", "provider_model": "op", "input_per_million": "1"},
        headers=tok,
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        f"/admin/v1/budgets/{tenant['budget']['id']}/temporary-increase",
        json={"amount": "5", "until": "2099-01-01T00:00:00Z"},
        headers=tok,
    )
    assert r.status_code == 200, r.text
    for method, path, body in (
        ("POST", "/admin/v1/organizations", {"name": "X", "slug": "x"}),
        ("POST", f"/admin/v1/organizations/{tenant['org']['id']}/teams", {"name": "t"}),
        ("POST", f"/admin/v1/teams/{tenant['team']['id']}/projects", {"name": "p"}),
        ("POST", f"/admin/v1/projects/{tenant['project']['id']}/keys", {"name": "k"}),
        ("POST", f"/admin/v1/keys/{tenant['key']['id']}/rotate", {}),
    ):
        r = await client.request(method, path, json=body, headers=tok)
        assert (r.status_code, r.json()["error"]["code"]) == (403, "insufficient_scope"), (method, path, r.text)


async def test_api_auth_config_is_public(client):
    r = await client.get("/admin/v1/auth/config")
    assert r.status_code == 200
    assert r.json() == {"admin_key": True, "oidc": {"issuer": ISSUER, "client_id": AUDIENCE, "audience": AUDIENCE}}


async def test_api_token_errors_map_to_envelope(client):
    r = await client.get("/admin/v1/organizations", headers=bearer(mint([])))
    assert (r.status_code, r.json()["error"]["code"]) == (403, "missing_role")
    r = await client.get("/admin/v1/organizations", headers=bearer(mint(["aigw-admin"], exp=int(time.time()) - 600)))
    assert (r.status_code, r.json()["error"]["code"]) == (401, "invalid_token")
    r = await client.get("/admin/v1/organizations", headers=bearer(mint(["aigw-admin"], kid="rogue")))
    assert (r.status_code, r.json()["error"]["code"]) == (401, "unknown_signing_key")


async def test_api_bearer_refused_when_oidc_not_configured(db, valkey):
    """Without AIGW_OIDC_ISSUER a bearer token is an authentication failure, never silently accepted."""
    from aigw.core.secrets import SecretResolver
    from aigw.main import create_app

    app = create_app(
        Settings(role="admin", database_url=TEST_DB, valkey_url=TEST_VALKEY, admin_key="test-admin"),
        db=db,
        valkey=valkey,
        secrets=SecretResolver({}),
    )
    async with app.router.lifespan_context(app):
        assert app.state.oidc is None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
            assert (await c.get("/admin/v1/auth/config")).json() == {"admin_key": True, "oidc": None}
            r = await c.get("/admin/v1/organizations", headers=bearer(mint(["aigw-admin"])))
            assert (r.status_code, r.json()["error"]["code"]) == (401, "oidc_not_configured")
            r = await c.get("/admin/v1/organizations", headers=ADMIN)
            assert r.status_code == 200

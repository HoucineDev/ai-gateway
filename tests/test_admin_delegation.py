"""Delegated tenant roles (docs/spec/01 §3.2): role bindings, tenant-scoped authorization, list filtering.

World: two organizations. Inside org 1 (the `tenant` fixture) bob is org_owner, carol is team_owner of team 1 and
dan is project_member of project 1. Org 2 has its own team/project/key and must stay invisible to all three.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from aigw.config import Settings
from tests.conftest import ADMIN, TEST_DB, TEST_VALKEY
from tests.test_admin_oidc import AUDIENCE, ISSUER, FakeKeycloak, bearer, mint


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


def tok(user: str, roles=(), **claims) -> dict:
    """Bearer for a realm user with no global role unless given; sub/email derive from the username."""
    return bearer(mint(roles, preferred_username=user, sub=f"sub-{user}", email=f"{user}@example.com", **claims))


async def bind(client, subject, role, scope_type, scope_id, headers=ADMIN, **extra):
    return await client.post(
        "/admin/v1/role-bindings",
        json={"subject": subject, "role": role, "scope_type": scope_type, "scope_id": scope_id, **extra},
        headers=headers,
    )


def err(r) -> tuple[int, str]:
    return r.status_code, r.json()["error"]["code"]


@pytest_asyncio.fixture
async def world(client, tenant):
    org2 = (await client.post("/admin/v1/organizations", json={"name": "Other", "slug": "other"}, headers=ADMIN)).json()
    team2 = (
        await client.post(f"/admin/v1/organizations/{org2['id']}/teams", json={"name": "t2"}, headers=ADMIN)
    ).json()
    project2 = (await client.post(f"/admin/v1/teams/{team2['id']}/projects", json={"name": "p2"}, headers=ADMIN)).json()
    key2 = (await client.post(f"/admin/v1/projects/{project2['id']}/keys", json={"name": "k2"}, headers=ADMIN)).json()
    budget2 = (
        await client.post(
            "/admin/v1/budgets",
            json={"scope_type": "project", "scope_id": project2["id"], "limit_amount": "5"},
            headers=ADMIN,
        )
    ).json()
    bindings = {}
    for user, role, scope_type, scope_id in (
        ("bob", "org_owner", "organization", tenant["org"]["id"]),
        ("carol", "team_owner", "team", tenant["team"]["id"]),
        ("dan", "project_member", "project", tenant["project"]["id"]),
    ):
        r = await bind(client, user, role, scope_type, scope_id)
        assert r.status_code == 201, r.text
        bindings[user] = r.json()
    return {
        **tenant,
        "org2": org2,
        "team2": team2,
        "project2": project2,
        "key2": key2,
        "budget2": budget2,
        "bindings": bindings,
    }


# ---- subjects and /me -------------------------------------------------------


async def test_binding_subject_matches_sub_username_or_email(client, tenant):
    pid = tenant["project"]["id"]
    assert (await bind(client, "erin@example.com", "project_member", "project", pid)).status_code == 201
    assert (await bind(client, "sub-frank", "project_member", "project", pid)).status_code == 201
    for user in ("erin", "frank"):
        r = await client.get("/admin/v1/me", headers=tok(user))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["roles"] == [] and [g["role"] for g in body["grants"]] == ["project_member"]
        assert body["grants"][0]["project_id"] == pid and "keys:write" in body["scopes"]
        assert "projects:write" not in body["scopes"]
    # nobody: no global role, no binding
    assert err(await client.get("/admin/v1/me", headers=tok("zed"))) == (403, "missing_role")


async def test_binding_validation(client, tenant):
    pid, oid = tenant["project"]["id"], tenant["org"]["id"]
    assert err(await bind(client, "x", "org_owner", "project", pid)) == (400, "role_scope_mismatch")
    assert err(await bind(client, "x", "project_member", "project", oid)) == (404, "project_not_found")
    assert (await bind(client, "x", "project_member", "project", pid)).status_code == 201
    assert err(await bind(client, "x", "project_member", "project", pid)) == (400, "conflict")
    r = await client.get("/admin/v1/role-bindings", params={"subject": "x"}, headers=ADMIN)
    assert [b["role"] for b in r.json()["data"]] == ["project_member"]


# ---- org owner ----------------------------------------------------------------


async def test_org_owner_is_confined_to_its_organization(client, world):
    h = tok("bob")
    org1, org2 = world["org"], world["org2"]
    r = await client.get("/admin/v1/organizations", headers=h)
    assert [o["id"] for o in r.json()["data"]] == [org1["id"]]
    assert (await client.get(f"/admin/v1/organizations/{org1['id']}", headers=h)).status_code == 200
    assert err(await client.get(f"/admin/v1/organizations/{org2['id']}", headers=h)) == (403, "tenant_forbidden")
    assert err(
        await client.post("/admin/v1/organizations", json={"name": "New org", "slug": "new-org"}, headers=h)
    ) == (
        403,
        "tenant_forbidden",
    )
    assert (
        await client.patch(f"/admin/v1/organizations/{org1['id']}", json={"name": "Exaion SA"}, headers=h)
    ).status_code == 200
    assert err(await client.patch(f"/admin/v1/organizations/{org2['id']}", json={"name": "X"}, headers=h)) == (
        403,
        "tenant_forbidden",
    )

    assert (
        await client.post(f"/admin/v1/organizations/{org1['id']}/teams", json={"name": "ops"}, headers=h)
    ).status_code == 201
    assert err(await client.post(f"/admin/v1/organizations/{org2['id']}/teams", json={"name": "ops"}, headers=h)) == (
        403,
        "tenant_forbidden",
    )
    r = await client.post(f"/admin/v1/projects/{world['project']['id']}/keys", json={"name": "bob-key"}, headers=h)
    assert r.status_code == 201, r.text
    assert err(
        await client.post(f"/admin/v1/projects/{world['project2']['id']}/keys", json={"name": "k"}, headers=h)
    ) == (403, "tenant_forbidden")
    assert err(await client.get(f"/admin/v1/keys/{world['key2']['id']}", headers=h)) == (403, "tenant_forbidden")

    # catalog: org-scoped models are theirs, the global catalog is read-only
    r = await client.post("/admin/v1/models", json={"name": "org1-model", "org_id": org1["id"]}, headers=h)
    assert r.status_code == 201, r.text
    assert err(await client.post("/admin/v1/models", json={"name": "global-model"}, headers=h)) == (
        403,
        "tenant_forbidden",
    )
    assert err(await client.post("/admin/v1/models", json={"name": "m", "org_id": org2["id"]}, headers=h)) == (
        403,
        "tenant_forbidden",
    )
    assert err(
        await client.patch(f"/admin/v1/models/{world['model']['id']}", json={"display_name": "x"}, headers=h)
    ) == (403, "tenant_forbidden")
    names = {m["name"] for m in (await client.get("/admin/v1/models", headers=h)).json()["data"]}
    assert {"org1-model", "local-chat", "local-embed"} <= names
    assert err(
        await client.post("/admin/v1/prices", json={"provider": "openai", "provider_model": "x"}, headers=h)
    ) == (403, "insufficient_scope")

    # budgets, audit and usage stay inside org 1
    budgets = (await client.get("/admin/v1/budgets", headers=h)).json()["data"]
    assert {b["org_id"] for b in budgets} == {org1["id"]}
    audit = (await client.get("/admin/v1/audit", headers=h)).json()["data"]
    assert audit and {a["org_id"] for a in audit} == {org1["id"]}
    assert (
        await client.get("/admin/v1/usage", params={"scope_type": "organization", "scope_id": org1["id"]}, headers=h)
    ).status_code == 200
    assert err(
        await client.get("/admin/v1/usage", params={"scope_type": "organization", "scope_id": org2["id"]}, headers=h)
    ) == (403, "tenant_forbidden")
    assert (await client.get("/admin/v1/overview", headers=h)).status_code == 200

    # audit rows carry the delegate's identity
    r = await client.get("/admin/v1/audit", params={"target_type": "team", "org_id": org1["id"]}, headers=ADMIN)
    assert ("user", "bob") in {(a["actor_type"], a["actor_id"]) for a in r.json()["data"]}


async def test_org_owner_delegates_but_cannot_escalate_outside(client, world):
    h = tok("bob")
    org1, org2 = world["org"], world["org2"]
    assert (await bind(client, "gina", "team_owner", "team", world["team"]["id"], headers=h)).status_code == 201
    assert (await bind(client, "hal", "org_owner", "organization", org1["id"], headers=h)).status_code == 201
    assert err(await bind(client, "ivy", "org_owner", "organization", org2["id"], headers=h)) == (
        403,
        "tenant_forbidden",
    )
    assert err(await bind(client, "ivy", "project_member", "project", world["project2"]["id"], headers=h)) == (
        403,
        "tenant_forbidden",
    )
    rows = (await client.get("/admin/v1/role-bindings", headers=h)).json()["data"]
    assert {b["subject"] for b in rows} == {"bob", "carol", "dan", "gina", "hal"}  # nothing from org 2
    # gina now works as team owner; revoking through bob is immediate
    assert (
        await client.post(f"/admin/v1/teams/{world['team']['id']}/projects", json={"name": "g"}, headers=tok("gina"))
    ).status_code == 201
    gina = next(b for b in rows if b["subject"] == "gina")
    assert (await client.post(f"/admin/v1/role-bindings/{gina['id']}/revoke", headers=h)).status_code == 200
    assert err(await client.get("/admin/v1/me", headers=tok("gina"))) == (403, "missing_role")


# ---- team owner ---------------------------------------------------------------


async def test_team_owner_scope(client, world):
    h = tok("carol")
    org1, team1, project1 = world["org"], world["team"], world["project"]
    assert (await client.get(f"/admin/v1/organizations/{org1['id']}", headers=h)).status_code == 200
    assert [o["id"] for o in (await client.get("/admin/v1/organizations", headers=h)).json()["data"]] == [org1["id"]]
    teams = (await client.get(f"/admin/v1/organizations/{org1['id']}/teams", headers=h)).json()["data"]
    assert [t["id"] for t in teams] == [team1["id"]]
    assert err(await client.post(f"/admin/v1/organizations/{org1['id']}/teams", json={"name": "x"}, headers=h)) == (
        403,
        "tenant_forbidden",
    )
    assert err(await client.patch(f"/admin/v1/organizations/{org1['id']}", json={"name": "x"}, headers=h)) == (
        403,
        "insufficient_scope",
    )

    assert (
        await client.post(f"/admin/v1/teams/{team1['id']}/projects", json={"name": "carol-p"}, headers=h)
    ).status_code == 201
    assert err(await client.get(f"/admin/v1/teams/{world['team2']['id']}/projects", headers=h)) == (
        403,
        "tenant_forbidden",
    )
    assert (
        await client.post(f"/admin/v1/projects/{project1['id']}/keys", json={"name": "ck"}, headers=h)
    ).status_code == 201
    assert err(
        await client.post(f"/admin/v1/projects/{world['project2']['id']}/keys", json={"name": "ck"}, headers=h)
    ) == (403, "tenant_forbidden")

    # budgets: project/team level yes, organization level no
    r = await client.post(
        "/admin/v1/budgets", json={"scope_type": "team", "scope_id": team1["id"], "limit_amount": "3"}, headers=h
    )
    assert r.status_code == 201, r.text
    assert err(
        await client.post(
            "/admin/v1/budgets",
            json={"scope_type": "organization", "scope_id": org1["id"], "limit_amount": "3"},
            headers=h,
        )
    ) == (403, "tenant_forbidden")
    ids = {b["id"] for b in (await client.get("/admin/v1/budgets", headers=h)).json()["data"]}
    assert world["budget"]["id"] in ids and r.json()["id"] in ids and world["budget2"]["id"] not in ids
    assert (
        await client.patch(f"/admin/v1/budgets/{world['budget']['id']}", json={"limit_amount": "12"}, headers=h)
    ).status_code == 200
    assert err(
        await client.patch(f"/admin/v1/budgets/{world['budget2']['id']}", json={"limit_amount": "1"}, headers=h)
    ) == (403, "tenant_forbidden")

    # delegation rank: project members and team owners inside the team, never org owners
    assert (await bind(client, "jo", "project_member", "project", project1["id"], headers=h)).status_code == 201
    assert (await bind(client, "kim", "team_owner", "team", team1["id"], headers=h)).status_code == 201
    assert err(await bind(client, "lee", "org_owner", "organization", org1["id"], headers=h)) == (
        403,
        "tenant_forbidden",
    )
    assert "audit:read" not in (await client.get("/admin/v1/me", headers=h)).json()["scopes"]
    assert err(await client.get("/admin/v1/audit", headers=h)) == (403, "insufficient_scope")


# ---- project member -----------------------------------------------------------


async def test_project_member_scope(client, world):
    h = tok("dan")
    org1, team1, project1 = world["org"], world["team"], world["project"]
    me = (await client.get("/admin/v1/me", headers=h)).json()
    assert me["global_scopes"] == [] and "keys:write" in me["scopes"]
    assert me["grants"] == [
        {
            "role": "project_member",
            "scope_type": "project",
            "scope_id": project1["id"],
            "org_id": org1["id"],
            "team_id": team1["id"],
            "project_id": project1["id"],
        }
    ]
    # own rows up the tree are readable, siblings are not
    assert [o["id"] for o in (await client.get("/admin/v1/organizations", headers=h)).json()["data"]] == [org1["id"]]
    assert [
        t["id"] for t in (await client.get(f"/admin/v1/organizations/{org1['id']}/teams", headers=h)).json()["data"]
    ] == [team1["id"]]
    assert (await client.get(f"/admin/v1/teams/{team1['id']}", headers=h)).status_code == 200
    assert [
        p["id"] for p in (await client.get(f"/admin/v1/teams/{team1['id']}/projects", headers=h)).json()["data"]
    ] == [project1["id"]]
    assert err(await client.get(f"/admin/v1/teams/{world['team2']['id']}", headers=h)) == (403, "tenant_forbidden")

    # keys in the project: full lifecycle; anything else is read-only or denied
    r = await client.post(f"/admin/v1/projects/{project1['id']}/keys", json={"name": "dan-key"}, headers=h)
    assert r.status_code == 201, r.text
    assert (await client.post(f"/admin/v1/keys/{r.json()['id']}/revoke", headers=h)).status_code == 200
    assert err(await client.get(f"/admin/v1/projects/{world['project2']['id']}/keys", headers=h)) == (
        403,
        "tenant_forbidden",
    )
    assert err(await client.post(f"/admin/v1/teams/{team1['id']}/projects", json={"name": "x"}, headers=h)) == (
        403,
        "insufficient_scope",
    )
    assert err(
        await client.post(
            "/admin/v1/budgets",
            json={"scope_type": "project", "scope_id": project1["id"], "limit_amount": "1"},
            headers=h,
        )
    ) == (403, "insufficient_scope")
    assert [b["id"] for b in (await client.get("/admin/v1/budgets", headers=h)).json()["data"]] == [
        world["budget"]["id"]
    ]
    assert (
        await client.get("/admin/v1/usage", params={"scope_type": "project", "scope_id": project1["id"]}, headers=h)
    ).status_code == 200
    assert err(
        await client.get(
            "/admin/v1/usage", params={"scope_type": "project", "scope_id": world["project2"]["id"]}, headers=h
        )
    ) == (403, "tenant_forbidden")
    assert (await client.get("/admin/v1/requests", headers=h)).status_code == 200
    assert (await client.get("/admin/v1/overview", headers=h)).status_code == 200
    # can see who else is on the project, cannot add anyone
    subjects = {b["subject"] for b in (await client.get("/admin/v1/role-bindings", headers=h)).json()["data"]}
    assert subjects == {"dan"}
    assert err(await bind(client, "eve", "project_member", "project", project1["id"], headers=h)) == (
        403,
        "insufficient_scope",
    )


# ---- mixing global and delegated ------------------------------------------------


async def test_global_viewer_with_project_binding(client, world):
    h = tok("viv", roles=["aigw-viewer"])
    assert (await bind(client, "viv", "project_member", "project", world["project"]["id"])).status_code == 201
    # global read everywhere, including org 2
    assert (await client.get(f"/admin/v1/organizations/{world['org2']['id']}", headers=h)).status_code == 200
    assert len((await client.get("/admin/v1/organizations", headers=h)).json()["data"]) == 2
    # writes only inside the bound project
    assert (
        await client.post(f"/admin/v1/projects/{world['project']['id']}/keys", json={"name": "v"}, headers=h)
    ).status_code == 201
    assert err(
        await client.post(f"/admin/v1/projects/{world['project2']['id']}/keys", json={"name": "v"}, headers=h)
    ) == (403, "tenant_forbidden")


async def test_revoked_binding_can_be_reactivated(client, world):
    bob = world["bindings"]["bob"]
    assert (await client.post(f"/admin/v1/role-bindings/{bob['id']}/revoke", headers=ADMIN)).status_code == 200
    assert err(await client.get("/admin/v1/organizations", headers=tok("bob"))) == (403, "missing_role")
    r = await bind(client, "bob", "org_owner", "organization", world["org"]["id"])
    assert r.status_code == 201 and r.json()["id"] == bob["id"] and r.json()["status"] == "active"
    assert (await client.get("/admin/v1/organizations", headers=tok("bob"))).status_code == 200
    revoked = (
        await client.get("/admin/v1/role-bindings", params={"subject": "bob", "include_revoked": "true"}, headers=ADMIN)
    ).json()["data"]
    assert len(revoked) == 1  # same row, re-activated, so the audit trail stays continuous

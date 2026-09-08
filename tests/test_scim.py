"""SCIM 2.0 provisioning (docs/spec/01 §3.4): discovery, Users CRUD/filter/patch, Groups → role bindings,
deactivation lockout, deletion revocation, token auth, audit."""

from __future__ import annotations

import pytest
import pytest_asyncio

from aigw.config import Settings
from tests.conftest import ADMIN, TEST_DB, TEST_VALKEY
from tests.test_admin_delegation import tok
from tests.test_admin_oidc import AUDIENCE, ISSUER, FakeKeycloak

SCIM = {"authorization": "Bearer scim-test-token", "content-type": "application/scim+json"}
USER = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


async def _user(client, name, **extra):
    r = await client.post("/scim/v2/Users", json={"schemas": [USER], "userName": name, **extra}, headers=SCIM)
    assert r.status_code == 201, r.text
    return r.json()


async def _group(client, name, members=()):
    r = await client.post(
        "/scim/v2/Groups",
        json={"schemas": [GROUP], "displayName": name, "members": [{"value": m} for m in members]},
        headers=SCIM,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _bindings(client, subject):
    r = await client.get("/admin/v1/role-bindings", params={"subject": subject}, headers=ADMIN)
    return [(b["role"], b["scope_type"], b["scope_id"]) for b in r.json()["data"]]


async def test_auth_and_discovery(client, app):
    r = await client.get("/scim/v2/ServiceProviderConfig")
    assert r.status_code == 401 and r.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert (await client.get("/scim/v2/Users", headers={"authorization": "Bearer wrong"})).status_code == 401
    r = await client.get("/scim/v2/ServiceProviderConfig", headers=SCIM)
    assert r.status_code == 200 and r.json()["patch"]["supported"] is True
    assert r.headers["content-type"].startswith("application/scim+json")
    assert [t["id"] for t in (await client.get("/scim/v2/ResourceTypes", headers=SCIM)).json()["Resources"]] == [
        "User",
        "Group",
    ]
    assert len((await client.get("/scim/v2/Schemas", headers=SCIM)).json()["Resources"]) == 2
    app.state.settings.scim_token = None
    try:
        assert (await client.get("/scim/v2/Users", headers=SCIM)).status_code == 503
    finally:
        app.state.settings.scim_token = "scim-test-token"


async def test_users_crud_filter_and_patch(client, app):
    u = await _user(
        client,
        "carol",
        externalId="idp-1",
        displayName="Carol V",
        emails=[{"value": "Carol@Example.com", "primary": True}],
    )
    assert u["active"] is True and u["emails"] == [{"value": "carol@example.com", "primary": True, "type": None}]
    assert u["meta"]["resourceType"] == "User" and u["meta"]["location"].endswith(f"/scim/v2/Users/{u['id']}")
    assert (await client.post("/scim/v2/Users", json={"userName": "CAROL"}, headers=SCIM)).status_code == 409
    assert (await client.post("/scim/v2/Users", json={"displayName": "x"}, headers=SCIM)).status_code == 400
    lst = (await client.get("/scim/v2/Users", params={"filter": 'userName eq "Carol"'}, headers=SCIM)).json()
    assert lst["totalResults"] == 1 and lst["Resources"][0]["id"] == u["id"] and lst["startIndex"] == 1
    assert (
        await client.get("/scim/v2/Users", params={"filter": 'emails.value eq "carol@example.com"'}, headers=SCIM)
    ).json()["totalResults"] == 1
    assert (await client.get("/scim/v2/Users", params={"filter": 'externalId eq "idp-1"'}, headers=SCIM)).json()[
        "totalResults"
    ] == 1
    assert (await client.get("/scim/v2/Users", params={"filter": 'userName co "car"'}, headers=SCIM)).status_code == 400
    r = await client.patch(
        f"/scim/v2/Users/{u['id']}",
        json={
            "schemas": [PATCH],
            "Operations": [
                {"op": "replace", "path": "displayName", "value": "Carol Vega"},
                {"op": "Replace", "value": {"active": False}},
            ],
        },
        headers=SCIM,
    )
    assert r.status_code == 200 and r.json()["displayName"] == "Carol Vega" and r.json()["active"] is False
    r = await client.put(
        f"/scim/v2/Users/{u['id']}", json={"schemas": [USER], "userName": "carol", "active": True}, headers=SCIM
    )
    assert r.json()["active"] is True and r.json().get("displayName") is None and r.json()["emails"] == []
    assert (await client.get("/scim/v2/Users/00000000-0000-0000-0000-000000000000", headers=SCIM)).status_code == 404
    assert (await client.delete(f"/scim/v2/Users/{u['id']}", headers=SCIM)).status_code == 204
    assert (await client.get(f"/scim/v2/Users/{u['id']}", headers=SCIM)).status_code == 404
    actions = [
        a["action"]
        for a in (await client.get("/admin/v1/audit", params={"target_type": "scim_user"}, headers=ADMIN)).json()[
            "data"
        ]
    ]
    assert "scim.user.create" in actions and "scim.user.delete" in actions and "scim.user.deactivate" in actions
    assert all(
        a["actor_type"] == "scim"
        for a in (await client.get("/admin/v1/audit", params={"target_type": "scim_user"}, headers=ADMIN)).json()[
            "data"
        ]
    )


async def test_groups_map_to_role_bindings(client, app, tenant):
    dan = await _user(client, "dan")
    eve = await _user(client, "eve")
    pid, tid, oid = tenant["project"]["id"], tenant["team"]["id"], tenant["org"]["id"]
    # plain group: stored, no access effect
    plain = await _group(client, "engineering", [dan["id"]])
    assert "urn:aigw:scim:mapping" not in plain and await _bindings(client, "dan") == []
    # mapped group grants on creation
    g = await _group(client, f"aigw:project_member:project:{pid}", [dan["id"]])
    assert g["urn:aigw:scim:mapping"]["role"] == "project_member" and [m["display"] for m in g["members"]] == ["dan"]
    assert await _bindings(client, "dan") == [("project_member", "project", pid)]
    me = (await client.get("/admin/v1/me", headers=tok("dan"))).json()
    assert [gr["role"] for gr in me["grants"]] == ["project_member"]
    # add / remove members through PATCH
    r = await client.patch(
        f"/scim/v2/Groups/{g['id']}",
        json={"schemas": [PATCH], "Operations": [{"op": "add", "path": "members", "value": [{"value": eve["id"]}]}]},
        headers=SCIM,
    )
    assert r.status_code == 200 and sorted(m["display"] for m in r.json()["members"]) == ["dan", "eve"]
    assert await _bindings(client, "eve") == [("project_member", "project", pid)]
    r = await client.patch(
        f"/scim/v2/Groups/{g['id']}",
        json={"schemas": [PATCH], "Operations": [{"op": "remove", "path": f'members[value eq "{dan["id"]}"]'}]},
        headers=SCIM,
    )
    assert [m["display"] for m in r.json()["members"]] == ["eve"] and await _bindings(client, "dan") == []
    assert (await client.get("/admin/v1/me", headers=tok("dan"))).status_code == 403  # no role left
    # renaming the group to another target moves every member
    r = await client.patch(
        f"/scim/v2/Groups/{g['id']}",
        json={
            "schemas": [PATCH],
            "Operations": [{"op": "replace", "path": "displayName", "value": f"aigw:team_owner:team:{tid}"}],
        },
        headers=SCIM,
    )
    assert r.status_code == 200, r.text
    assert await _bindings(client, "eve") == [("team_owner", "team", tid)]
    # org slug form and validation
    org_group = await _group(client, f"aigw:org_owner:organization:{tenant['org']['slug']}", [dan["id"]])
    assert org_group["urn:aigw:scim:mapping"]["scope_id"] == oid and await _bindings(client, "dan") == [
        ("org_owner", "organization", oid)
    ]
    r = await client.post("/scim/v2/Groups", json={"displayName": "aigw:org_owner:project:" + pid}, headers=SCIM)
    assert r.status_code == 400 and r.json()["scimType"] == "invalidValue"
    r = await client.post(
        "/scim/v2/Groups",
        json={"displayName": "aigw:project_member:project:00000000-0000-0000-0000-000000000000"},
        headers=SCIM,
    )
    assert r.status_code == 400
    # PUT replaces the member set; DELETE revokes everything the group granted
    r = await client.put(
        f"/scim/v2/Groups/{g['id']}",
        json={"schemas": [GROUP], "displayName": f"aigw:team_owner:team:{tid}", "members": [{"value": dan["id"]}]},
        headers=SCIM,
    )
    assert [m["display"] for m in r.json()["members"]] == ["dan"] and await _bindings(client, "eve") == []
    assert (await client.delete(f"/scim/v2/Groups/{g['id']}", headers=SCIM)).status_code == 204
    assert await _bindings(client, "dan") == [("org_owner", "organization", oid)]
    lst = (await client.get("/scim/v2/Groups", params={"filter": 'displayName eq "engineering"'}, headers=SCIM)).json()
    assert lst["totalResults"] == 1 and [m["display"] for m in lst["Resources"][0]["members"]] == ["dan"]


async def test_deactivation_locks_out_and_deletion_revokes(client, app, tenant):
    pid = tenant["project"]["id"]
    dan = await _user(client, "dan", emails=[{"value": "dan@example.com"}])
    await _group(client, f"aigw:project_member:project:{pid}", [dan["id"]])
    assert (await client.get("/admin/v1/me", headers=tok("dan"))).status_code == 200
    r = await client.patch(
        f"/scim/v2/Users/{dan['id']}",
        json={"schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": False}]},
        headers=SCIM,
    )
    assert r.status_code == 200 and r.json()["active"] is False
    r = await client.get("/admin/v1/me", headers=tok("dan"))
    assert (r.status_code, r.json()["error"]["code"]) == (403, "user_deactivated")
    # a global-role token for the same person is locked out as well (identity, not scope)
    r = await client.get("/admin/v1/organizations", headers=tok("dan", roles=["aigw-admin"]))
    assert (r.status_code, r.json()["error"]["code"]) == (403, "user_deactivated")
    await client.patch(
        f"/scim/v2/Users/{dan['id']}",
        json={"schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": "True"}]},
        headers=SCIM,
    )
    assert (await client.get("/admin/v1/me", headers=tok("dan"))).status_code == 200
    assert (await client.delete(f"/scim/v2/Users/{dan['id']}", headers=SCIM)).status_code == 204
    assert await _bindings(client, "dan") == []
    assert (await client.get("/admin/v1/me", headers=tok("dan"))).status_code == 403  # missing_role now


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
        scim_token="scim-test-token",
        config_refresh_seconds=0.2,
        default_max_output_tokens=256,
    )

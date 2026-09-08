"""SCIM 2.0 provisioning (RFC 7643/7644 core) on the admin role (docs/spec/01 §3.4).

An identity provider (Keycloak, Entra ID, Okta …) pushes **Users** and **Groups** to ``/scim/v2`` with a bearer
token (``AIGW_SCIM_TOKEN``). Users are the subjects delegated roles are bound to: deactivating (``active: false``)
or deleting a user locks its Keycloak identity out of the control API (403 ``user_deactivated``) and deleting also
revokes its role bindings. Groups whose ``displayName`` follows ``aigw:<role>:<scope_type>:<id>`` (for example
``aigw:project_member:project:<uuid>`` or ``aigw:org_owner:organization:<slug>``) are mapped to delegated roles:
membership creates the role binding for the member's ``userName``, removal revokes it. Other groups are stored as
plain groups. Every write is audited with actor ``scim``.
"""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select

from aigw.admin.auth import ROLE_RANK, Actor
from aigw.admin.service import audit
from aigw.db.models import Organization, Project, RoleBinding, ScimGroup, ScimGroupMember, ScimUser, Team

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_ACTOR = Actor("scim", "scim")
_GROUP_MAP = re.compile(
    r"^aigw:(?P<role>org_owner|team_owner|project_member):(?P<scope>organization|team|project):(?P<id>[^:]+)$"
)
_FILTER = re.compile(r'^\s*(?P<attr>[A-Za-z.]+)\s+eq\s+"(?P<value>[^"]*)"\s*$', re.IGNORECASE)

router = APIRouter(prefix="/scim/v2")


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str | None = None):
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type

    def response(self) -> JSONResponse:
        body: dict[str, Any] = {"schemas": [ERROR_SCHEMA], "status": str(self.status), "detail": self.detail}
        if self.scim_type:
            body["scimType"] = self.scim_type
        return JSONResponse(body, status_code=self.status, media_type="application/scim+json")


async def require_scim(request: Request, authorization: str | None = Header(default=None)) -> None:
    token = request.app.state.settings.scim_token
    if not token:
        raise ScimError(503, "SCIM provisioning is not configured (AIGW_SCIM_TOKEN)")
    if not authorization or authorization[:7].lower() != "bearer ":
        raise ScimError(401, "Bearer token required")
    if not secrets.compare_digest(authorization[7:].strip().encode(), token.encode()):
        raise ScimError(401, "Invalid SCIM token")


def _db(request: Request):
    return request.app.state.db


def _meta(kind: str, obj, request: Request) -> dict:
    return {
        "resourceType": kind,
        "created": obj.created_at.isoformat(),
        "lastModified": (obj.updated_at or obj.created_at).isoformat(),
        "location": str(request.url_for(f"scim_get_{kind.lower()}", id=str(obj.id))),
    }


def user_view(u: ScimUser, request: Request, groups: list[ScimGroup] | None = None) -> dict:
    out = {
        "schemas": [USER_SCHEMA],
        "id": str(u.id),
        "externalId": u.external_id,
        "userName": u.user_name,
        "displayName": u.display_name,
        "emails": u.emails or [],
        "active": u.active,
        "groups": [{"value": str(g.id), "display": g.display_name} for g in (groups or [])],
        "meta": _meta("User", u, request),
    }
    return {k: v for k, v in out.items() if v is not None}


def group_view(g: ScimGroup, members: list[ScimUser], request: Request) -> dict:
    out = {
        "schemas": [GROUP_SCHEMA],
        "id": str(g.id),
        "externalId": g.external_id,
        "displayName": g.display_name,
        "members": [{"value": str(m.id), "display": m.user_name} for m in members],
        "meta": _meta("Group", g, request),
    }
    if g.role:
        out["urn:aigw:scim:mapping"] = {"role": g.role, "scope_type": g.scope_type, "scope_id": str(g.scope_id)}
    return {k: v for k, v in out.items() if v is not None}


def _parse_filter(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    m = _FILTER.match(value)
    if not m:
        raise ScimError(400, f'unsupported filter {value!r} (only `attr eq "value"`)', "invalidFilter")
    return m.group("attr").lower(), m.group("value")


def _uuid(value: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ScimError(404, f"{what} {value} not found") from None


async def _resolve_mapping(s, display_name: str) -> tuple[str | None, str | None, uuid.UUID | None]:
    m = _GROUP_MAP.match(display_name or "")
    if not m:
        return None, None, None
    role, scope_type, ident = m.group("role"), m.group("scope"), m.group("id")
    expected = {"org_owner": "organization", "team_owner": "team", "project_member": "project"}[role]
    if scope_type != expected:
        raise ScimError(400, f"role {role} binds to a {expected}, not a {scope_type}", "invalidValue")
    model = {"organization": Organization, "team": Team, "project": Project}[scope_type]
    obj = None
    try:
        obj = await s.get(model, uuid.UUID(ident))
    except ValueError:
        if scope_type == "organization":
            obj = (await s.execute(select(Organization).where(Organization.slug == ident))).scalar_one_or_none()
    if obj is None:
        raise ScimError(400, f"{scope_type} {ident!r} does not exist", "invalidValue")
    return role, scope_type, obj.id


async def _binding_ids(s, scope_type: str, scope_id: uuid.UUID) -> dict:
    if scope_type == "organization":
        return {"org_id": scope_id, "team_id": None, "project_id": None}
    if scope_type == "team":
        t = await s.get(Team, scope_id)
        return {"org_id": t.org_id, "team_id": t.id, "project_id": None}
    p = await s.get(Project, scope_id)
    return {"org_id": p.org_id, "team_id": p.team_id, "project_id": p.id}


async def _grant(s, group: ScimGroup, user: ScimUser) -> None:
    if not group.role:
        return
    existing = (
        await s.execute(
            select(RoleBinding).where(
                RoleBinding.subject == user.user_name,
                RoleBinding.role == group.role,
                RoleBinding.scope_type == group.scope_type,
                RoleBinding.scope_id == group.scope_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        if existing.status != "active":
            before = {"status": existing.status}
            existing.status, existing.revoked_at, existing.created_by = "active", None, "scim"
            audit(
                s,
                SCIM_ACTOR,
                "role_binding.create",
                "role_binding",
                existing.id,
                before,
                {"status": "active"},
                existing.org_id,
            )
        return
    ids = await _binding_ids(s, group.scope_type, group.scope_id)
    rb = RoleBinding(
        subject=user.user_name,
        subject_kind="user",
        role=group.role,
        scope_type=group.scope_type,
        scope_id=group.scope_id,
        created_by="scim",
        **ids,
    )
    s.add(rb)
    await s.flush()
    audit(
        s,
        SCIM_ACTOR,
        "role_binding.create",
        "role_binding",
        rb.id,
        after={"subject": user.user_name, "role": group.role, "group": group.display_name},
        org_id=rb.org_id,
    )


async def _revoke(s, group: ScimGroup | None, user: ScimUser, *, all_bindings: bool = False) -> int:
    q = select(RoleBinding).where(RoleBinding.subject == user.user_name, RoleBinding.status == "active")
    if not all_bindings:
        if not group or not group.role:
            return 0
        q = q.where(
            RoleBinding.role == group.role,
            RoleBinding.scope_type == group.scope_type,
            RoleBinding.scope_id == group.scope_id,
        )
    n = 0
    for rb in (await s.execute(q)).scalars():
        rb.status, rb.revoked_at = "revoked", datetime.now(UTC)
        audit(
            s,
            SCIM_ACTOR,
            "role_binding.revoke",
            "role_binding",
            rb.id,
            {"status": "active"},
            {"status": "revoked"},
            rb.org_id,
        )
        n += 1
    return n


async def _members(s, group_id: uuid.UUID) -> list[ScimUser]:
    q = (
        select(ScimUser)
        .join(ScimGroupMember, ScimGroupMember.user_id == ScimUser.id)
        .where(ScimGroupMember.group_id == group_id)
        .order_by(ScimUser.user_name)
    )
    return list((await s.execute(q)).scalars())


async def _groups_of(s, user_id: uuid.UUID) -> list[ScimGroup]:
    q = (
        select(ScimGroup)
        .join(ScimGroupMember, ScimGroupMember.group_id == ScimGroup.id)
        .where(ScimGroupMember.user_id == user_id)
        .order_by(ScimGroup.display_name)
    )
    return list((await s.execute(q)).scalars())


def _emails(raw) -> list[dict]:
    if not raw:
        return []
    out = []
    for e in raw:
        if isinstance(e, dict) and e.get("value"):
            out.append(
                {"value": str(e["value"]).lower(), "primary": bool(e.get("primary", False)), "type": e.get("type")}
            )
        elif isinstance(e, str):
            out.append({"value": e.lower(), "primary": False, "type": None})
    return out


def _apply_user(u: ScimUser, body: dict) -> None:
    if "userName" in body:
        if not body["userName"]:
            raise ScimError(400, "userName is required", "invalidValue")
        u.user_name = str(body["userName"])
    if "externalId" in body:
        u.external_id = body["externalId"]
    if "displayName" in body:
        u.display_name = body["displayName"]
    elif "name" in body and isinstance(body["name"], dict) and body["name"].get("formatted"):
        u.display_name = body["name"]["formatted"]
    if "emails" in body:
        u.emails = _emails(body["emails"])
    if "active" in body:
        u.active = _bool(body["active"])
    u.updated_at = datetime.now(UTC)


def _bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _list(request: Request, items: list[dict], total: int, start: int, count: int) -> JSONResponse:
    return JSONResponse(
        {
            "schemas": [LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start,
            "itemsPerPage": len(items),
            "Resources": items,
        },
        media_type="application/scim+json",
    )


def _json(body: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type="application/scim+json")


# ---- discovery -------------------------------------------------------------


@router.get("/ServiceProviderConfig", dependencies=[Depends(require_scim)])
async def service_provider_config():
    return _json(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 200},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {"type": "oauthbearertoken", "name": "Bearer token", "description": "AIGW_SCIM_TOKEN"}
            ],
        }
    )


@router.get("/ResourceTypes", dependencies=[Depends(require_scim)])
async def resource_types():
    return _json(
        {
            "schemas": [LIST_SCHEMA],
            "totalResults": 2,
            "Resources": [
                {
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                    "id": "User",
                    "name": "User",
                    "endpoint": "/Users",
                    "schema": USER_SCHEMA,
                },
                {
                    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                    "id": "Group",
                    "name": "Group",
                    "endpoint": "/Groups",
                    "schema": GROUP_SCHEMA,
                },
            ],
        }
    )


@router.get("/Schemas", dependencies=[Depends(require_scim)])
async def schemas():
    return _json(
        {
            "schemas": [LIST_SCHEMA],
            "totalResults": 2,
            "Resources": [
                {
                    "id": USER_SCHEMA,
                    "name": "User",
                    "attributes": [{"name": n, "type": "string"} for n in ("userName", "displayName", "externalId")]
                    + [
                        {"name": "active", "type": "boolean"},
                        {"name": "emails", "type": "complex", "multiValued": True},
                    ],
                },
                {
                    "id": GROUP_SCHEMA,
                    "name": "Group",
                    "attributes": [
                        {"name": "displayName", "type": "string"},
                        {"name": "members", "type": "complex", "multiValued": True},
                    ],
                },
            ],
        }
    )


# ---- users ---------------------------------------------------------------


@router.get("/Users", dependencies=[Depends(require_scim)])
async def list_users(
    request: Request, filter: str | None = None, startIndex: int = Query(1, ge=1), count: int = Query(100, ge=0, le=200)
):  # noqa: A002
    flt = _parse_filter(filter)
    async with _db(request).session() as s:
        q = select(ScimUser)
        if flt:
            attr, value = flt
            if attr == "username":
                q = q.where(func.lower(ScimUser.user_name) == value.lower())
            elif attr == "externalid":
                q = q.where(ScimUser.external_id == value)
            elif attr in ("emails.value", "emails"):
                q = q.where(ScimUser.emails.op("@>")([{"value": value.lower()}]))
            else:
                raise ScimError(400, f"unsupported filter attribute {attr}", "invalidFilter")
        total = (await s.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
        rows = list((await s.execute(q.order_by(ScimUser.user_name).offset(startIndex - 1).limit(count))).scalars())
        items = [user_view(u, request, await _groups_of(s, u.id)) for u in rows]
        return _list(request, items, total, startIndex, count)


@router.post("/Users", status_code=201, dependencies=[Depends(require_scim)])
async def create_user(request: Request):
    body = await request.json()
    if not body.get("userName"):
        raise ScimError(400, "userName is required", "invalidValue")
    async with _db(request).tx() as s:
        dup = (
            await s.execute(select(ScimUser).where(func.lower(ScimUser.user_name) == str(body["userName"]).lower()))
        ).scalar_one_or_none()
        if dup:
            raise ScimError(409, f"userName {body['userName']!r} already exists", "uniqueness")
        u = ScimUser(user_name=str(body["userName"]), active=True)
        _apply_user(u, body)
        s.add(u)
        await s.flush()
        audit(s, SCIM_ACTOR, "scim.user.create", "scim_user", u.id, after=user_view(u, request))
        return _json(user_view(u, request), 201)


@router.get("/Users/{id}", dependencies=[Depends(require_scim)], name="scim_get_user")
async def get_user(id: str, request: Request):  # noqa: A002
    async with _db(request).session() as s:
        u = await s.get(ScimUser, _uuid(id, "User"))
        if u is None:
            raise ScimError(404, f"User {id} not found")
        return _json(user_view(u, request, await _groups_of(s, u.id)))


@router.put("/Users/{id}", dependencies=[Depends(require_scim)])
async def replace_user(id: str, request: Request):  # noqa: A002
    body = await request.json()
    async with _db(request).tx() as s:
        u = await s.get(ScimUser, _uuid(id, "User"))
        if u is None:
            raise ScimError(404, f"User {id} not found")
        before = user_view(u, request)
        _apply_user(u, {"externalId": None, "displayName": None, "emails": [], "active": True, **body})
        await _after_user_change(s, u, before)
        return _json(user_view(u, request, await _groups_of(s, u.id)))


@router.patch("/Users/{id}", dependencies=[Depends(require_scim)])
async def patch_user(id: str, request: Request):  # noqa: A002
    body = await request.json()
    async with _db(request).tx() as s:
        u = await s.get(ScimUser, _uuid(id, "User"))
        if u is None:
            raise ScimError(404, f"User {id} not found")
        before = user_view(u, request)
        for op in body.get("Operations") or []:
            name = str(op.get("op", "")).lower()
            path = (op.get("path") or "").strip()
            value = op.get("value")
            if name not in ("add", "replace", "remove"):
                raise ScimError(400, f"unsupported op {name!r}", "invalidValue")
            if not path:
                if not isinstance(value, dict):
                    raise ScimError(400, "value must be an object when path is omitted", "invalidValue")
                _apply_user(u, value)
                continue
            key = path.split("[")[0]
            if key in ("userName", "externalId", "displayName", "active", "emails"):
                _apply_user(u, {key: None if name == "remove" else value})
            elif key == "name.formatted":
                u.display_name = None if name == "remove" else value
            else:
                raise ScimError(400, f"unsupported path {path!r}", "invalidPath")
        await _after_user_change(s, u, before)
        return _json(user_view(u, request, await _groups_of(s, u.id)))


async def _after_user_change(s, u: ScimUser, before: dict) -> None:
    audit(s, SCIM_ACTOR, "scim.user.update", "scim_user", u.id, before, user_view_plain(u))
    if before.get("active") and not u.active:
        audit(s, SCIM_ACTOR, "scim.user.deactivate", "scim_user", u.id, after={"userName": u.user_name})
    if before.get("userName") and before["userName"] != u.user_name:
        # bindings follow the subject rename
        for rb in (await s.execute(select(RoleBinding).where(RoleBinding.subject == before["userName"]))).scalars():
            rb.subject = u.user_name


def user_view_plain(u: ScimUser) -> dict:
    return {
        "userName": u.user_name,
        "externalId": u.external_id,
        "displayName": u.display_name,
        "emails": u.emails or [],
        "active": u.active,
    }


@router.delete("/Users/{id}", status_code=204, dependencies=[Depends(require_scim)])
async def delete_user(id: str, request: Request):  # noqa: A002
    async with _db(request).tx() as s:
        u = await s.get(ScimUser, _uuid(id, "User"))
        if u is None:
            raise ScimError(404, f"User {id} not found")
        revoked = await _revoke(s, None, u, all_bindings=True)
        for m in (await s.execute(select(ScimGroupMember).where(ScimGroupMember.user_id == u.id))).scalars():
            await s.delete(m)
        audit(
            s,
            SCIM_ACTOR,
            "scim.user.delete",
            "scim_user",
            u.id,
            before=user_view_plain(u),
            after={"revoked_bindings": revoked},
        )
        await s.flush()  # membership deletes first: no relationship() orders them before the parent
        await s.delete(u)
    return Response(status_code=204)


# ---- groups --------------------------------------------------------------


@router.get("/Groups", dependencies=[Depends(require_scim)])
async def list_groups(
    request: Request, filter: str | None = None, startIndex: int = Query(1, ge=1), count: int = Query(100, ge=0, le=200)
):  # noqa: A002
    flt = _parse_filter(filter)
    async with _db(request).session() as s:
        q = select(ScimGroup)
        if flt:
            attr, value = flt
            if attr == "displayname":
                q = q.where(func.lower(ScimGroup.display_name) == value.lower())
            elif attr == "externalid":
                q = q.where(ScimGroup.external_id == value)
            else:
                raise ScimError(400, f"unsupported filter attribute {attr}", "invalidFilter")
        total = (await s.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
        rows = list((await s.execute(q.order_by(ScimGroup.display_name).offset(startIndex - 1).limit(count))).scalars())
        items = [group_view(g, await _members(s, g.id), request) for g in rows]
        return _list(request, items, total, startIndex, count)


async def _set_members(s, g: ScimGroup, member_ids: list[str]) -> list[ScimUser]:
    wanted: dict[uuid.UUID, ScimUser] = {}
    for raw in member_ids:
        u = await s.get(ScimUser, _uuid(raw, "member"))
        if u is None:
            raise ScimError(400, f"member {raw} is not a known User", "invalidValue")
        wanted[u.id] = u
    current = {
        m.user_id: m
        for m in (await s.execute(select(ScimGroupMember).where(ScimGroupMember.group_id == g.id))).scalars()
    }
    for uid, m in current.items():
        if uid not in wanted:
            user = await s.get(ScimUser, uid)
            await s.delete(m)
            if user:
                await _revoke(s, g, user)
    for uid, user in wanted.items():
        if uid not in current:
            s.add(ScimGroupMember(group_id=g.id, user_id=uid))
            await _grant(s, g, user)
    await s.flush()
    return sorted(wanted.values(), key=lambda u: u.user_name)


@router.post("/Groups", status_code=201, dependencies=[Depends(require_scim)])
async def create_group(request: Request):
    body = await request.json()
    name = body.get("displayName")
    if not name:
        raise ScimError(400, "displayName is required", "invalidValue")
    async with _db(request).tx() as s:
        if (
            await s.execute(select(ScimGroup).where(func.lower(ScimGroup.display_name) == name.lower()))
        ).scalar_one_or_none():
            raise ScimError(409, f"displayName {name!r} already exists", "uniqueness")
        role, scope_type, scope_id = await _resolve_mapping(s, name)
        g = ScimGroup(
            display_name=name, external_id=body.get("externalId"), role=role, scope_type=scope_type, scope_id=scope_id
        )
        s.add(g)
        await s.flush()
        members = await _set_members(s, g, [m.get("value") for m in body.get("members") or [] if isinstance(m, dict)])
        audit(
            s,
            SCIM_ACTOR,
            "scim.group.create",
            "scim_group",
            g.id,
            after={"displayName": name, "role": role, "members": [m.user_name for m in members]},
        )
        return _json(group_view(g, members, request), 201)


@router.get("/Groups/{id}", dependencies=[Depends(require_scim)], name="scim_get_group")
async def get_group(id: str, request: Request):  # noqa: A002
    async with _db(request).session() as s:
        g = await s.get(ScimGroup, _uuid(id, "Group"))
        if g is None:
            raise ScimError(404, f"Group {id} not found")
        return _json(group_view(g, await _members(s, g.id), request))


@router.put("/Groups/{id}", dependencies=[Depends(require_scim)])
async def replace_group(id: str, request: Request):  # noqa: A002
    body = await request.json()
    async with _db(request).tx() as s:
        g = await s.get(ScimGroup, _uuid(id, "Group"))
        if g is None:
            raise ScimError(404, f"Group {id} not found")
        await _rename_group(s, g, body.get("displayName") or g.display_name)
        if "externalId" in body:
            g.external_id = body["externalId"]
        members = await _set_members(s, g, [m.get("value") for m in body.get("members") or [] if isinstance(m, dict)])
        g.updated_at = datetime.now(UTC)
        audit(
            s,
            SCIM_ACTOR,
            "scim.group.update",
            "scim_group",
            g.id,
            after={"displayName": g.display_name, "members": [m.user_name for m in members]},
        )
        return _json(group_view(g, members, request))


async def _rename_group(s, g: ScimGroup, name: str) -> None:
    if name == g.display_name:
        return
    role, scope_type, scope_id = await _resolve_mapping(s, name)
    if (role, scope_type, scope_id) != (g.role, g.scope_type, g.scope_id):
        # mapping changed: move every member's binding
        members = await _members(s, g.id)
        for u in members:
            await _revoke(s, g, u)
        g.role, g.scope_type, g.scope_id = role, scope_type, scope_id
        for u in members:
            await _grant(s, g, u)
    g.display_name = name


@router.patch("/Groups/{id}", dependencies=[Depends(require_scim)])
async def patch_group(id: str, request: Request):  # noqa: A002
    body = await request.json()
    async with _db(request).tx() as s:
        g = await s.get(ScimGroup, _uuid(id, "Group"))
        if g is None:
            raise ScimError(404, f"Group {id} not found")
        current = [str(m.id) for m in await _members(s, g.id)]
        for op in body.get("Operations") or []:
            name = str(op.get("op", "")).lower()
            path = (op.get("path") or "").strip()
            value = op.get("value")
            if path.lower().startswith("members"):
                ids = _member_ids_from(path, value)
                if name == "add":
                    current = list(dict.fromkeys(current + ids))
                elif name == "remove":
                    current = [c for c in current if c not in ids] if ids else []
                elif name == "replace":
                    current = ids
                else:
                    raise ScimError(400, f"unsupported op {name!r}", "invalidValue")
            elif path.lower() == "displayname" or (not path and isinstance(value, dict) and "displayName" in value):
                new_name = value if path else value["displayName"]
                await _rename_group(s, g, str(new_name))
            elif path.lower() == "externalid" or (not path and isinstance(value, dict) and "externalId" in value):
                g.external_id = value if path else value["externalId"]
            elif not path and isinstance(value, dict) and "members" in value:
                ids = _member_ids_from("members", value["members"])
                current = list(dict.fromkeys(current + ids)) if name == "add" else ids
            else:
                raise ScimError(400, f"unsupported path {path!r}", "invalidPath")
        members = await _set_members(s, g, current)
        g.updated_at = datetime.now(UTC)
        audit(
            s,
            SCIM_ACTOR,
            "scim.group.update",
            "scim_group",
            g.id,
            after={"displayName": g.display_name, "members": [m.user_name for m in members]},
        )
        return _json(group_view(g, members, request))


def _member_ids_from(path: str, value) -> list[str]:
    m = re.search(r'members\[value\s+eq\s+"([^"]+)"\]', path, re.IGNORECASE)
    if m:
        return [m.group(1)]
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    return [str(v.get("value")) for v in value if isinstance(v, dict) and v.get("value")]


@router.delete("/Groups/{id}", status_code=204, dependencies=[Depends(require_scim)])
async def delete_group(id: str, request: Request):  # noqa: A002
    async with _db(request).tx() as s:
        g = await s.get(ScimGroup, _uuid(id, "Group"))
        if g is None:
            raise ScimError(404, f"Group {id} not found")
        members = await _members(s, g.id)
        for u in members:
            await _revoke(s, g, u)
        for m in (await s.execute(select(ScimGroupMember).where(ScimGroupMember.group_id == g.id))).scalars():
            await s.delete(m)
        audit(
            s,
            SCIM_ACTOR,
            "scim.group.delete",
            "scim_group",
            g.id,
            before={"displayName": g.display_name, "members": [m.user_name for m in members]},
        )
        await s.flush()  # membership deletes first: no relationship() orders them before the parent
        await s.delete(g)
    return Response(status_code=204)


# ---- lookups used by the control-API authenticator ----------------------------


async def deactivated_subject(s, identities: set[str]) -> str | None:
    """Return the userName when any identity matches a SCIM user that is not active."""
    if not identities:
        return None
    lowered = [i.lower() for i in identities]
    q = select(ScimUser).where(
        or_(func.lower(ScimUser.user_name).in_(lowered), *[ScimUser.emails.op("@>")([{"value": i}]) for i in lowered])
    )
    for u in (await s.execute(q)).scalars():
        if not u.active:
            return u.user_name
    return None


__all__ = ["ROLE_RANK", "ScimError", "deactivated_subject", "router"]

"""Control API /admin/v1 (docs/spec/01 §3). Every write is audited and bumps the config version when it
affects the gateway snapshot.

Authorization is two-step (docs/spec/01 §3.1–3.2): ``require_scope`` gates the route, then every handler calls
``actor.require(scope, org_id=…, team_id=…, project_id=…)`` with the target's tenant ids, and list handlers
apply ``tenancy.visible(...)`` so a delegate only sees rows inside its tenants.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import case, func, select

from aigw.admin import reconcile as R
from aigw.admin import schemas as S
from aigw.admin.auth import ROLE_RANK, Actor, require_admin, require_scope
from aigw.admin.service import (
    audit,
    bump_config,
    current_config_version,
    get_or_404,
    parse_uuid,
    publish_key_invalidation,
    to_dict,
)
from aigw.admin.tenancy import budget_target_ids, visible, visible_budgets
from aigw.core.errors import ErrorType, GatewayError
from aigw.db.models import (
    AuditEvent,
    Budget,
    Deployment,
    DeploymentHealth,
    GuardrailEvent,
    Invoice,
    InvoiceLine,
    Model,
    Organization,
    Price,
    Project,
    RequestAttempt,
    RoleBinding,
    Team,
    UsageEvent,
    VirtualKey,
)
from aigw.gateway.auth import generate_key

# require_admin at router level is the fail-closed backstop; each route declares its scope with require_scope
# (docs/spec/01 §3.1). tests/test_admin_oidc.py fails if a route is added without one.
router = APIRouter(prefix="/admin/v1", dependencies=[Depends(require_admin)])

# Unauthenticated discovery for the portal: which credentials this control plane accepts (docs/spec/01 §3.1).
# Carries no secrets — issuer and client id are public parts of the OIDC authorization-code flow.
public_router = APIRouter(prefix="/admin/v1")


@public_router.get("/auth/config")
async def auth_config(request: Request):
    settings = request.app.state.settings
    oidc = None
    if settings.oidc_issuer:
        oidc = {
            "issuer": settings.oidc_issuer.rstrip("/"),
            "client_id": settings.oidc_client_id or settings.oidc_audience,
            "audience": settings.oidc_audience,
        }
    return {"admin_key": bool(settings.admin_key), "oidc": oidc}


@router.get("/me")
async def me(actor: Actor = Depends(require_admin)):
    """Who am I: actor identity, roles from the token, delegated grants and the effective control-API scopes
    (the portal uses it to hide actions the caller cannot perform)."""
    return {
        "actor_type": actor.type,
        "actor_id": actor.id,
        "roles": sorted(actor.roles),
        "scopes": actor.effective_scopes(),
        "global_scopes": [s for s in actor.effective_scopes() if actor.unrestricted(s)],
        "grants": [
            {
                "role": g.role,
                "scope_type": g.scope_type,
                "scope_id": str(g.project_id or g.team_id or g.org_id),
                "org_id": str(g.org_id),
                "team_id": str(g.team_id) if g.team_id else None,
                "project_id": str(g.project_id) if g.project_id else None,
            }
            for g in actor.grants
        ],
    }


def _db(request: Request):
    return request.app.state.db


def _org_visible(actor: Actor, org_id: uuid.UUID) -> None:
    """Reading an organization row: org scope, or any grant inside it (a team owner may see its org)."""
    if not (actor.permits("organizations:read", org_id=org_id) or any(g.org_id == org_id for g in actor.grants)):
        actor.require("organizations:read", org_id=org_id)


def _team_visible(actor: Actor, team: Team) -> None:
    if not (
        actor.permits("teams:read", org_id=team.org_id, team_id=team.id)
        or any(g.team_id == team.id for g in actor.grants)
    ):
        actor.require("teams:read", org_id=team.org_id, team_id=team.id)


# ---- organizations ------------------------------------------------------


@router.post("/organizations", status_code=201)
async def create_org(body: S.OrgCreate, request: Request, actor: Actor = Depends(require_scope("organizations:write"))):
    actor.require("organizations:write")  # creating an organization is a global act
    async with _db(request).tx() as s:
        if (await s.execute(select(Organization).where(Organization.slug == body.slug))).scalar_one_or_none():
            raise GatewayError(ErrorType.invalid_request, "slug already exists", code="conflict", param="slug")
        org = Organization(name=body.name, slug=body.slug, settings=body.settings)
        s.add(org)
        await s.flush()
        audit(s, actor, "organization.create", "organization", org.id, after=to_dict(org), org_id=org.id)
        return to_dict(org)


@router.get("/organizations")
async def list_orgs(
    request: Request, limit: int = Query(50, le=200), actor: Actor = Depends(require_scope("organizations:read"))
):
    async with _db(request).session() as s:
        q = (
            select(Organization)
            .where(visible(actor, "organizations:read", id_=Organization.id))
            .order_by(Organization.created_at.desc())
            .limit(limit)
        )
        return {"data": [to_dict(o) for o in (await s.execute(q)).scalars()]}


@router.get("/organizations/{org_id}")
async def get_org(org_id: str, request: Request, actor: Actor = Depends(require_scope("organizations:read"))):
    async with _db(request).session() as s:
        org = await get_or_404(s, Organization, org_id, "organization")
        _org_visible(actor, org.id)
        return to_dict(org)


@router.patch("/organizations/{org_id}")
async def patch_org(
    org_id: str, body: S.OrgPatch, request: Request, actor: Actor = Depends(require_scope("organizations:write"))
):
    async with _db(request).tx() as s:
        org = await get_or_404(s, Organization, org_id, "organization")
        actor.require("organizations:write", org_id=org.id)
        before = to_dict(org)
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(org, k, v)
        audit(s, actor, "organization.update", "organization", org.id, before, to_dict(org), org.id)
        return to_dict(org)


# ---- teams / projects ---------------------------------------------------


@router.post("/organizations/{org_id}/teams", status_code=201)
async def create_team(
    org_id: str, body: S.TeamCreate, request: Request, actor: Actor = Depends(require_scope("teams:write"))
):
    async with _db(request).tx() as s:
        org = await get_or_404(s, Organization, org_id, "organization")
        actor.require("teams:write", org_id=org.id)
        team = Team(org_id=org.id, name=body.name, settings=body.settings)
        s.add(team)
        await s.flush()
        audit(s, actor, "team.create", "team", team.id, after=to_dict(team), org_id=org.id)
        return to_dict(team)


@router.get("/organizations/{org_id}/teams")
async def list_teams(org_id: str, request: Request, actor: Actor = Depends(require_scope("teams:read"))):
    async with _db(request).session() as s:
        oid = parse_uuid(org_id)
        _org_visible(actor, oid)
        q = (
            select(Team)
            .where(Team.org_id == oid, visible(actor, "teams:read", org=Team.org_id, id_=Team.id))
            .order_by(Team.name)
        )
        return {"data": [to_dict(t) for t in (await s.execute(q)).scalars()]}


@router.get("/teams/{team_id}")
async def get_team(team_id: str, request: Request, actor: Actor = Depends(require_scope("teams:read"))):
    async with _db(request).session() as s:
        t = await get_or_404(s, Team, team_id, "team")
        _team_visible(actor, t)
        return to_dict(t)


@router.patch("/teams/{team_id}")
async def patch_team(
    team_id: str, body: S.NamePatch, request: Request, actor: Actor = Depends(require_scope("teams:write"))
):
    async with _db(request).tx() as s:
        t = await get_or_404(s, Team, team_id, "team")
        actor.require("teams:write", org_id=t.org_id, team_id=t.id)
        before = to_dict(t)
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(t, k, v)
        audit(s, actor, "team.update", "team", t.id, before, to_dict(t), t.org_id)
        return to_dict(t)


@router.post("/teams/{team_id}/projects", status_code=201)
async def create_project(
    team_id: str, body: S.ProjectCreate, request: Request, actor: Actor = Depends(require_scope("projects:write"))
):
    async with _db(request).tx() as s:
        team = await get_or_404(s, Team, team_id, "team")
        actor.require("projects:write", org_id=team.org_id, team_id=team.id)
        p = Project(org_id=team.org_id, team_id=team.id, name=body.name, settings=body.settings)
        s.add(p)
        await s.flush()
        audit(s, actor, "project.create", "project", p.id, after=to_dict(p), org_id=team.org_id)
        bump_config(s, f"project.create {p.id}")
        return to_dict(p)


@router.get("/teams/{team_id}/projects")
async def list_projects(team_id: str, request: Request, actor: Actor = Depends(require_scope("projects:read"))):
    async with _db(request).session() as s:
        team = await get_or_404(s, Team, team_id, "team")
        _team_visible(actor, team)
        q = (
            select(Project)
            .where(
                Project.team_id == team.id,
                visible(actor, "projects:read", org=Project.org_id, team=Project.team_id, id_=Project.id),
            )
            .order_by(Project.name)
        )
        return {"data": [to_dict(p) for p in (await s.execute(q)).scalars()]}


@router.get("/projects/{project_id}")
async def get_project(project_id: str, request: Request, actor: Actor = Depends(require_scope("projects:read"))):
    async with _db(request).session() as s:
        p = await get_or_404(s, Project, project_id, "project")
        actor.require("projects:read", org_id=p.org_id, team_id=p.team_id, project_id=p.id)
        return to_dict(p)


@router.patch("/projects/{project_id}")
async def patch_project(
    project_id: str, body: S.NamePatch, request: Request, actor: Actor = Depends(require_scope("projects:write"))
):
    async with _db(request).tx() as s:
        p = await get_or_404(s, Project, project_id, "project")
        actor.require("projects:write", org_id=p.org_id, team_id=p.team_id, project_id=p.id)
        before = to_dict(p)
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(p, k, v)
        audit(s, actor, "project.update", "project", p.id, before, to_dict(p), p.org_id)
        bump_config(s, f"project.update {p.id}")
        return to_dict(p)


# ---- keys ---------------------------------------------------------------


def _key_ids(k: VirtualKey) -> dict:
    return {"org_id": k.org_id, "team_id": k.team_id, "project_id": k.project_id}


@router.post("/projects/{project_id}/keys", status_code=201)
async def create_key(
    project_id: str, body: S.KeyCreate, request: Request, actor: Actor = Depends(require_scope("keys:write"))
):
    async with _db(request).tx() as s:
        p = await get_or_404(s, Project, project_id, "project")
        actor.require("keys:write", org_id=p.org_id, team_id=p.team_id, project_id=p.id)
        plaintext, key_hash, prefix = generate_key()
        k = VirtualKey(
            org_id=p.org_id,
            team_id=p.team_id,
            project_id=p.id,
            name=body.name,
            key_prefix=prefix,
            key_hash=key_hash,
            expires_at=body.expires_at,
            allowed_models=body.allowed_models,
            rpm_limit=body.rpm_limit,
            tpm_limit=body.tpm_limit,
            metadata_=body.metadata,
        )
        s.add(k)
        await s.flush()
        audit(s, actor, "key.create", "key", k.id, after=to_dict(k), org_id=p.org_id)
        bump_config(s, f"key.create {k.id}")
        return {**to_dict(k), "key": plaintext}


@router.get("/projects/{project_id}/keys")
async def list_keys(project_id: str, request: Request, actor: Actor = Depends(require_scope("keys:read"))):
    async with _db(request).session() as s:
        p = await get_or_404(s, Project, project_id, "project")
        actor.require("keys:read", org_id=p.org_id, team_id=p.team_id, project_id=p.id)
        q = select(VirtualKey).where(VirtualKey.project_id == p.id).order_by(VirtualKey.created_at.desc())
        return {"data": [to_dict(k) for k in (await s.execute(q)).scalars()]}


@router.get("/keys/{key_id}")
async def get_key(key_id: str, request: Request, actor: Actor = Depends(require_scope("keys:read"))):
    async with _db(request).session() as s:
        k = await get_or_404(s, VirtualKey, key_id, "key")
        actor.require("keys:read", **_key_ids(k))
        return to_dict(k)


@router.post("/keys/{key_id}/revoke")
async def revoke_key(key_id: str, request: Request, actor: Actor = Depends(require_scope("keys:write"))):
    async with _db(request).tx() as s:
        k = await get_or_404(s, VirtualKey, key_id, "key")
        actor.require("keys:write", **_key_ids(k))
        before = to_dict(k)
        k.status, k.revoked_at, k.grace_until = "revoked", datetime.now(UTC), None
        audit(s, actor, "key.revoke", "key", k.id, before, to_dict(k), k.org_id)
        bump_config(s, f"key.revoke {k.id}")
        key_hash = k.key_hash
        out = to_dict(k)
    await publish_key_invalidation(request.app.state.valkey, key_hash)
    return out


@router.post("/keys/{key_id}/rotate", status_code=201)
async def rotate_key(
    key_id: str, body: S.KeyRotate, request: Request, actor: Actor = Depends(require_scope("keys:write"))
):
    async with _db(request).tx() as s:
        old = await get_or_404(s, VirtualKey, key_id, "key")
        actor.require("keys:write", **_key_ids(old))
        if old.status != "active":
            raise GatewayError(ErrorType.invalid_request, "only active keys can be rotated", code="key_not_active")
        plaintext, key_hash, prefix = generate_key()
        new = VirtualKey(
            org_id=old.org_id,
            team_id=old.team_id,
            project_id=old.project_id,
            name=old.name,
            key_prefix=prefix,
            key_hash=key_hash,
            expires_at=old.expires_at,
            allowed_models=old.allowed_models,
            rpm_limit=old.rpm_limit,
            tpm_limit=old.tpm_limit,
            metadata_=old.metadata_,
            rotated_from=old.id,
        )
        old.status = "revoked"
        old.revoked_at = datetime.now(UTC)
        old.grace_until = old.revoked_at + timedelta(seconds=body.grace_seconds)
        s.add(new)
        await s.flush()
        audit(
            s,
            actor,
            "key.rotate",
            "key",
            old.id,
            after={"new_key_id": str(new.id), "grace_until": old.grace_until.isoformat()},
            org_id=old.org_id,
        )
        bump_config(s, f"key.rotate {old.id}")
        return {**to_dict(new), "key": plaintext, "previous_key_grace_until": old.grace_until.isoformat()}


# ---- models / deployments / prices -------------------------------------


@router.post("/models", status_code=201)
async def create_model(body: S.ModelCreate, request: Request, actor: Actor = Depends(require_scope("models:write"))):
    org_id = parse_uuid(body.org_id, "org_id") if body.org_id else None
    actor.require("models:write", org_id=org_id)  # global models (org_id null) need a global scope
    async with _db(request).tx() as s:
        m = Model(
            org_id=org_id,
            name=body.name,
            display_name=body.display_name,
            modalities=body.modalities,
            context_window=body.context_window,
            supports_tools=body.supports_tools,
            supports_json_schema=body.supports_json_schema,
            supports_vision=body.supports_vision,
            metadata_=body.metadata,
        )
        s.add(m)
        await s.flush()
        audit(s, actor, "model.create", "model", m.id, after=to_dict(m), org_id=org_id)
        bump_config(s, f"model.create {m.id}")
        return to_dict(m)


@router.get("/models")
async def list_models(
    request: Request, org_id: str | None = None, actor: Actor = Depends(require_scope("models:read"))
):
    async with _db(request).session() as s:
        q = select(Model).where(Model.org_id.is_(None) | visible(actor, "models:read", org=Model.org_id))
        if org_id:
            q = q.where((Model.org_id == parse_uuid(org_id)) | (Model.org_id.is_(None)))
        models = list((await s.execute(q.order_by(Model.name))).scalars())
        deps = list(
            (await s.execute(select(Deployment).where(Deployment.model_id.in_([m.id for m in models])))).scalars()
        )
        health = {
            h.deployment_id: h
            for h in (
                await s.execute(
                    select(DeploymentHealth).where(DeploymentHealth.deployment_id.in_([d.id for d in deps]))
                )
            ).scalars()
        }
        by_model: dict[uuid.UUID, list] = {}
        for d in deps:
            h = health.get(d.id)
            by_model.setdefault(d.model_id, []).append(
                {**to_dict(d), "health": {k: v for k, v in to_dict(h).items() if k != "deployment_id"} if h else None}
            )
        return {"data": [{**to_dict(m), "deployments": by_model.get(m.id, [])} for m in models]}


@router.patch("/models/{model_id}")
async def patch_model(
    model_id: str, body: S.ModelPatch, request: Request, actor: Actor = Depends(require_scope("models:write"))
):
    async with _db(request).tx() as s:
        m = await get_or_404(s, Model, model_id, "model")
        actor.require("models:write", org_id=m.org_id)
        before = to_dict(m)
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(m, "metadata_" if k == "metadata" else k, v)
        audit(s, actor, "model.update", "model", m.id, before, to_dict(m), m.org_id)
        bump_config(s, f"model.update {m.id}")
        return to_dict(m)


@router.post("/models/{model_id}/deployments", status_code=201)
async def create_deployment(
    model_id: str,
    body: S.DeploymentCreate,
    request: Request,
    actor: Actor = Depends(require_scope("deployments:write")),
):
    async with _db(request).tx() as s:
        m = await get_or_404(s, Model, model_id, "model")
        actor.require("deployments:write", org_id=m.org_id)
        d = Deployment(model_id=m.id, org_id=m.org_id, **body.model_dump())
        s.add(d)
        await s.flush()
        audit(s, actor, "deployment.create", "deployment", d.id, after=to_dict(d), org_id=m.org_id)
        bump_config(s, f"deployment.create {d.id}")
        return to_dict(d)


@router.patch("/deployments/{deployment_id}")
async def patch_deployment(
    deployment_id: str,
    body: S.DeploymentPatch,
    request: Request,
    actor: Actor = Depends(require_scope("deployments:write")),
):
    async with _db(request).tx() as s:
        d = await get_or_404(s, Deployment, deployment_id, "deployment")
        actor.require("deployments:write", org_id=d.org_id)
        before = to_dict(d)
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(d, k, v)
        audit(s, actor, "deployment.update", "deployment", d.id, before, to_dict(d), d.org_id)
        bump_config(s, f"deployment.update {d.id}")
        return to_dict(d)


@router.post("/deployments/{deployment_id}/cooldown")
async def cooldown_deployment(
    deployment_id: str,
    body: S.CooldownRequest,
    request: Request,
    actor: Actor = Depends(require_scope("deployments:write")),
):
    async with _db(request).tx() as s:
        d = await get_or_404(s, Deployment, deployment_id, "deployment")
        actor.require("deployments:write", org_id=d.org_id)
        before = to_dict(d)
        d.cooldown_until = datetime.now(UTC) + timedelta(seconds=body.seconds) if body.seconds else None
        audit(s, actor, "deployment.cooldown", "deployment", d.id, before, to_dict(d), d.org_id)
        bump_config(s, f"deployment.cooldown {d.id}")
        return to_dict(d)


@router.post("/prices", status_code=201)
async def create_price(body: S.PriceCreate, request: Request, actor: Actor = Depends(require_scope("prices:write"))):
    actor.require("prices:write")  # the price registry is global
    async with _db(request).tx() as s:
        latest = (
            await s.execute(
                select(func.max(Price.version)).where(
                    Price.provider == body.provider, Price.provider_model == body.provider_model
                )
            )
        ).scalar()
        p = Price(
            provider=body.provider,
            provider_model=body.provider_model,
            version=(latest or 0) + 1,
            input_per_million=body.input_per_million,
            output_per_million=body.output_per_million,
            cached_input_per_million=body.cached_input_per_million,
            reasoning_per_million=body.reasoning_per_million,
            effective_from=body.effective_from or datetime.now(UTC),
            source=body.source,
            reviewed_by=body.reviewed_by,
        )
        s.add(p)
        await s.flush()
        audit(s, actor, "price.create", "price", p.id, after=to_dict(p))
        bump_config(s, f"price.create {p.id}")
        return to_dict(p)


@router.get("/prices", dependencies=[Depends(require_scope("prices:read"))])
async def list_prices(request: Request, provider: str | None = None):
    async with _db(request).session() as s:
        q = select(Price).order_by(Price.provider, Price.provider_model, Price.version.desc())
        if provider:
            q = q.where(Price.provider == provider)
        return {"data": [to_dict(p) for p in (await s.execute(q)).scalars()]}


# ---- budgets ------------------------------------------------------------

_SCOPE_MODEL = {"organization": Organization, "team": Team, "project": Project, "key": VirtualKey}


async def _budget_scope_ids(s, scope_type: str, scope_id) -> dict:
    target = await get_or_404(s, _SCOPE_MODEL[scope_type], str(scope_id), scope_type)
    return budget_target_ids(target, scope_type)


@router.post("/budgets", status_code=201)
async def create_budget(body: S.BudgetCreate, request: Request, actor: Actor = Depends(require_scope("budgets:write"))):
    async with _db(request).tx() as s:
        target = await get_or_404(s, _SCOPE_MODEL[body.scope_type], body.scope_id, body.scope_type)
        ids = budget_target_ids(target, body.scope_type)
        actor.require("budgets:write", **ids)
        org_id = ids["org_id"]
        existing = (
            await s.execute(
                select(Budget).where(
                    Budget.scope_type == body.scope_type, Budget.scope_id == target.id, Budget.period == body.period
                )
            )
        ).scalar_one_or_none()
        if existing:
            raise GatewayError(
                ErrorType.invalid_request, "budget already exists for this scope/period", code="conflict"
            )
        b = Budget(
            org_id=org_id,
            scope_type=body.scope_type,
            scope_id=target.id,
            limit_amount=body.limit_amount,
            period=body.period,
            soft_alert_pct=body.soft_alert_pct,
            period_start=datetime.now(UTC),
        )
        s.add(b)
        await s.flush()
        audit(s, actor, "budget.create", "budget", b.id, after=to_dict(b), org_id=org_id)
        return to_dict(b)


@router.get("/budgets")
async def list_budgets(
    request: Request,
    scope_type: str | None = None,
    scope_id: str | None = None,
    org_id: str | None = None,
    actor: Actor = Depends(require_scope("budgets:read")),
):
    async with _db(request).session() as s:
        q = select(Budget).where(visible_budgets(actor)).order_by(Budget.created_at.desc())
        if scope_type:
            q = q.where(Budget.scope_type == scope_type)
        if scope_id:
            q = q.where(Budget.scope_id == parse_uuid(scope_id, "scope_id"))
        if org_id:
            q = q.where(Budget.org_id == parse_uuid(org_id, "org_id"))
        return {"data": [_budget_view(b) for b in (await s.execute(q)).scalars()]}


def _budget_view(b: Budget) -> dict:
    d = to_dict(b)
    limit = Decimal(b.limit_amount)
    if b.temporary_until and b.temporary_until > datetime.now(UTC):
        limit += Decimal(b.temporary_increase or 0)
    d["available_amount"] = format(limit - Decimal(b.spent_amount) - Decimal(b.reserved_amount), "f")
    return d


@router.patch("/budgets/{budget_id}")
async def patch_budget(
    budget_id: str, body: S.BudgetPatch, request: Request, actor: Actor = Depends(require_scope("budgets:write"))
):
    async with _db(request).tx() as s:
        b = await get_or_404(s, Budget, budget_id, "budget")
        actor.require("budgets:write", **await _budget_scope_ids(s, b.scope_type, b.scope_id))
        before = to_dict(b)
        for k, v in body.model_dump(exclude_none=True).items():
            setattr(b, k, v)
        audit(s, actor, "budget.update", "budget", b.id, before, to_dict(b), b.org_id)
        return _budget_view(b)


@router.post("/budgets/{budget_id}/temporary-increase")
async def temporary_increase(
    budget_id: str, body: S.TemporaryIncrease, request: Request, actor: Actor = Depends(require_scope("budgets:write"))
):
    async with _db(request).tx() as s:
        b = await get_or_404(s, Budget, budget_id, "budget")
        actor.require("budgets:write", **await _budget_scope_ids(s, b.scope_type, b.scope_id))
        before = to_dict(b)
        b.temporary_increase, b.temporary_until = body.amount, body.until
        audit(s, actor, "budget.temporary_increase", "budget", b.id, before, to_dict(b), b.org_id)
        return _budget_view(b)


# ---- usage / requests / audit / config ----------------------------------


@router.get("/usage")
async def usage(
    request: Request,
    scope_type: str = Query(pattern="^(organization|team|project|key)$"),
    scope_id: str = Query(...),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    group_by: str = Query("model", pattern="^(model|day|key|deployment)$"),
    actor: Actor = Depends(require_scope("usage:read")),
):
    col = {
        "organization": UsageEvent.org_id,
        "team": UsageEvent.team_id,
        "project": UsageEvent.project_id,
        "key": UsageEvent.key_id,
    }[scope_type]
    group_col = {
        "model": UsageEvent.model_name,
        "day": func.date_trunc("day", UsageEvent.created_at),
        "key": UsageEvent.key_id,
        "deployment": UsageEvent.deployment_id,
    }[group_by]
    async with _db(request).session() as s:
        actor.require("usage:read", **await _budget_scope_ids(s, scope_type, parse_uuid(scope_id, "scope_id")))
        q = select(
            group_col.label("group"),
            func.count().label("requests"),
            func.sum(UsageEvent.prompt_tokens).label("prompt_tokens"),
            func.sum(UsageEvent.completion_tokens).label("completion_tokens"),
            func.sum(UsageEvent.cost).label("cost"),
        ).where(col == parse_uuid(scope_id, "scope_id"))
        if from_:
            q = q.where(UsageEvent.created_at >= from_)
        if to:
            q = q.where(UsageEvent.created_at < to)
        rows = (await s.execute(q.group_by("group").order_by("group"))).all()
        data = [
            {
                "group": (str(r.group) if not isinstance(r.group, datetime) else r.group.date().isoformat()),
                "requests": r.requests,
                "prompt_tokens": int(r.prompt_tokens or 0),
                "completion_tokens": int(r.completion_tokens or 0),
                "cost": str(r.cost or 0),
            }
            for r in rows
        ]
        return {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "group_by": group_by,
            "data": data,
            "total_cost": str(sum((Decimal(d["cost"]) for d in data), Decimal(0))),
        }


@router.get("/requests/{request_id}")
async def get_request(request_id: str, request: Request, actor: Actor = Depends(require_scope("requests:read"))):
    async with _db(request).session() as s:
        rid = parse_uuid(request_id, "request_id")
        attempts = list(
            (
                await s.execute(
                    select(RequestAttempt).where(RequestAttempt.request_id == rid).order_by(RequestAttempt.attempt_no)
                )
            ).scalars()
        )
        if not attempts:
            raise GatewayError(ErrorType.not_found, "request not found", code="request_not_found")
        a = attempts[0]
        actor.require("requests:read", org_id=a.org_id, team_id=a.team_id, project_id=a.project_id)
        events = list((await s.execute(select(UsageEvent).where(UsageEvent.request_id == rid))).scalars())
        guards = list(
            (
                await s.execute(
                    select(GuardrailEvent).where(GuardrailEvent.request_id == rid).order_by(GuardrailEvent.created_at)
                )
            ).scalars()
        )
        return {
            "request_id": request_id,
            "attempts": [to_dict(a) for a in attempts],
            "usage": [to_dict(e) for e in events],
            "guardrails": [to_dict(g) for g in guards],
        }


def _usage_visible(actor: Actor):
    return visible(
        actor, "requests:read", org=UsageEvent.org_id, team=UsageEvent.team_id, project=UsageEvent.project_id
    )


@router.get("/requests")
async def list_requests(
    request: Request,
    project_id: str | None = None,
    key_id: str | None = None,
    org_id: str | None = None,
    limit: int = Query(50, le=500),
    actor: Actor = Depends(require_scope("requests:read")),
):
    async with _db(request).session() as s:
        q = select(UsageEvent).where(_usage_visible(actor)).order_by(UsageEvent.created_at.desc()).limit(limit)
        if project_id:
            q = q.where(UsageEvent.project_id == parse_uuid(project_id, "project_id"))
        if key_id:
            q = q.where(UsageEvent.key_id == parse_uuid(key_id, "key_id"))
        if org_id:
            q = q.where(UsageEvent.org_id == parse_uuid(org_id, "org_id"))
        return {"data": [to_dict(e) for e in (await s.execute(q)).scalars()]}


@router.get("/audit")
async def list_audit(
    request: Request,
    target_type: str | None = None,
    target_id: str | None = None,
    org_id: str | None = None,
    limit: int = Query(100, le=1000),
    actor: Actor = Depends(require_scope("audit:read")),
):
    async with _db(request).session() as s:
        q = (
            select(AuditEvent)
            .where(visible(actor, "audit:read", org=AuditEvent.org_id))
            .order_by(AuditEvent.created_at.desc())
            .limit(limit)
        )
        if target_type:
            q = q.where(AuditEvent.target_type == target_type)
        if target_id:
            q = q.where(AuditEvent.target_id == parse_uuid(target_id, "target_id"))
        if org_id:
            q = q.where(AuditEvent.org_id == parse_uuid(org_id, "org_id"))
        return {"data": [to_dict(a) for a in (await s.execute(q)).scalars()]}


@router.get("/config/version", dependencies=[Depends(require_scope("config:read"))])
async def config_version(request: Request):
    async with _db(request).session() as s:
        return {"version": await current_config_version(s)}


@router.get("/overview")
async def overview(
    request: Request,
    org_id: str | None = None,
    hours: int = Query(24, ge=1, le=24 * 90),
    actor: Actor = Depends(require_scope("usage:read")),
):
    """Traffic, latency, failures and spend for the portal overview."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    async with _db(request).session() as s:
        base = select(UsageEvent).where(UsageEvent.created_at >= since, _usage_visible(actor))
        if org_id:
            base = base.where(UsageEvent.org_id == parse_uuid(org_id, "org_id"))
        sub = base.subquery()
        totals = (
            await s.execute(
                select(
                    func.count(),
                    func.sum(sub.c.cost),
                    func.sum(sub.c.prompt_tokens + sub.c.completion_tokens),
                    func.percentile_cont(0.95).within_group(sub.c.latency_ms),
                    func.sum(case((sub.c.status != "succeeded", 1), else_=0)),
                )
            )
        ).one()
        by_model = (
            await s.execute(
                select(sub.c.model_name, func.count(), func.sum(sub.c.cost))
                .group_by(sub.c.model_name)
                .order_by(func.count().desc())
                .limit(10)
            )
        ).all()
        by_status = (await s.execute(select(sub.c.status, func.count()).group_by(sub.c.status))).all()
        return {
            "since": since.isoformat(),
            "requests": totals[0],
            "cost": str(totals[1] or 0),
            "tokens": int(totals[2] or 0),
            "p95_latency_ms": float(totals[3]) if totals[3] is not None else None,
            "failed": int(totals[4] or 0),
            "by_model": [{"model": r[0], "requests": r[1], "cost": str(r[2] or 0)} for r in by_model],
            "by_status": {r[0]: r[1] for r in by_status},
        }


# ---- role bindings (docs/spec/01 §3.2) ----------------------------------

_ROLE_SCOPE_TYPE = {"org_owner": "organization", "team_owner": "team", "project_member": "project"}


def _binding_ids(b: RoleBinding) -> dict:
    return {"org_id": b.org_id, "team_id": b.team_id, "project_id": b.project_id}


@router.post("/role-bindings", status_code=201)
async def create_role_binding(
    body: S.RoleBindingCreate, request: Request, actor: Actor = Depends(require_scope("role_bindings:write"))
):
    if _ROLE_SCOPE_TYPE[body.role] != body.scope_type:
        raise GatewayError(
            ErrorType.invalid_request,
            f"role {body.role} binds to a {_ROLE_SCOPE_TYPE[body.role]}, not a {body.scope_type}",
            code="role_scope_mismatch",
            param="scope_type",
        )
    async with _db(request).tx() as s:
        target = await get_or_404(s, _SCOPE_MODEL[body.scope_type], body.scope_id, body.scope_type)
        ids = budget_target_ids(target, body.scope_type)
        actor.require("role_bindings:write", **ids)
        if actor.max_rank(**ids) < ROLE_RANK[body.role]:
            raise GatewayError(
                ErrorType.permission, f"You cannot grant {body.role} here", code="rank_exceeded", param="role"
            )
        existing = (
            await s.execute(
                select(RoleBinding).where(
                    RoleBinding.subject == body.subject,
                    RoleBinding.role == body.role,
                    RoleBinding.scope_type == body.scope_type,
                    RoleBinding.scope_id == target.id,
                )
            )
        ).scalar_one_or_none()
        if existing and existing.status == "active":
            raise GatewayError(ErrorType.invalid_request, "binding already exists", code="conflict")
        if existing:  # re-activate a revoked binding rather than violate the unique constraint
            before = to_dict(existing)
            existing.status, existing.revoked_at, existing.created_by = "active", None, actor.id
            audit(
                s, actor, "role_binding.create", "role_binding", existing.id, before, to_dict(existing), ids["org_id"]
            )
            return to_dict(existing)
        rb = RoleBinding(
            subject=body.subject,
            subject_kind=body.subject_kind,
            role=body.role,
            scope_type=body.scope_type,
            scope_id=target.id,
            org_id=ids["org_id"],
            team_id=ids.get("team_id"),
            project_id=ids.get("project_id"),
            created_by=actor.id,
        )
        s.add(rb)
        await s.flush()
        audit(s, actor, "role_binding.create", "role_binding", rb.id, after=to_dict(rb), org_id=rb.org_id)
        return to_dict(rb)


@router.get("/role-bindings")
async def list_role_bindings(
    request: Request,
    subject: str | None = None,
    org_id: str | None = None,
    scope_type: str | None = None,
    scope_id: str | None = None,
    include_revoked: bool = False,
    actor: Actor = Depends(require_scope("role_bindings:read")),
):
    async with _db(request).session() as s:
        q = (
            select(RoleBinding)
            .where(
                visible(
                    actor,
                    "role_bindings:read",
                    org=RoleBinding.org_id,
                    team=RoleBinding.team_id,
                    project=RoleBinding.project_id,
                )
            )
            .order_by(RoleBinding.created_at.desc())
        )
        if not include_revoked:
            q = q.where(RoleBinding.status == "active")
        if subject:
            q = q.where(RoleBinding.subject == subject)
        if org_id:
            q = q.where(RoleBinding.org_id == parse_uuid(org_id, "org_id"))
        if scope_type:
            q = q.where(RoleBinding.scope_type == scope_type)
        if scope_id:
            q = q.where(RoleBinding.scope_id == parse_uuid(scope_id, "scope_id"))
        return {"data": [to_dict(b) for b in (await s.execute(q)).scalars()]}


@router.post("/role-bindings/{binding_id}/revoke")
async def revoke_role_binding(
    binding_id: str, request: Request, actor: Actor = Depends(require_scope("role_bindings:write"))
):
    async with _db(request).tx() as s:
        rb = await get_or_404(s, RoleBinding, binding_id, "role_binding")
        ids = _binding_ids(rb)
        actor.require("role_bindings:write", **ids)
        if actor.max_rank(**ids) < ROLE_RANK[rb.role]:
            raise GatewayError(
                ErrorType.permission, f"You cannot revoke a {rb.role} binding here", code="rank_exceeded"
            )
        before = to_dict(rb)
        if rb.status != "revoked":
            rb.status, rb.revoked_at = "revoked", datetime.now(UTC)
        audit(s, actor, "role_binding.revoke", "role_binding", rb.id, before, to_dict(rb), rb.org_id)
        return to_dict(rb)


# ---- provider invoices (docs/spec/04 §10) --------------------------------------
# Provider bills span every tenant, so these routes are global: no delegated role includes `invoices:*`.


def _invoice_view(inv: Invoice, lines: list[InvoiceLine] | None = None) -> dict:
    d = to_dict(inv)
    if lines is not None:
        d["lines"] = [to_dict(line) for line in lines]
    return d


async def _store_invoice(s, actor: Actor, provider: str, period_start, period_end, currency, source, lines: list[dict]):
    if period_end < period_start:
        raise GatewayError(
            ErrorType.invalid_request, "period_end before period_start", code="invalid_period", param="period_end"
        )
    inv = Invoice(
        provider=provider,
        period_start=period_start,
        period_end=period_end,
        currency=currency.upper(),
        source=source,
        created_by=actor.id,
    )
    s.add(inv)
    await s.flush()
    seen: set[tuple[str, object]] = set()
    rows: list[InvoiceLine] = []
    for ln in lines:
        key = (ln["provider_model"], ln["day"])
        if key in seen:
            raise GatewayError(
                ErrorType.invalid_request,
                f"duplicate line for {ln['provider_model']} on {ln['day']}",
                code="duplicate_line",
                param="lines",
            )
        if not (period_start <= ln["day"] <= period_end):
            raise GatewayError(
                ErrorType.invalid_request,
                f"line day {ln['day']} outside the invoice period",
                code="line_outside_period",
                param="lines",
            )
        seen.add(key)
        rows.append(
            InvoiceLine(
                invoice_id=inv.id,
                provider_model=ln["provider_model"],
                day=ln["day"],
                amount=ln["amount"],
                prompt_tokens=ln.get("prompt_tokens"),
                completion_tokens=ln.get("completion_tokens"),
                meta=ln.get("meta") or {},
            )
        )
    s.add_all(rows)
    await s.flush()
    audit(s, actor, "invoice.create", "invoice", inv.id, after={**to_dict(inv), "lines": len(rows)})
    return inv, rows


@router.post("/invoices", status_code=201)
async def create_invoice(
    body: S.InvoiceCreate, request: Request, actor: Actor = Depends(require_scope("invoices:write"))
):
    actor.require("invoices:write")
    async with _db(request).tx() as s:
        inv, rows = await _store_invoice(
            s,
            actor,
            body.provider,
            body.period_start,
            body.period_end,
            body.currency,
            body.source,
            [ln.model_dump() for ln in body.lines],
        )
        return _invoice_view(inv, rows)


@router.post("/invoices/import", status_code=201)
async def import_invoice_csv(
    request: Request,
    provider: str = Query(...),
    period_start: date = Query(...),
    period_end: date = Query(...),
    currency: str = Query("USD", min_length=3, max_length=3),
    source: str | None = None,
    actor: Actor = Depends(require_scope("invoices:write")),
):
    """Body: CSV with columns provider_model, day, amount[, prompt_tokens, completion_tokens, …]."""
    actor.require("invoices:write")
    text = (await request.body()).decode("utf-8-sig", "replace")
    try:
        lines = R.parse_csv(text)
    except ValueError as exc:
        raise GatewayError(ErrorType.invalid_request, str(exc), code="invalid_csv") from None
    async with _db(request).tx() as s:
        inv, rows = await _store_invoice(s, actor, provider, period_start, period_end, currency, source, lines)
        return _invoice_view(inv, rows)


@router.get("/invoices")
async def list_invoices(
    request: Request,
    provider: str | None = None,
    status: str | None = None,
    limit: int = Query(50, le=200),
    actor: Actor = Depends(require_scope("invoices:read")),
):
    actor.require("invoices:read")
    async with _db(request).session() as s:
        q = select(Invoice).order_by(Invoice.period_start.desc(), Invoice.created_at.desc()).limit(limit)
        if provider:
            q = q.where(Invoice.provider == provider)
        if status:
            q = q.where(Invoice.status == status)
        return {"data": [_invoice_view(i) for i in (await s.execute(q)).scalars()]}


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: str, request: Request, actor: Actor = Depends(require_scope("invoices:read"))):
    actor.require("invoices:read")
    async with _db(request).session() as s:
        inv = await get_or_404(s, Invoice, invoice_id, "invoice")
        lines = list(
            (
                await s.execute(
                    select(InvoiceLine)
                    .where(InvoiceLine.invoice_id == inv.id)
                    .order_by(InvoiceLine.day, InvoiceLine.provider_model)
                )
            ).scalars()
        )
        return _invoice_view(inv, lines)


@router.post("/invoices/{invoice_id}/reconcile")
async def reconcile_invoice(
    invoice_id: str, body: S.ReconcileRequest, request: Request, actor: Actor = Depends(require_scope("invoices:write"))
):
    actor.require("invoices:write")
    async with _db(request).tx() as s:
        inv = await get_or_404(s, Invoice, invoice_id, "invoice")
        before = to_dict(inv)
        lines = list((await s.execute(select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id))).scalars())
        await R.reconcile(
            s,
            inv,
            lines,
            tolerance_pct=body.tolerance_pct,
            tolerance_abs=body.tolerance_abs,
            token_tolerance_pct=body.token_tolerance_pct,
        )
        audit(s, actor, "invoice.reconcile", "invoice", inv.id, before, to_dict(inv))
        return _invoice_view(inv, lines)


# ---- guardrail audit trail (docs/spec/04 §11) -----------------------------------


@router.get("/guardrail-events")
async def list_guardrail_events(
    request: Request,
    project_id: str | None = None,
    request_id: str | None = None,
    direction: str | None = Query(default=None, pattern="^(pre|post)$"),
    action: str | None = Query(default=None, pattern="^(block|redact|flag|error)$"),
    limit: int = Query(100, le=1000),
    actor: Actor = Depends(require_scope("guardrails:read")),
):
    async with _db(request).session() as s:
        q = (
            select(GuardrailEvent)
            .where(
                visible(
                    actor,
                    "guardrails:read",
                    org=GuardrailEvent.org_id,
                    team=GuardrailEvent.team_id,
                    project=GuardrailEvent.project_id,
                )
            )
            .order_by(GuardrailEvent.created_at.desc())
            .limit(limit)
        )
        if project_id:
            q = q.where(GuardrailEvent.project_id == parse_uuid(project_id, "project_id"))
        if request_id:
            q = q.where(GuardrailEvent.request_id == parse_uuid(request_id, "request_id"))
        if direction:
            q = q.where(GuardrailEvent.direction == direction)
        if action:
            q = q.where(GuardrailEvent.action == action)
        return {"data": [to_dict(g) for g in (await s.execute(q)).scalars()]}

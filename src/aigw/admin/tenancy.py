"""Delegated tenant roles: load an actor's grants and build the SQL predicates that keep a delegate inside its
tenants (docs/spec/01 §3.2)."""

from __future__ import annotations

import uuid
from dataclasses import replace

from sqlalchemy import ColumnElement, false, or_, select, true

from aigw.admin.auth import Actor, Grant
from aigw.admin.scim import deactivated_subject
from aigw.core.errors import ErrorType, GatewayError
from aigw.db.models import Budget, Project, RoleBinding, VirtualKey


async def load_grants(db, actor: Actor) -> Actor:
    """Attach the active role bindings whose subject equals any of the actor's identities."""
    if actor.type != "user" or not actor.identities:
        return actor
    async with db.session() as s:
        locked = await deactivated_subject(s, set(actor.identities))
        if locked:
            raise GatewayError(ErrorType.permission, f"User {locked} is deactivated (SCIM)", code="user_deactivated")
        rows = (
            await s.execute(
                select(RoleBinding).where(
                    RoleBinding.subject.in_(sorted(actor.identities)), RoleBinding.status == "active"
                )
            )
        ).scalars()
        grants = tuple(
            Grant(r.role, r.scope_type, r.org_id, r.team_id, r.project_id, r.id)
            for r in rows
            if r.role in {"org_owner", "team_owner", "project_member"}
        )
    return replace(actor, grants=grants)


def _ids(actor: Actor, scope: str):
    """Ids from grants that include ``scope``: (org-level org ids, team ids, project ids, every org touched,
    parent teams of project grants)."""
    orgs: set[uuid.UUID] = set()
    teams: set[uuid.UUID] = set()
    projects: set[uuid.UUID] = set()
    touched: set[uuid.UUID] = set()
    parent_teams: set[uuid.UUID] = set()  # teams a project grant sits in (own-row visibility only)
    resource, _, action = scope.partition(":")
    for g in actor.grants:
        if not g.patterns & {"*", scope, f"*:{action}", f"{resource}:*"}:
            continue
        touched.add(g.org_id)
        if g.scope_type == "organization":
            orgs.add(g.org_id)
        elif g.scope_type == "team" and g.team_id:
            teams.add(g.team_id)
        elif g.scope_type == "project" and g.project_id:
            projects.add(g.project_id)
            if g.team_id:
                parent_teams.add(g.team_id)
    return orgs, teams, projects, touched, parent_teams


def visible(actor: Actor, scope: str, *, org=None, team=None, project=None, id_=None) -> ColumnElement:
    """Predicate restricting a query to rows the actor may read under ``scope``.

    ``org``/``team``/``project`` are the row's tenant columns (any may be omitted when the table lacks it);
    ``id_`` is the row's own id column when the table *is* the tenant object (organizations, teams, projects), so a
    team-level grant can see its own team row. Global scope → no restriction.
    """
    if actor.unrestricted(scope):
        return true()
    orgs, teams, projects, touched, parent_teams = _ids(actor, scope)
    clauses = []
    if org is not None and orgs:
        clauses.append(org.in_(orgs))
    if team is not None and teams:
        clauses.append(team.in_(teams))
    if project is not None and projects:
        clauses.append(project.in_(projects))
    if id_ is not None:
        # the tenant object itself: an org row for org grants, a team row for team grants, ...
        own = {"organizations": touched, "teams": teams | parent_teams, "projects": projects}
        if org is None and id_.table.name == "organizations":
            clauses.append(id_.in_(own["organizations"]) if own["organizations"] else false())
        elif id_.table.name in own and own[id_.table.name]:
            clauses.append(id_.in_(own[id_.table.name]))
    return or_(*clauses) if clauses else false()


def visible_budgets(actor: Actor, scope: str = "budgets:read") -> ColumnElement:
    """Budgets carry only org_id; team/project/key scopes resolve through scope_type/scope_id."""
    if actor.unrestricted(scope):
        return true()
    orgs, teams, projects, _, _ = _ids(actor, scope)
    clauses = []
    if orgs:
        clauses.append(Budget.org_id.in_(orgs))
    if teams:
        clauses.append((Budget.scope_type == "team") & Budget.scope_id.in_(teams))
        clauses.append(
            (Budget.scope_type == "project") & Budget.scope_id.in_(select(Project.id).where(Project.team_id.in_(teams)))
        )
    if projects:
        clauses.append((Budget.scope_type == "project") & Budget.scope_id.in_(projects))
    key_filter = []
    if teams:
        key_filter.append(VirtualKey.team_id.in_(teams))
    if projects:
        key_filter.append(VirtualKey.project_id.in_(projects))
    if key_filter:
        clauses.append(
            (Budget.scope_type == "key") & Budget.scope_id.in_(select(VirtualKey.id).where(or_(*key_filter)))
        )
    return or_(*clauses) if clauses else false()


def budget_target_ids(target, scope_type: str) -> dict:
    """Tenant ids of a budget's scope object, for ``actor.require``."""
    if scope_type == "organization":
        return {"org_id": target.id}
    if scope_type == "team":
        return {"org_id": target.org_id, "team_id": target.id}
    if scope_type == "project":
        return {"org_id": target.org_id, "team_id": target.team_id, "project_id": target.id}
    return {"org_id": target.org_id, "team_id": target.team_id, "project_id": target.project_id}

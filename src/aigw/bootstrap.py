"""Declarative bootstrap and restore verification (used by `aigw bootstrap` / `aigw verify-restore`)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from aigw.config import get_settings
from aigw.db.models import (
    Budget,
    ConfigVersion,
    Deployment,
    Model,
    Organization,
    Price,
    Project,
    RequestAttempt,
    Team,
    UsageEvent,
    VirtualKey,
)
from aigw.db.session import Database
from aigw.gateway.auth import generate_key


async def apply(spec: dict[str, Any], db: Database | None = None) -> dict[str, Any]:
    db = db or Database(get_settings().database_url)
    out: dict[str, Any] = {}
    async with db.tx() as s:
        for p in spec.get("prices", []):
            exists = (
                await s.execute(
                    select(Price).where(Price.provider == p["provider"], Price.provider_model == p["provider_model"])
                )
            ).first()
            if not exists:
                s.add(
                    Price(
                        provider=p["provider"],
                        provider_model=p["provider_model"],
                        version=1,
                        input_per_million=Decimal(str(p.get("input_per_million", 0))),
                        output_per_million=Decimal(str(p.get("output_per_million", 0))),
                        source=p.get("source", "bootstrap"),
                    )
                )
        for m in spec.get("models", []):
            model = (
                await s.execute(select(Model).where(Model.org_id.is_(None), Model.name == m["name"]))
            ).scalar_one_or_none()
            if not model:
                model = Model(
                    name=m["name"],
                    display_name=m.get("display_name"),
                    modalities=m.get("modalities", ["chat"]),
                    context_window=m.get("context_window"),
                    supports_tools=m.get("supports_tools", False),
                    supports_json_schema=m.get("supports_json_schema", False),
                    supports_vision=m.get("supports_vision", False),
                )
                s.add(model)
                await s.flush()
            for d in m.get("deployments", []):
                exists = (
                    await s.execute(
                        select(Deployment).where(Deployment.model_id == model.id, Deployment.name == d["name"])
                    )
                ).first()
                if not exists:
                    s.add(
                        Deployment(
                            model_id=model.id,
                            name=d["name"],
                            provider=d["provider"],
                            provider_model=d["provider_model"],
                            base_url=d.get("base_url"),
                            credential_ref=d.get("credential_ref", "none"),
                            weight=d.get("weight", 1),
                            priority=d.get("priority", 0),
                            capabilities=d.get("capabilities", {}),
                            max_input_tokens=d.get("max_input_tokens"),
                            region=d.get("region"),
                        )
                    )
            out.setdefault("models", []).append(m["name"])
        org_spec = spec.get("organization")
        if org_spec:
            org = (
                await s.execute(select(Organization).where(Organization.slug == org_spec["slug"]))
            ).scalar_one_or_none()
            if not org:
                org = Organization(name=org_spec["name"], slug=org_spec["slug"])
                s.add(org)
                await s.flush()
            out["organization_id"] = str(org.id)
            team_spec = org_spec.get("team") or {"name": "default"}
            team = (
                await s.execute(select(Team).where(Team.org_id == org.id, Team.name == team_spec["name"]))
            ).scalar_one_or_none()
            if not team:
                team = Team(org_id=org.id, name=team_spec["name"])
                s.add(team)
                await s.flush()
            out["team_id"] = str(team.id)
            proj_spec = team_spec.get("project") or {"name": "default"}
            proj = (
                await s.execute(select(Project).where(Project.team_id == team.id, Project.name == proj_spec["name"]))
            ).scalar_one_or_none()
            if not proj:
                proj = Project(
                    org_id=org.id, team_id=team.id, name=proj_spec["name"], settings=proj_spec.get("settings", {})
                )
                s.add(proj)
                await s.flush()
            out["project_id"] = str(proj.id)
            if "budget" in proj_spec:
                b = proj_spec["budget"]
                exists = (
                    await s.execute(
                        select(Budget).where(
                            Budget.scope_type == "project",
                            Budget.scope_id == proj.id,
                            Budget.period == b.get("period", "total"),
                        )
                    )
                ).first()
                if not exists:
                    s.add(
                        Budget(
                            org_id=org.id,
                            scope_type="project",
                            scope_id=proj.id,
                            period=b.get("period", "total"),
                            limit_amount=Decimal(str(b["limit"])),
                            soft_alert_pct=b.get("soft_alert_pct"),
                            period_start=datetime.now(UTC),
                        )
                    )
            if proj_spec.get("key"):
                plaintext, key_hash, prefix = generate_key()
                k = VirtualKey(
                    org_id=org.id,
                    team_id=team.id,
                    project_id=proj.id,
                    name=proj_spec["key"].get("name", "bootstrap"),
                    key_prefix=prefix,
                    key_hash=key_hash,
                    rpm_limit=proj_spec["key"].get("rpm_limit"),
                    tpm_limit=proj_spec["key"].get("tpm_limit"),
                )
                s.add(k)
                await s.flush()
                out["key_id"] = str(k.id)
                out["key"] = plaintext
        s.add(ConfigVersion(reason="bootstrap"))
    return out


async def add_price(provider: str, provider_model: str, inp: Decimal, outp: Decimal, source: str) -> str:
    db = Database(get_settings().database_url)
    async with db.tx() as s:
        latest = (
            await s.execute(
                select(func.max(Price.version)).where(
                    Price.provider == provider, Price.provider_model == provider_model
                )
            )
        ).scalar()
        p = Price(
            provider=provider,
            provider_model=provider_model,
            version=(latest or 0) + 1,
            input_per_million=inp,
            output_per_million=outp,
            source=source,
        )
        s.add(p)
        s.add(ConfigVersion(reason=f"price {provider}/{provider_model}"))
        await s.flush()
        return f"price {p.provider}/{p.provider_model} v{p.version} = {p.id}"


async def verify(db: Database | None = None) -> list[str]:
    """docs/spec/05 §6: Σ settled usage per scope equals budget.spent (for total budgets); no stale pending."""
    db = db or Database(get_settings().database_url)
    problems: list[str] = []
    async with db.session() as s:
        budgets = list((await s.execute(select(Budget).where(Budget.period == "total"))).scalars())
        col = {
            "organization": UsageEvent.org_id,
            "team": UsageEvent.team_id,
            "project": UsageEvent.project_id,
            "key": UsageEvent.key_id,
        }
        for b in budgets:
            total = (
                await s.execute(
                    select(func.coalesce(func.sum(UsageEvent.cost), 0)).where(
                        col[b.scope_type] == b.scope_id, UsageEvent.created_at >= b.period_start
                    )
                )
            ).scalar_one()
            if Decimal(total) != Decimal(b.spent_amount):
                problems.append(f"budget {b.id} ({b.scope_type}) spent={b.spent_amount} but usage sum={total}")
        pending = (await s.execute(select(func.count()).where(RequestAttempt.status == "pending"))).scalar_one()
        reserved = (await s.execute(select(func.coalesce(func.sum(Budget.reserved_amount), 0)))).scalar_one()
        if pending == 0 and Decimal(reserved) != 0:
            problems.append(f"no pending attempts but reserved_amount total is {reserved}")
        n_models = (await s.execute(select(func.count()).select_from(Model))).scalar_one()
        if n_models == 0:
            problems.append("no models registered")
    return problems

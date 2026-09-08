"""Config snapshot loaded from PostgreSQL and refreshed on config_version change (docs/spec/01 §4).

Budgets and spend are deliberately NOT in the snapshot — they are always checked transactionally.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from aigw.adapters.base import DeploymentConfig
from aigw.core.pricing import PriceCard
from aigw.db.models import ConfigVersion, Deployment, Model, Price, Project, VirtualKey
from aigw.db.session import Database
from aigw.gateway.cache import CachePolicy
from aigw.gateway.guardrails import GuardrailPolicy

log = logging.getLogger(__name__)


@dataclass
class KeyScope:
    key_id: str
    org_id: str
    team_id: str
    project_id: str
    status: str
    expires_at: float | None
    grace_until: float | None
    allowed_models: list[str] | None
    rpm_limit: int | None
    tpm_limit: int | None
    allowed_tags: list[str] | None  # from project settings
    project_rpm_limit: int | None = None
    project_tpm_limit: int | None = None
    cache_ttl_seconds: int = 0  # projects.settings.cache (docs/spec/04 §9); 0 = cache off
    cache_deterministic_only: bool = False
    guardrails: Any = None  # GuardrailPolicy from projects.settings.guardrails (docs/spec/04 §11)
    guardrails_error: str | None = None  # invalid policy → the project fails closed
    cache_ttl_seconds: int = 0  # projects.settings.cache (docs/spec/04 §9); 0 = cache off
    cache_deterministic_only: bool = False


@dataclass
class ModelInfo:
    id: str
    org_id: str | None
    name: str
    display_name: str | None
    modalities: list[str]
    context_window: int | None
    supports_tools: bool
    supports_json_schema: bool
    supports_vision: bool
    deployments: list[DeploymentConfig] = field(default_factory=list)


@dataclass
class Snapshot:
    version: int
    loaded_at: float
    keys_by_hash: dict[str, KeyScope]
    models: dict[tuple[str | None, str], ModelInfo]  # (org_id or None, name) -> model
    prices: dict[tuple[str, str], PriceCard]

    def resolve_model(self, org_id: str, name: str) -> ModelInfo | None:
        return self.models.get((org_id, name)) or self.models.get((None, name))

    def models_for(self, scope: KeyScope) -> list[ModelInfo]:
        out = []
        for (org, _name), m in self.models.items():
            if org not in (None, scope.org_id):
                continue
            if scope.allowed_models is not None and m.name not in scope.allowed_models:
                continue
            out.append(m)
        return sorted(out, key=lambda m: m.name)

    def price_for(self, provider: str, provider_model: str) -> PriceCard:
        return self.prices.get((provider, provider_model)) or PriceCard.zero(provider, provider_model)


class SnapshotStore:
    def __init__(self, db: Database, refresh_seconds: float = 5.0, max_staleness: float = 300.0):
        self.db = db
        self.refresh_seconds = refresh_seconds
        self.max_staleness = max_staleness
        self._snapshot: Snapshot | None = None
        self._task: asyncio.Task | None = None
        self._revoked_hashes: set[str] = set()  # fast-path invalidation between refreshes

    @property
    def current(self) -> Snapshot | None:
        return self._snapshot

    def is_stale(self) -> bool:
        return self._snapshot is None or (time.time() - self._snapshot.loaded_at) > self.max_staleness

    def invalidate_key(self, key_hash: str) -> None:
        self._revoked_hashes.add(key_hash)

    def lookup_key(self, key_hash: str) -> KeyScope | None:
        if key_hash in self._revoked_hashes or self._snapshot is None:
            return None
        return self._snapshot.keys_by_hash.get(key_hash)

    async def start(self) -> None:
        await self.refresh(force=True)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_seconds)
            try:
                await self.refresh()
            except Exception as exc:  # keep serving the last snapshot within the staleness bound
                log.warning("snapshot refresh failed: %s", exc)

    async def refresh(self, force: bool = False) -> None:
        async with self.db.session() as s:
            version = (await s.execute(select(func.coalesce(func.max(ConfigVersion.id), 0)))).scalar_one()
            if not force and self._snapshot and version == self._snapshot.version:
                self._snapshot.loaded_at = time.time()
                return
            self._snapshot = await self._load(s, int(version))
            self._revoked_hashes.clear()
            log.info(
                "config snapshot loaded: version=%s keys=%d models=%d",
                version,
                len(self._snapshot.keys_by_hash),
                len(self._snapshot.models),
            )

    async def _load(self, s, version: int) -> Snapshot:
        projects = {str(p.id): p for p in (await s.execute(select(Project))).scalars()}
        keys: dict[str, KeyScope] = {}
        for k in (await s.execute(select(VirtualKey).where(VirtualKey.status != "expired"))).scalars():
            proj = projects.get(str(k.project_id))
            settings = (proj.settings if proj else {}) or {}
            keys[k.key_hash] = KeyScope(
                key_id=str(k.id),
                org_id=str(k.org_id),
                team_id=str(k.team_id),
                project_id=str(k.project_id),
                status=k.status,
                expires_at=k.expires_at.timestamp() if k.expires_at else None,
                grace_until=k.grace_until.timestamp() if k.grace_until else None,
                allowed_models=list(k.allowed_models) if k.allowed_models is not None else None,
                rpm_limit=k.rpm_limit,
                tpm_limit=k.tpm_limit,
                allowed_tags=settings.get("allowed_tags"),
                cache_ttl_seconds=(cache.ttl_seconds if (cache := CachePolicy.from_project_settings(settings)) else 0),
                cache_deterministic_only=bool(cache and cache.deterministic_only),
                **_guardrails(settings),
                project_rpm_limit=settings.get("rpm_limit"),
                project_tpm_limit=settings.get("tpm_limit"),
            )
        models: dict[tuple[str | None, str], ModelInfo] = {}
        by_id: dict[str, ModelInfo] = {}
        for m in (await s.execute(select(Model).where(Model.status == "active"))).scalars():
            info = ModelInfo(
                id=str(m.id),
                org_id=str(m.org_id) if m.org_id else None,
                name=m.name,
                display_name=m.display_name,
                modalities=list(m.modalities or []),
                context_window=m.context_window,
                supports_tools=m.supports_tools,
                supports_json_schema=m.supports_json_schema,
                supports_vision=m.supports_vision,
            )
            models[(info.org_id, m.name)] = info
            by_id[info.id] = info
        for d in (await s.execute(select(Deployment).where(Deployment.status == "active"))).scalars():
            info = by_id.get(str(d.model_id))
            if not info:
                continue
            info.deployments.append(
                DeploymentConfig(
                    id=str(d.id),
                    model_id=info.id,
                    model_name=info.name,
                    name=d.name,
                    provider=d.provider,
                    provider_model=d.provider_model,
                    base_url=d.base_url,
                    credential_ref=d.credential_ref,
                    weight=d.weight,
                    priority=d.priority,
                    capabilities=dict(d.capabilities or {}),
                    extra_headers=dict(d.extra_headers or {}),
                    timeout_seconds=d.timeout_seconds,
                    max_input_tokens=d.max_input_tokens,
                    region=d.region,
                    cooldown_until=d.cooldown_until.timestamp() if d.cooldown_until else None,
                )
            )
        prices: dict[tuple[str, str], PriceCard] = {}
        rows = (
            await s.execute(
                select(Price)
                .where(
                    Price.effective_from <= datetime.now(UTC)
                )  # app clock, same source that stamps effective_from  # noqa: E501
                .order_by(Price.provider, Price.provider_model, Price.version)
            )
        ).scalars()
        for p in rows:  # ascending version → last wins
            prices[(p.provider, p.provider_model)] = PriceCard(
                id=str(p.id),
                provider=p.provider,
                provider_model=p.provider_model,
                version=p.version,
                input_per_million=Decimal(p.input_per_million),
                output_per_million=Decimal(p.output_per_million),
                cached_input_per_million=Decimal(p.cached_input_per_million)
                if p.cached_input_per_million is not None
                else None,
                reasoning_per_million=Decimal(p.reasoning_per_million) if p.reasoning_per_million is not None else None,
            )
        return Snapshot(version=version, loaded_at=time.time(), keys_by_hash=keys, models=models, prices=prices)


def _guardrails(settings: dict) -> dict:
    try:
        return {"guardrails": GuardrailPolicy.from_project_settings(settings), "guardrails_error": None}
    except ValueError as exc:
        log.warning("invalid guardrail policy: %s", exc)
        return {"guardrails": None, "guardrails_error": str(exc)}

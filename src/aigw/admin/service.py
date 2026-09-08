"""Control-plane helpers: serialization, audit, config version bumps, invalidation publish."""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aigw.admin.auth import Actor
from aigw.core.errors import ErrorType, GatewayError
from aigw.db.models import AuditEvent, ConfigVersion

log = logging.getLogger(__name__)

_HIDDEN = {"key_hash"}


def to_dict(obj: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in obj.__table__.columns:
        name = col.name
        if name in _HIDDEN:
            continue
        val = getattr(obj, col.key if col.key != "metadata" else "metadata_", None)
        if col.key == "metadata":
            val = obj.metadata_
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime):
            val = val.isoformat()
        elif isinstance(val, date):
            val = val.isoformat()
        elif isinstance(val, Decimal):
            val = format(val, "f")
        out[name] = val
    return out


def parse_uuid(value: str, what: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError):
        raise GatewayError(ErrorType.invalid_request, f"Invalid {what}", code="invalid_id", param=what) from None


async def get_or_404(s: AsyncSession, model, id_: str, what: str):
    obj = await s.get(model, parse_uuid(id_, f"{what}_id"))
    if obj is None:
        raise GatewayError(ErrorType.not_found, f"{what} not found", code=f"{what}_not_found")
    return obj


def audit(
    s: AsyncSession,
    actor: Actor,
    action: str,
    target_type: str,
    target_id: uuid.UUID | None,
    before: dict | None = None,
    after: dict | None = None,
    org_id: uuid.UUID | None = None,
) -> None:
    s.add(
        AuditEvent(
            org_id=org_id,
            actor_type=actor.type,
            actor_id=actor.id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before=before,
            after=after,
        )
    )


def bump_config(s: AsyncSession, reason: str) -> None:
    s.add(ConfigVersion(reason=reason[:200]))


async def current_config_version(s: AsyncSession) -> int:
    from sqlalchemy import func

    return int((await s.execute(select(func.coalesce(func.max(ConfigVersion.id), 0)))).scalar_one())


async def publish_key_invalidation(valkey, key_hash: str) -> None:
    if valkey is None:
        return
    try:
        await valkey.publish("aigw:invalidate:key", key_hash)
    except Exception as exc:
        log.warning("invalidation publish failed: %s", exc)

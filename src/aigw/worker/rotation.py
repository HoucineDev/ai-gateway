"""Scheduled key rotation (docs/spec/01 §3.2 keys): the worker rotates virtual keys whose `rotate_every_seconds`
has elapsed, keeps the previous key valid for `rotation_grace_seconds`, and parks the new plaintext for a single
audited pickup.

Nobody is present to copy a plaintext when a schedule fires, and the gateway never stores plaintexts. The new
plaintext is therefore encrypted with `AIGW_KEY_PICKUP_SECRET` (a Fernet key) into `key_pickups` with a TTL;
`POST /admin/v1/keys/{id}/pickup` returns it exactly once and deletes the row. Without the secret the schedule is
skipped with a warning rather than rotating keys nobody can retrieve.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select, text

from aigw.db.models import AuditEvent, ConfigVersion, KeyPickup, Outbox, VirtualKey
from aigw.gateway.auth import generate_key

log = logging.getLogger("aigw.worker.rotation")
WORKER_ACTOR = ("worker", "rotation")


def fernet(secret: str | None) -> Fernet | None:
    if not secret:
        return None
    try:
        return Fernet(secret.encode() if isinstance(secret, str) else secret)
    except (ValueError, TypeError) as exc:
        log.error("AIGW_KEY_PICKUP_SECRET is not a valid Fernet key: %s", exc)
        return None


def decrypt_pickup(secret: str | None, ciphertext: bytes) -> str | None:
    f = fernet(secret)
    if f is None:
        return None
    try:
        return f.decrypt(ciphertext).decode()
    except InvalidToken:
        return None


async def rotate_due_keys(db, settings, now: datetime | None = None) -> int:
    """Rotate every active key whose schedule elapsed. Returns the number rotated."""
    f = fernet(settings.key_pickup_secret)
    now = now or datetime.now(UTC)
    async with db.tx() as s:
        await s.execute(delete(KeyPickup).where(KeyPickup.expires_at < now))  # expired, never picked up
        due = list(
            (
                await s.execute(
                    select(VirtualKey)
                    .where(
                        VirtualKey.status == "active",
                        VirtualKey.rotate_every_seconds.is_not(None),
                        VirtualKey.created_at + text("make_interval(secs => rotate_every_seconds)") < now,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalars()
        )
        if not due:
            return 0
        if f is None:
            log.warning("%d key(s) due for rotation but AIGW_KEY_PICKUP_SECRET is not set: skipping", len(due))
            return 0
        rotated = 0
        for old in due:
            plaintext, key_hash, prefix = generate_key()
            grace = int(old.rotation_grace_seconds or 3600)
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
                rotate_every_seconds=old.rotate_every_seconds,
                rotation_grace_seconds=old.rotation_grace_seconds,
            )
            old.status, old.revoked_at = "revoked", now
            old.grace_until = now + timedelta(seconds=grace)
            s.add(new)
            await s.flush()
            expires = now + timedelta(seconds=int(settings.key_pickup_ttl_seconds))
            s.add(KeyPickup(key_id=new.id, ciphertext=f.encrypt(plaintext.encode()), expires_at=expires))
            s.add(
                AuditEvent(
                    org_id=old.org_id,
                    actor_type=WORKER_ACTOR[0],
                    actor_id=WORKER_ACTOR[1],
                    action="key.rotate.scheduled",
                    target_type="key",
                    target_id=old.id,
                    after={
                        "new_key_id": str(new.id),
                        "grace_until": old.grace_until.isoformat(),
                        "pickup_expires_at": expires.isoformat(),
                    },
                )
            )
            s.add(
                Outbox(
                    topic="key.rotated",
                    payload={
                        "old_key_id": str(old.id),
                        "new_key_id": str(new.id),
                        "project_id": str(old.project_id),
                        "name": old.name,
                        "grace_until": old.grace_until.isoformat(),
                        "pickup_expires_at": expires.isoformat(),
                    },
                )
            )
            rotated += 1
            log.info("rotated key %s (%s) -> %s; grace %ss; pickup until %s", old.id, old.name, new.id, grace, expires)
        s.add(ConfigVersion(reason=f"worker.rotate_keys {rotated}"))
    return rotated


async def next_rotation(key: VirtualKey) -> datetime | None:
    if key.status != "active" or not key.rotate_every_seconds:
        return None
    return key.created_at + timedelta(seconds=int(key.rotate_every_seconds))


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False

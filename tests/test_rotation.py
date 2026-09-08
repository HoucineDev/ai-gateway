"""Scheduled key rotation (docs/spec/01 §3.2): schedule on create/patch, worker rotation with grace, inherited
schedule, sealed one-time pickup, missing-secret safety, audit and outbox trail."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from sqlalchemy import text

from aigw.config import Settings
from aigw.worker.main import Worker
from aigw.worker.rotation import rotate_due_keys
from tests.conftest import ADMIN, TEST_DB, TEST_VALKEY, refresh

SECRET = Fernet.generate_key().decode()


def rotation_settings(secret: str | None = SECRET, **kw) -> Settings:
    return Settings(
        database_url=TEST_DB, valkey_url=TEST_VALKEY, admin_key="test-admin", key_pickup_secret=secret, **kw
    )


async def _age(db, key_id: str, seconds: int) -> None:
    """Pretend the key was created `seconds` ago."""
    async with db.tx() as s:
        await s.execute(
            text("UPDATE virtual_keys SET created_at = now() - make_interval(secs => :s) WHERE id = :id"),
            {"s": seconds, "id": key_id},
        )


async def _chat(client, key: str):
    return await client.post(
        "/v1/chat/completions",
        json={"model": "local-chat", "messages": [{"role": "user", "content": "hi"}]},
        headers={"authorization": f"Bearer {key}"},
    )


async def test_schedule_on_create_and_patch(client, app, tenant):
    pid = tenant["project"]["id"]
    r = await client.post(
        f"/admin/v1/projects/{pid}/keys", json={"name": "sched", "rotate_every_seconds": 7200}, headers=ADMIN
    )
    assert r.status_code == 201, r.text
    k = (await client.get(f"/admin/v1/keys/{r.json()['id']}", headers=ADMIN)).json()
    assert k["rotate_every_seconds"] == 7200 and k["next_rotation_at"] and not k["pickup_available"]
    r2 = await client.patch(
        f"/admin/v1/keys/{k['id']}", json={"rotate_every_seconds": 86400, "rotation_grace_seconds": 60}, headers=ADMIN
    )
    assert (
        r2.status_code == 200
        and r2.json()["rotate_every_seconds"] == 86400
        and r2.json()["rotation_grace_seconds"] == 60
    )
    r3 = await client.patch(f"/admin/v1/keys/{k['id']}", json={"clear_schedule": True}, headers=ADMIN)
    assert r3.json()["rotate_every_seconds"] is None and r3.json()["next_rotation_at"] is None
    assert (
        await client.post(
            f"/admin/v1/projects/{pid}/keys", json={"name": "x", "rotate_every_seconds": 60}, headers=ADMIN
        )
    ).status_code == 422
    assert (await client.post(f"/admin/v1/keys/{k['id']}/pickup", headers=ADMIN)).status_code == 404


async def test_worker_rotates_due_keys_with_grace_and_sealed_pickup(client, app, db, tenant):
    pid = tenant["project"]["id"]
    created = (
        await client.post(
            f"/admin/v1/projects/{pid}/keys",
            json={"name": "svc", "rotate_every_seconds": 3600, "rotation_grace_seconds": 120, "rpm_limit": 42},
            headers=ADMIN,
        )
    ).json()
    await refresh(app)
    assert (await _chat(client, created["key"])).status_code == 200
    settings = rotation_settings()
    app.state.settings.key_pickup_secret = SECRET  # the admin role must hold the same secret as the worker
    assert await rotate_due_keys(db, settings) == 0  # not due yet
    await _age(db, created["id"], 3601)
    version = (await client.get("/admin/v1/config/version", headers=ADMIN)).json()["version"]
    assert await rotate_due_keys(db, settings) == 1
    assert (await client.get("/admin/v1/config/version", headers=ADMIN)).json()["version"] > version

    old = (await client.get(f"/admin/v1/keys/{created['id']}", headers=ADMIN)).json()
    assert old["status"] == "revoked" and old["grace_until"] and old["next_rotation_at"] is None
    keys = (await client.get(f"/admin/v1/projects/{pid}/keys", headers=ADMIN)).json()["data"]
    new = next(k for k in keys if k["rotated_from"] == created["id"])
    assert new["status"] == "active" and new["rotate_every_seconds"] == 3600 and new["rotation_grace_seconds"] == 120
    assert new["rpm_limit"] == 42 and new["name"] == "svc"
    view = (await client.get(f"/admin/v1/keys/{new['id']}", headers=ADMIN)).json()
    assert view["pickup_available"] and view["pickup_expires_at"]

    # the previous plaintext keeps working during the grace period
    await refresh(app)
    assert (await _chat(client, created["key"])).status_code == 200

    # pickup returns the new plaintext exactly once, audited, and it authenticates
    r = await client.post(f"/admin/v1/keys/{new['id']}/pickup", headers=ADMIN)
    assert r.status_code == 200, r.text
    plaintext = r.json()["key"]
    assert plaintext.startswith(new["key_prefix"]) and r.json()["pickup_available"] is False
    assert (await client.post(f"/admin/v1/keys/{new['id']}/pickup", headers=ADMIN)).status_code == 404
    assert (await _chat(client, plaintext)).status_code == 200
    audit = (await client.get("/admin/v1/audit", params={"target_type": "key"}, headers=ADMIN)).json()["data"]
    actions = {(a["action"], a["actor_type"]) for a in audit}
    assert ("key.rotate.scheduled", "worker") in actions and ("key.pickup", "admin_key") in actions

    # the outbox carries the event and the worker tick processes it
    w = Worker(db, settings)
    tick = await w.tick()
    assert tick["rotated_keys"] == 0 and tick["outbox"] >= 1

    # expired grace: the old key stops working once the worker expires it / the snapshot drops it
    async with db.tx() as s:
        await s.execute(
            text("UPDATE virtual_keys SET grace_until = now() - interval '1 minute' WHERE id = :id"),
            {"id": created["id"]},
        )
    await refresh(app)
    assert (await _chat(client, created["key"])).status_code == 401


async def test_missing_secret_skips_and_wrong_secret_cannot_decrypt(client, app, db, tenant):
    pid = tenant["project"]["id"]
    created = (
        await client.post(
            f"/admin/v1/projects/{pid}/keys", json={"name": "svc", "rotate_every_seconds": 3600}, headers=ADMIN
        )
    ).json()
    await _age(db, created["id"], 4000)
    assert await rotate_due_keys(db, rotation_settings(secret=None)) == 0
    assert (await client.get(f"/admin/v1/keys/{created['id']}", headers=ADMIN)).json()[
        "status"
    ] == "active"  # untouched
    assert await rotate_due_keys(db, rotation_settings(secret="not-a-fernet-key")) == 0
    assert await rotate_due_keys(db, rotation_settings()) == 1
    keys = (await client.get(f"/admin/v1/projects/{pid}/keys", headers=ADMIN)).json()["data"]
    new = next(k for k in keys if k["rotated_from"] == created["id"])
    # the admin role runs with a different (here: no) secret → cannot decrypt, never leaks
    r = await client.post(f"/admin/v1/keys/{new['id']}/pickup", headers=ADMIN)
    assert (r.status_code, r.json()["error"]["code"]) == (503, "pickup_undecryptable")
    app.state.settings.key_pickup_secret = SECRET
    try:
        assert (await client.post(f"/admin/v1/keys/{new['id']}/pickup", headers=ADMIN)).status_code == 200
    finally:
        app.state.settings.key_pickup_secret = None
    # expired pickups are purged on the next sweep
    created2 = (
        await client.post(
            f"/admin/v1/projects/{pid}/keys", json={"name": "svc2", "rotate_every_seconds": 3600}, headers=ADMIN
        )
    ).json()
    await _age(db, created2["id"], 4000)
    assert await rotate_due_keys(db, rotation_settings(key_pickup_ttl_seconds=1)) == 1
    keys = (await client.get(f"/admin/v1/projects/{pid}/keys", headers=ADMIN)).json()["data"]
    new2 = next(k for k in keys if k["rotated_from"] == created2["id"])
    await rotate_due_keys(db, rotation_settings(), now=datetime.now(UTC) + timedelta(seconds=5))
    assert (await client.get(f"/admin/v1/keys/{new2['id']}", headers=ADMIN)).json()["pickup_available"] is False

"""Worker role: outbox consumer, pending-attempt reconciliation, key expiry (docs/spec/05 §1)."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime

from sqlalchemy import select, update

from aigw.config import Settings, get_settings
from aigw.db.models import ConfigVersion, Outbox, VirtualKey
from aigw.db.session import Database
from aigw.gateway.accounting import Ledger

log = logging.getLogger("aigw.worker")


class Worker:
    def __init__(self, db: Database, settings: Settings, health=None):
        self.db = db
        self.settings = settings
        self.ledger = Ledger(db)
        self.health = health  # HealthChecker (docs/spec/04 §6) or None when disabled
        self._health_last = float("-inf")
        self.handlers = {"budget.soft_alert": self.on_soft_alert, "usage.settled": self.on_usage_settled}

    async def run_forever(self, interval: float = 5.0) -> None:
        log.info("worker started")
        while True:
            try:
                await self.tick()
            except Exception as exc:
                log.exception("worker tick failed: %s", exc)
            await asyncio.sleep(interval)

    async def tick(self) -> dict[str, int]:
        processed = await self.process_outbox()
        reconciled = await self.ledger.reconcile_pending(self.settings.attempt_timeout_seconds)
        expired = await self.expire_keys()
        if reconciled or expired:
            log.info("reconciled=%d expired_keys=%d", reconciled, expired)
        probed = await self.maybe_check_health()
        return {"outbox": processed, "reconciled": reconciled, "expired_keys": expired, "health_probed": probed}

    async def maybe_check_health(self) -> int:
        interval = float(self.settings.health_check_interval_seconds)
        if self.health is None or interval <= 0 or time.monotonic() - self._health_last < interval:
            return 0
        self._health_last = time.monotonic()
        statuses = await self.health.run_once()
        bad = [k for k, v in statuses.items() if v != "healthy"]
        if bad:
            log.info("health sweep: %d deployments, %d not healthy", len(statuses), len(bad))
        return len(statuses)

    async def process_outbox(self, batch: int = 200) -> int:
        n = 0
        async with self.db.tx() as s:
            rows = list(
                (
                    await s.execute(
                        select(Outbox)
                        .where(Outbox.processed_at.is_(None))
                        .order_by(Outbox.id)
                        .limit(batch)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            for row in rows:
                handler = self.handlers.get(row.topic)
                try:
                    if handler:
                        await handler(row.payload)
                    row.processed_at = datetime.now(UTC)
                    n += 1
                except Exception as exc:
                    row.attempts += 1
                    log.warning("outbox %s (%s) failed attempt %d: %s", row.id, row.topic, row.attempts, exc)
                    if row.attempts >= 10:
                        row.processed_at = datetime.now(UTC)  # dead-letter: stop retrying, keep the row
        return n

    async def expire_keys(self) -> int:
        async with self.db.tx() as s:
            res = await s.execute(
                update(VirtualKey)
                .where(VirtualKey.status == "active", VirtualKey.expires_at < datetime.now(UTC))
                .values(status="expired")
                .returning(VirtualKey.id)
            )
            ids = [r[0] for r in res.all()]
            if ids:
                s.add(ConfigVersion(reason=f"worker.expire_keys {len(ids)}"))
        return len(ids)

    # ---- handlers (delivery adapters are Phase 2; alpha logs) ------------
    async def on_soft_alert(self, payload: dict) -> None:
        log.warning(
            "BUDGET SOFT ALERT %s %s: spent %s of %s (%s%%)",
            payload.get("scope_type"),
            payload.get("scope_id"),
            payload.get("spent"),
            payload.get("limit"),
            payload.get("pct"),
        )

    async def on_usage_settled(self, payload: dict) -> None:
        return None  # hook for exporters (Phase 3 logging destinations)


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db = Database(settings.database_url)
    health = None
    http = None
    if settings.health_check_interval_seconds > 0:
        import httpx
        import redis.asyncio as redis

        from aigw.adapters.registry import AdapterRegistry
        from aigw.core.secrets import SecretResolver
        from aigw.gateway.ratelimit import CooldownStore
        from aigw.worker.health import HealthChecker

        http = httpx.AsyncClient(timeout=settings.health_check_timeout_seconds)
        valkey = (
            redis.from_url(settings.valkey_url, socket_connect_timeout=1, socket_timeout=1)
            if settings.valkey_url
            else None
        )
        health = HealthChecker(db, settings, AdapterRegistry(http), SecretResolver(), CooldownStore(valkey), http)
    try:
        await Worker(db, settings, health).run_forever()
    finally:
        if http is not None:
            await http.aclose()
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())

"""Application factory. One package, roles selected by AIGW_ROLE (docs/spec/05 §1)."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from aigw.adapters.registry import AdapterRegistry
from aigw.config import Settings, get_settings
from aigw.core.errors import GatewayError
from aigw.core.secrets import SecretResolver
from aigw.db.session import Database
from aigw.gateway import metrics
from aigw.gateway.accounting import Ledger
from aigw.gateway.ratelimit import CooldownStore, RateLimiter
from aigw.gateway.snapshot import SnapshotStore

log = logging.getLogger("aigw")


def create_app(
    settings: Settings | None = None,
    *,
    db: Database | None = None,
    upstream_client: httpx.AsyncClient | None = None,
    valkey: redis.Redis | None = None,
    secrets: SecretResolver | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.db = db or Database(settings.database_url)
        app.state.valkey = valkey
        if app.state.valkey is None and settings.valkey_url:
            app.state.valkey = redis.from_url(settings.valkey_url, socket_connect_timeout=1, socket_timeout=1)
        app.state.upstream = upstream_client or httpx.AsyncClient(
            limits=httpx.Limits(max_connections=500, max_keepalive_connections=100), http2=False
        )
        app.state.adapters = AdapterRegistry(
            app.state.upstream, settings.upstream_timeout_seconds, settings.upstream_connect_timeout_seconds
        )
        app.state.snapshots = SnapshotStore(
            app.state.db, settings.config_refresh_seconds, settings.config_max_staleness_seconds
        )
        app.state.ledger = Ledger(app.state.db)
        app.state.limiter = RateLimiter(app.state.valkey, settings.ratelimit_fail_mode)
        app.state.cooldowns = CooldownStore(app.state.valkey)
        app.state.secrets = secrets or SecretResolver()
        from aigw.gateway.pipeline import Pipeline

        app.state.pipeline = Pipeline(
            settings=settings,
            snapshots=app.state.snapshots,
            adapters=app.state.adapters,
            ledger=app.state.ledger,
            limiter=app.state.limiter,
            cooldowns=app.state.cooldowns,
            secrets=app.state.secrets,
        )
        if settings.role in ("gateway", "all"):
            await app.state.snapshots.start()
            app.state.invalidation = InvalidationListener(app.state.valkey, app.state.snapshots)
            await app.state.invalidation.start()
        try:
            yield
        finally:
            if settings.role in ("gateway", "all"):
                await app.state.invalidation.stop()
                await app.state.snapshots.stop()
            if upstream_client is None:
                await app.state.upstream.aclose()
            if db is None:
                await app.state.db.dispose()

    app = FastAPI(
        title="Independent AI Gateway",
        version="0.1.0a1",
        lifespan=lifespan,
        docs_url="/admin/docs" if settings.role in ("admin", "all") else None,
    )

    @app.exception_handler(GatewayError)
    async def _gw_error(request: Request, exc: GatewayError):
        rid = request.headers.get("x-aigw-request-id")
        return JSONResponse(exc.envelope(rid), status_code=exc.status_code, headers=exc.headers)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "role": settings.role}

    @app.get("/readyz")
    async def readyz(request: Request):
        st = request.app.state
        if settings.role in ("gateway", "all"):
            if st.snapshots.current is None or st.snapshots.is_stale():
                return JSONResponse({"status": "not_ready", "reason": "config"}, status_code=503)
            metrics.CONFIG_VERSION.set(st.snapshots.current.version)
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics_endpoint(request: Request):
        st = request.app.state
        metrics.RATELIMIT_DEGRADED.set(1 if st.limiter.degraded else 0)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    if settings.role in ("gateway", "all"):
        from aigw.gateway.routes import router as gateway_router

        app.include_router(gateway_router)
    if settings.role in ("admin", "all"):
        from aigw.admin.routes import router as admin_router

        app.include_router(admin_router)
    return app


class InvalidationListener:
    """Subscribes to Valkey channel aigw:invalidate:key so revocations land within ~1 s (docs/spec/01 §4)."""

    CHANNEL = "aigw:invalidate:key"

    def __init__(self, client: redis.Redis | None, snapshots: SnapshotStore):
        self.client = client
        self.snapshots = snapshots
        self._task = None
        self._pubsub = None

    async def start(self) -> None:
        if not self.client:
            return
        import asyncio

        try:
            self._pubsub = self.client.pubsub()
            await self._pubsub.subscribe(self.CHANNEL)
        except Exception as exc:  # Valkey optional for correctness; refresh loop still catches up
            log.warning("invalidation listener disabled: %s", exc)
            self._pubsub = None
            return
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        try:
            async for msg in self._pubsub.listen():
                if msg.get("type") == "message":
                    data = msg["data"]
                    key_hash = data.decode() if isinstance(data, bytes) else str(data)
                    self.snapshots.invalidate_key(key_hash)
        except Exception as exc:
            log.warning("invalidation listener stopped: %s", exc)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._pubsub:
            try:
                await self._pubsub.aclose()
            except Exception:
                pass


app = None


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app

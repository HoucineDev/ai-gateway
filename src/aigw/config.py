"""Runtime configuration (all variables prefixed AIGW_). See docs/spec/05-deployment.md §4."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIGW_", env_file=".env", extra="ignore")

    role: Literal["gateway", "admin", "worker", "all"] = "all"
    database_url: str = "postgresql+asyncpg://aigw:aigw@localhost:5432/aigw"
    valkey_url: str = "redis://localhost:6379/0"
    admin_key: str | None = None
    # Keycloak OIDC bearer tokens on the control API (docs/spec/01 §3.1, docs/spec/05 §4)
    oidc_issuer: str | None = None  # e.g. https://keycloak.example/realms/aigw
    oidc_audience: str | None = None  # expected `aud`; unset = audience not verified
    oidc_client_id: str | None = None  # Keycloak client whose `resource_access` roles count (default: audience)
    oidc_jwks_url: str | None = None  # default: {issuer}/protocol/openid-connect/certs
    oidc_leeway_seconds: float = 30.0
    oidc_jwks_cache_seconds: float = 3600.0
    oidc_jwks_min_refresh_seconds: float = 30.0  # bound on refetches triggered by unknown `kid`
    oidc_role_scopes: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "aigw-admin": ["*"],
            "aigw-operator": ["*:read", "models:write", "deployments:write", "prices:write", "budgets:write"],
            "aigw-viewer": ["*:read"],
        }
    )

    # Active health checks (worker; docs/spec/04 §6). 0 disables.
    health_check_interval_seconds: float = 30.0
    health_check_timeout_seconds: float = 5.0
    health_failure_threshold: int = 2  # consecutive failed probes before the deployment is cooled down
    health_cooldown_seconds: float = 90.0  # refreshed each sweep while unhealthy; cleared on recovery

    # Routing (docs/spec/04 §5.1): weighted = configured weights only; adaptive = weights scaled by latency EWMA,
    # vLLM queue pressure and in-flight load. References are the load at which a signal halves a candidate's weight.
    routing_strategy: Literal["weighted", "adaptive"] = "adaptive"
    routing_ewma_alpha: float = 0.2
    routing_latency_ref_ms: float = 1000.0
    routing_queue_ref: float = 8.0
    routing_inflight_ref: float = 4.0

    # Routing (docs/spec/04 §5.1): weighted = configured weights only; adaptive = weights scaled by latency EWMA,
    # vLLM queue pressure and in-flight load. References are the load at which a signal halves a candidate's weight.
    routing_strategy: Literal["weighted", "adaptive"] = "adaptive"
    routing_ewma_alpha: float = 0.2
    routing_latency_ref_ms: float = 1000.0
    routing_queue_ref: float = 8.0
    routing_inflight_ref: float = 4.0

    # Routing (docs/spec/04 §5.1): weighted = configured weights only; adaptive = weights scaled by latency EWMA,
    # vLLM queue pressure and in-flight load. References are the load at which a signal halves a candidate's weight.
    routing_strategy: Literal["weighted", "adaptive"] = "adaptive"
    routing_ewma_alpha: float = 0.2
    routing_latency_ref_ms: float = 1000.0
    routing_queue_ref: float = 8.0
    routing_inflight_ref: float = 4.0

    # Scheduled key rotation (docs/spec/01 §3.2): Fernet key that seals a rotated key's plaintext until pickup.
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    key_pickup_secret: str | None = None
    key_pickup_ttl_seconds: int = 86_400

    cache_max_entry_bytes: int = 262_144  # exact response cache (docs/spec/04 §9): larger answers are not stored

    cache_max_entry_bytes: int = 262_144  # exact response cache (docs/spec/04 §9): larger answers are not stored

    config_refresh_seconds: float = 5.0
    config_max_staleness_seconds: float = 300.0
    ratelimit_fail_mode: Literal["open", "closed"] = "open"
    max_attempts: int = 3
    attempt_timeout_seconds: int = 900
    default_max_output_tokens: int = 4096
    upstream_timeout_seconds: float = 120.0
    upstream_connect_timeout_seconds: float = 10.0
    log_content: bool = False
    ledger_currency: str = "USD"

    otel_exporter_otlp_endpoint: str | None = Field(default=None)
    cors_origins: str = ""  # comma-separated origins allowed on the control API (portal dev server)
    portal_dir: str | None = None  # built portal (portal/dist) served by the admin role at /


@lru_cache
def get_settings() -> Settings:
    return Settings()

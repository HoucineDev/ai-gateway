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
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_admin_role: str = "aigw-admin"

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

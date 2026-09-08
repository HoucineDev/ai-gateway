"""SQLAlchemy models. Schema documented in docs/spec/02-data-model.md."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aigw.core.ids import uuid7

MONEY = Numeric(20, 8)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[str]: ARRAY(Text)}


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ---- tenancy ---------------------------------------------------------------


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created()


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("org_id", "name"),)
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created()


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("team_id", "name"),)
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active")
    # settings.allowed_tags: list[str] — validated allocation tags for request.metadata
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created()


class RoleBinding(Base):
    """Delegated tenant role (docs/spec/01 §3.2). Rows are revoked, never deleted, so the audit trail stays whole."""

    __tablename__ = "role_bindings"
    __table_args__ = (
        UniqueConstraint("subject", "role", "scope_type", "scope_id"),
        Index("ix_role_bindings_subject_status", "subject", "status"),
    )
    id: Mapped[uuid.UUID] = _pk()
    subject: Mapped[str] = mapped_column(String(320))  # Keycloak sub, preferred_username or email
    subject_kind: Mapped[str] = mapped_column(String(20), default="user")  # user | service_account
    role: Mapped[str] = mapped_column(String(30))  # org_owner | team_owner | project_member
    scope_type: Mapped[str] = mapped_column(String(20))  # organization | team | project
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    team_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | revoked
    created_by: Mapped[str] = mapped_column(String(200))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class VirtualKey(Base):
    __tablename__ = "virtual_keys"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    key_prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|revoked|expired
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    allowed_models: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    rpm_limit: Mapped[int | None] = mapped_column(Integer)
    tpm_limit: Mapped[int | None] = mapped_column(Integer)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    rotated_from: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("virtual_keys.id"))
    grace_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


# ---- catalog ---------------------------------------------------------------


class Model(Base):
    __tablename__ = "models"
    __table_args__ = (UniqueConstraint("org_id", "name"),)
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    modalities: Mapped[list[str]] = mapped_column(ARRAY(Text), default=lambda: ["chat"])
    context_window: Mapped[int | None] = mapped_column(Integer)
    supports_tools: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_json_schema: Mapped[bool] = mapped_column(Boolean, default=False)
    supports_vision: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="active")
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = _created()


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (UniqueConstraint("model_id", "name"),)
    id: Mapped[uuid.UUID] = _pk()
    model_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("models.id"), index=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id"))
    name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(40))
    provider_model: Mapped[str] = mapped_column(String(200))
    base_url: Mapped[str | None] = mapped_column(Text)
    credential_ref: Mapped[str] = mapped_column(Text, default="none")
    weight: Mapped[int] = mapped_column(Integer, default=1)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    extra_headers: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer)
    max_input_tokens: Mapped[int | None] = mapped_column(Integer)
    region: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


class DeploymentHealth(Base):
    """Latest active-probe result per deployment (docs/spec/04 §6); written by the worker, read by the portal."""

    __tablename__ = "deployment_health"
    deployment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("deployments.id", ondelete="CASCADE"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # healthy | degraded | unhealthy | unknown
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(200))
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # vLLM queue pressure from /metrics (docs/spec/04 §5.1); NULL when the deployment exposes no metrics
    queue_waiting: Mapped[int | None] = mapped_column(Integer)
    queue_running: Mapped[int | None] = mapped_column(Integer)
    kv_cache_usage: Mapped[float | None] = mapped_column(Float)
    metrics_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # vLLM queue pressure from /metrics (docs/spec/04 §5.1); NULL when the deployment exposes no metrics
    queue_waiting: Mapped[int | None] = mapped_column(Integer)
    queue_running: Mapped[int | None] = mapped_column(Integer)
    kv_cache_usage: Mapped[float | None] = mapped_column(Float)
    metrics_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # vLLM queue pressure from /metrics (docs/spec/04 §5.1); NULL when the deployment exposes no metrics
    queue_waiting: Mapped[int | None] = mapped_column(Integer)
    queue_running: Mapped[int | None] = mapped_column(Integer)
    kv_cache_usage: Mapped[float | None] = mapped_column(Float)
    metrics_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (UniqueConstraint("provider", "provider_model", "version"),)
    id: Mapped[uuid.UUID] = _pk()
    provider: Mapped[str] = mapped_column(String(40))
    provider_model: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, default=1)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    input_per_million: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    output_per_million: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    cached_input_per_million: Mapped[Decimal | None] = mapped_column(MONEY)
    reasoning_per_million: Mapped[Decimal | None] = mapped_column(MONEY)
    source: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created()


# ---- budgets & ledger ------------------------------------------------------


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("scope_type", "scope_id", "period"),)
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), index=True)
    scope_type: Mapped[str] = mapped_column(String(20))  # organization|team|project|key
    scope_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    limit_amount: Mapped[Decimal] = mapped_column(MONEY)
    period: Mapped[str] = mapped_column(String(20), default="total")  # total|daily|monthly
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reserved_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    spent_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    soft_alert_pct: Mapped[int | None] = mapped_column(SmallInteger)
    soft_alert_sent_period: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    temporary_increase: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    temporary_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = _created()


class RequestAttempt(Base):
    __tablename__ = "request_attempts"
    id: Mapped[uuid.UUID] = _pk()
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    attempt_no: Mapped[int] = mapped_column(SmallInteger, default=1)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    team_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    key_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    model_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    model_name: Mapped[str] = mapped_column(String(200))
    deployment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(String(40))
    provider_model: Mapped[str] = mapped_column(String(200))
    endpoint: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    reserved_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    settled_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    usage_source: Mapped[str | None] = mapped_column(String(20))
    price_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("prices.id"))
    upstream_request_id: Mapped[str | None] = mapped_column(Text)
    error_class: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)
    routing: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # decision explanation
    started_at: Mapped[datetime] = _created()
    first_byte_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dedup_key: Mapped[str] = mapped_column(String(100), unique=True)


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_org_created", "org_id", "created_at"),
        Index("ix_usage_project_created", "project_id", "created_at"),
        Index("ix_usage_key_created", "key_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = _pk()
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    attempt_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("request_attempts.id"), unique=True)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    team_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    key_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    model_name: Mapped[str] = mapped_column(String(200))
    deployment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(String(40))
    endpoint: Mapped[str] = mapped_column(String(20))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    usage_source: Mapped[str] = mapped_column(String(20), default="reported")
    status: Mapped[str] = mapped_column(String(20))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    ttfb_ms: Mapped[int | None] = mapped_column(Integer)
    stream: Mapped[bool] = mapped_column(Boolean, default=False)
    tags: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created()


# ---- governance ------------------------------------------------------------


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_target", "target_type", "target_id", "created_at"),)
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    request_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = _created()


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _created()
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class ConfigVersion(Base):
    __tablename__ = "config_versions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    reason: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = _created()

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Provider = Literal["openai_compat", "openai", "anthropic", "azure_openai"]


class OrgCreate(BaseModel):
    name: str
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,60}$")
    settings: dict[str, Any] = {}


class OrgPatch(BaseModel):
    name: str | None = None
    status: Literal["active", "suspended"] | None = None
    settings: dict[str, Any] | None = None


class TeamCreate(BaseModel):
    name: str
    settings: dict[str, Any] = {}


class ProjectCreate(BaseModel):
    name: str
    settings: dict[str, Any] = {}  # allowed_tags: [...], rpm_limit, tpm_limit


class NamePatch(BaseModel):
    name: str | None = None
    status: Literal["active", "suspended"] | None = None
    settings: dict[str, Any] | None = None


class RoleBindingCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=320)  # Keycloak sub, preferred_username or email
    subject_kind: Literal["user", "service_account"] = "user"
    role: Literal["org_owner", "team_owner", "project_member"]
    scope_type: Literal["organization", "team", "project"]
    scope_id: str


class KeyCreate(BaseModel):
    name: str
    expires_at: datetime | None = None
    allowed_models: list[str] | None = None
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = {}


class KeyRotate(BaseModel):
    grace_seconds: int = Field(default=3600, ge=0, le=7 * 86400)


class ModelCreate(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9._:/-]{1,200}$")
    org_id: str | None = None
    display_name: str | None = None
    modalities: list[Literal["chat", "embedding"]] = ["chat"]
    context_window: int | None = None
    supports_tools: bool = False
    supports_json_schema: bool = False
    supports_vision: bool = False
    metadata: dict[str, Any] = {}


class ModelPatch(BaseModel):
    display_name: str | None = None
    status: Literal["active", "disabled"] | None = None
    context_window: int | None = None
    supports_tools: bool | None = None
    supports_json_schema: bool | None = None
    supports_vision: bool | None = None
    metadata: dict[str, Any] | None = None


class DeploymentCreate(BaseModel):
    name: str
    provider: Provider
    provider_model: str
    base_url: str | None = None
    credential_ref: str = "none"
    weight: int = Field(default=1, ge=0)
    priority: int = 0
    capabilities: dict[str, Any] = {}
    extra_headers: dict[str, str] = {}
    timeout_seconds: int | None = None
    max_input_tokens: int | None = None
    region: str | None = None


class DeploymentPatch(BaseModel):
    base_url: str | None = None
    credential_ref: str | None = None
    weight: int | None = Field(default=None, ge=0)
    priority: int | None = None
    status: Literal["active", "disabled"] | None = None
    capabilities: dict[str, Any] | None = None
    extra_headers: dict[str, str] | None = None
    timeout_seconds: int | None = None
    max_input_tokens: int | None = None
    region: str | None = None


class CooldownRequest(BaseModel):
    seconds: int = Field(ge=0, le=86400)


class PriceCreate(BaseModel):
    provider: Provider
    provider_model: str
    input_per_million: Decimal = Decimal(0)
    output_per_million: Decimal = Decimal(0)
    cached_input_per_million: Decimal | None = None
    reasoning_per_million: Decimal | None = None
    effective_from: datetime | None = None
    source: str | None = None
    reviewed_by: str | None = None


class BudgetCreate(BaseModel):
    scope_type: Literal["organization", "team", "project", "key"]
    scope_id: str
    limit_amount: Decimal = Field(ge=0)
    period: Literal["total", "daily", "monthly"] = "total"
    soft_alert_pct: int | None = Field(default=None, ge=1, le=100)


class BudgetPatch(BaseModel):
    limit_amount: Decimal | None = Field(default=None, ge=0)
    soft_alert_pct: int | None = Field(default=None, ge=1, le=100)
    status: Literal["active", "disabled"] | None = None


class TemporaryIncrease(BaseModel):
    amount: Decimal = Field(gt=0)
    until: datetime


class Page(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    data: list[Any]
    next_cursor: str | None = None

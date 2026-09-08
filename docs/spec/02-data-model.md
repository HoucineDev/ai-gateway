# 02 — Data model (PostgreSQL)

All tables have `id UUID PRIMARY KEY` (UUIDv7 generated in application), `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`. Tenant-owned tables carry `org_id` denormalized for isolation predicates and future row-level security. Money is `NUMERIC(20,8)` in a single ledger currency (default USD; internal GPU allocation is expressed in the same unit through a per-deployment price row).

## Tenancy

```
organizations(id, name, slug UNIQUE, status, settings JSONB)
teams(id, org_id→organizations, name, status, settings JSONB)             UNIQUE(org_id, name)
projects(id, org_id, team_id→teams, name, status, settings JSONB)         UNIQUE(team_id, name)
role_bindings(id, subject TEXT (Keycloak sub | username | email), subject_kind ENUM(user, service_account),
       role ENUM(org_owner, team_owner, project_member), scope_type ENUM(organization, team, project), scope_id UUID,
       org_id→organizations, team_id NULL, project_id NULL (denormalized from the scope for predicates),
       status ENUM(active, revoked), created_by TEXT, revoked_at TIMESTAMPTZ NULL)
                                                                         UNIQUE(subject, role, scope_type, scope_id)
```

Role bindings are delegated tenant roles (docs/spec/01 §3.2); they never reach the gateway snapshot.

## Keys

```
virtual_keys(
  id, org_id, team_id, project_id→projects,
  name, key_prefix TEXT (first 12 chars, for display), key_hash TEXT UNIQUE (sha256 hex),
  status ENUM(active, revoked, expired), expires_at, revoked_at,
  allowed_models TEXT[] NULL (NULL = every model visible to the project),
  rpm_limit INT NULL, tpm_limit INT NULL,
  metadata JSONB, rotated_from→virtual_keys NULL, grace_until TIMESTAMPTZ NULL)
```

Plaintext key format: `aigw_` + 40 url-safe random chars. Plaintext is returned once at creation and never stored.

## Model catalog

```
models(id, org_id NULL (global when NULL), name, display_name, modalities TEXT[] (chat, embedding),
       context_window INT, supports_tools BOOL, supports_json_schema BOOL, supports_vision BOOL,
       status, metadata JSONB)                                              UNIQUE(org_id, name)
deployments(id, model_id→models, org_id NULL, name,
       provider ENUM(openai_compat, openai, anthropic),
       provider_model TEXT, base_url TEXT NULL, credential_ref TEXT (env:NAME | openbao:path#key),
       weight INT DEFAULT 1, priority INT DEFAULT 0 (lower = preferred),
       status ENUM(active, disabled), cooldown_until TIMESTAMPTZ NULL,
       capabilities JSONB (adapter-declared overrides), extra_headers JSONB, timeout_seconds INT,
       max_input_tokens INT NULL, region TEXT NULL)                          UNIQUE(model_id, name)
prices(id, provider, provider_model, version INT, effective_from TIMESTAMPTZ,
       input_per_million NUMERIC(20,8), output_per_million NUMERIC(20,8),
       cached_input_per_million NUMERIC(20,8) NULL, reasoning_per_million NUMERIC(20,8) NULL,
       source TEXT (url or 'internal-allocation'), reviewed_by TEXT NULL)
                                                        UNIQUE(provider, provider_model, version)
```

Price resolution: newest `version` whose `effective_from <= now()` for `(provider, provider_model)`; a deployment may pin `capabilities.price_override_id`.

```
deployment_health(deployment_id PK→deployments ON DELETE CASCADE, status ENUM(healthy, degraded, unhealthy, unknown),
       consecutive_failures INT, latency_ms INT NULL, error TEXT NULL, checked_at TIMESTAMPTZ NULL,
       queue_waiting INT NULL, queue_running INT NULL, kv_cache_usage FLOAT NULL, metrics_at TIMESTAMPTZ NULL)
```

Written only by the worker's active health checks (docs/spec/04 §6); never part of the gateway snapshot.

## Budgets and ledger

```
budgets(id, org_id, scope_type ENUM(organization, team, project, key), scope_id UUID,
        limit_amount NUMERIC(20,8), period ENUM(total, daily, monthly),
        period_start TIMESTAMPTZ, reserved_amount NUMERIC(20,8) DEFAULT 0, spent_amount NUMERIC(20,8) DEFAULT 0,
        soft_alert_pct SMALLINT NULL, temporary_increase NUMERIC(20,8) DEFAULT 0, temporary_until TIMESTAMPTZ NULL,
        status)                                             UNIQUE(scope_type, scope_id, period)

request_attempts(id, request_id UUID, attempt_no SMALLINT, org_id, team_id, project_id, key_id,
        model_id, deployment_id, provider, provider_model,
        status ENUM(pending, succeeded, failed, ambiguous, cancelled),
        reserved_amount NUMERIC(20,8), settled_amount NUMERIC(20,8) NULL,
        prompt_tokens INT NULL, completion_tokens INT NULL, cached_tokens INT NULL, reasoning_tokens INT NULL,
        usage_source ENUM(reported, estimated) NULL, price_id→prices NULL,
        upstream_request_id TEXT NULL, error_class TEXT NULL, error_message TEXT NULL,
        started_at, first_byte_at NULL, ended_at NULL, dedup_key TEXT UNIQUE)

usage_events(id, request_id, attempt_id→request_attempts UNIQUE, org_id, team_id, project_id, key_id,
        model_name, deployment_id, provider, endpoint ENUM(chat, embeddings),
        prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens,
        cost NUMERIC(20,8), usage_source, status, latency_ms INT, ttfb_ms INT NULL,
        stream BOOL, tags JSONB (validated allocation tags from request.metadata), created_at)
        INDEX(org_id, created_at), INDEX(project_id, created_at), INDEX(key_id, created_at)
```

`reserved_amount` is decremented and `spent_amount` incremented in the same transaction as the `request_attempts` status change and the `usage_events` insert, so a crash leaves either a pending reservation (reconciled by the worker after `AIGW_ATTEMPT_TIMEOUT_SECONDS`) or a fully settled row — never a half state.

## Governance

```
audit_events(id, org_id NULL, actor_type ENUM(admin_key, user, system), actor_id TEXT,
        action TEXT (e.g. key.revoke), target_type TEXT, target_id UUID,
        before JSONB NULL, after JSONB NULL, request_id TEXT NULL, created_at)
   -- trigger: RAISE on UPDATE/DELETE (append-only)
outbox(id BIGSERIAL, topic TEXT, payload JSONB, created_at, processed_at NULL, attempts INT)
config_versions(id BIGSERIAL, reason TEXT, created_at)  -- max(id) is the current config version
```

## Isolation rules

- Every repository method takes an explicit `org_id` (or key scope) and adds it to the WHERE clause; cross-org lookups are only allowed for global models/prices.
- Phase 2: `ALTER TABLE … ENABLE ROW LEVEL SECURITY` on tenant tables with `current_setting('aigw.org_id')`; the gateway DB role must not be table owner.
- Cache keys (Phase 2 exact cache) include `org_id`, `project_id`, model, deployment provider model, and a hash of the full request body.

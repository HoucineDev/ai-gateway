# 01 — API contracts

## 1. Surfaces

| Surface | Path prefix | Auth | Role serving it |
|---------|-------------|------|-----------------|
| Inference API | `/v1/*` | Virtual key (`Authorization: Bearer aigw_…`) | `gateway` |
| Control API | `/admin/v1/*` | Admin key (`X-Admin-Key`) or Keycloak OIDC bearer JWT with roles mapped to scopes (§3.1) | `admin` |
| Health | `/healthz`, `/readyz` | none | all |
| Metrics | `/metrics` | none (private ingress) | all |

Both roles are the same Python package started with `AIGW_ROLE=gateway|admin|all`. `all` is for local development and tests.

## 2. Inference API (OpenAI-compatible wire format)

The public wire format is the OpenAI Chat Completions / Embeddings / Models shape because that is what most client SDKs already speak. This is a **wire compatibility choice**, not a dependency; the schema is owned in `aigw.core.types`.

### 2.1 `POST /v1/chat/completions`

Request fields accepted in Phase 1: `model`, `messages`, `stream`, `stream_options.include_usage`, `max_tokens`, `max_completion_tokens`, `temperature`, `top_p`, `stop`, `n` (must be 1), `tools`, `tool_choice`, `response_format` (`text`, `json_object`, `json_schema`), `seed`, `user`, `metadata` (map of string tags used for allocation), `presence_penalty`, `frequency_penalty`, `logprobs`, `top_logprobs`, `parallel_tool_calls`.

Unknown fields → `400 invalid_request` with `param` set, unless the deployment declares `provider_extensions_passthrough=true`, in which case fields under `extra_body` are forwarded verbatim.

`model` is a **logical model name** registered in the control API; the gateway selects a deployment (see 04).

Non-streaming response: OpenAI `chat.completion` object. Gateway adds `x-aigw-request-id`, `x-aigw-deployment`, `x-aigw-provider` response headers.

Streaming response: `text/event-stream`, each event `data: {chat.completion.chunk}` and a terminal `data: [DONE]`. The last content chunk before `[DONE]` carries `usage` when `stream_options.include_usage=true` **or always** when the gateway had to estimate usage (then `usage.aigw_estimated=true`). Tool-call fragments are emitted exactly as received from the adapter's canonical events; the gateway never re-chunks tool arguments.

Cancellation: client disconnect cancels the upstream request; the attempt settles as `ambiguous` (reserved maximum) unless usage was already received.

### 2.2 `POST /v1/embeddings`

Fields: `model`, `input` (string | string[] | int[] | int[][]), `encoding_format` (`float` only in Phase 1), `dimensions`, `user`, `metadata`.

### 2.3 `GET /v1/models`

Returns the logical models the calling key may use (intersection of project scope and key `allowed_models`), OpenAI `list` shape with extra `aigw` object: `{modalities, context_window, supports_tools, supports_json_schema, deployments: n}`.

### 2.3b Cache control

Request header `X-AIGW-Cache: no-store | no-cache`; response header `X-AIGW-Cache: hit | miss | refresh | bypass | off`;
a served hit carries `aigw.cached: true` (docs/spec/04 §9). Only projects with `settings.cache.enabled` are affected.

### 2.4 Error envelope

```json
{"error": {"message": "…", "type": "invalid_request|authentication|permission|not_found|rate_limit|budget_exceeded|upstream|unavailable|internal", "code": "string", "param": "optional", "request_id": "…"}}
```

HTTP mapping: 400 invalid_request; 401 authentication; 403 permission; 404 not_found; 429 rate_limit (`Retry-After` header) and budget_exceeded; 502 upstream; 503 unavailable (no eligible deployment / all in cooldown); 500 internal.

## 3. Control API

All write operations produce an append-only `audit_events` row with actor, action, target, before/after.

| Resource | Endpoints | Notes |
|----------|-----------|-------|
| organizations | `POST/GET /organizations`, `GET/PATCH /organizations/{id}` | |
| teams | `POST /organizations/{org}/teams`, `GET/PATCH /teams/{id}` | |
| projects | `POST /teams/{team}/projects`, `GET/PATCH /projects/{id}` | |
| keys | `POST /projects/{project}/keys` → returns plaintext once; `GET /keys/{id}`; `PATCH /keys/{id}` (rotation schedule); `POST /keys/{id}/revoke`; `POST /keys/{id}/rotate` (grace period); `POST /keys/{id}/pickup` (plaintext of a scheduled rotation, once) | stored as SHA-256 hash + prefix; §3.2 scheduled rotation |
| models | `POST/GET /models`, `PATCH /models/{id}` | logical models; `org_id` null = global; each deployment carries its latest active-probe `health` (docs/spec/04 §6) |
| deployments | `POST /models/{model}/deployments`, `PATCH /deployments/{id}`, `POST /deployments/{id}/cooldown` | credential is a secret reference (`env:NAME` in alpha, `openbao:path#key` later) |
| prices | `POST/GET /prices` | insert-only, versioned by `effective_from` |
| budgets | `POST /budgets`, `GET /budgets?scope_type&scope_id`, `PATCH /budgets/{id}`, `POST /budgets/{id}/temporary-increase` | scope: organization/team/project/key |
| usage | `GET /usage?scope_type&scope_id&from&to&group_by=model|day|key` | reads `usage_events` |
| requests | `GET /requests/{request_id}` | attempts, routing decision, usage — the developer diagnostics view |
| audit | `GET /audit?target_type&target_id&from&to` | |
| config | `GET /config/version` | monotonically increasing version the gateway polls |
| me | `GET /me` | caller identity: `actor_type`, `actor_id`, `roles`, effective `scopes`, `global_scopes` (unrestricted by tenant), delegated `grants` |
| auth | `GET /auth/config` | unauthenticated: `{admin_key: bool, oidc: {issuer, client_id, audience} | null}` for the portal's login flow |
| guardrail-events | `GET /guardrail-events?project_id&request_id&direction&action` | guardrail audit trail (docs/spec/04 §11), tenant-scoped; also embedded in `GET /requests/{id}` |
| invoices | `POST /invoices`, `POST /invoices/import?provider&period_start&period_end` (CSV body), `GET /invoices`, `GET /invoices/{id}`, `POST /invoices/{id}/reconcile` | provider bills vs settled ledger (docs/spec/04 §10); global scopes only |
| role-bindings | `POST /role-bindings`, `GET /role-bindings?subject&org_id&scope_type&scope_id`, `POST /role-bindings/{id}/revoke` | delegated tenant roles (§3.2) |

### 3.1 Authentication and scopes

Two credentials are accepted. `X-Admin-Key` (`AIGW_ADMIN_KEY`) is the bootstrap credential and carries every scope.
`Authorization: Bearer <JWT>` is accepted when `AIGW_OIDC_ISSUER` is set: the token is validated against the issuer's
JWKS (`{issuer}/protocol/openid-connect/certs` unless `AIGW_OIDC_JWKS_URL` overrides it) — asymmetric algorithms only,
`exp`/`iat`/`iss`/`sub` required, `aud` checked when `AIGW_OIDC_AUDIENCE` is set, Keycloak `typ` must be `Bearer`
(ID and refresh tokens are refused). Signing keys are cached for `AIGW_OIDC_JWKS_CACHE_SECONDS`; an unknown `kid`
triggers a refetch at most once per `AIGW_OIDC_JWKS_MIN_REFRESH_SECONDS` (key rotation without restart, no JWKS
hammering from garbage tokens). If the JWKS cannot be fetched and nothing is cached, bearer callers get 503
`oidc_unavailable`; cached keys keep working through an identity-provider outage.

Roles are read from `realm_access.roles`, `resource_access.<AIGW_OIDC_CLIENT_ID or AIGW_OIDC_AUDIENCE>.roles` and a
flat `roles` claim, then mapped to scopes through `AIGW_OIDC_ROLE_SCOPES` (JSON object, role → list of scope
patterns). Scopes are `<resource>:<read|write>` over `organizations, teams, projects, keys, models, deployments,
prices, budgets, usage, requests, audit, config`; `*`, `*:read`, `*:write` and `<resource>:*` are wildcards. Default
mapping:

| Keycloak role | Scopes |
|---------------|--------|
| `aigw-admin` | `*` |
| `aigw-operator` | `*:read`, `models:write`, `deployments:write`, `prices:write`, `budgets:write` |
| `aigw-viewer` | `*:read` |

Every route declares the scope it needs (`require_scope`, enforced by a test). Outcomes: 401 `invalid_token`,
`unknown_signing_key`, `invalid_admin_key`, `admin_auth_required`, `oidc_not_configured`; 403 `missing_role` (valid
token, no mapped role) and `insufficient_scope`. Audit rows record `actor_type=user` and `actor_id` = `preferred_username`
(falling back to `sub`). The Keycloak client must keep the `basic` client scope (Keycloak ≥ 24 emits `sub` through
it) and the `roles` scope; `deploy/compose/keycloak/realm-aigw.json` is a reference realm. Delegated tenant-level roles (org owner, project member) are a separate Phase 2 item and will
narrow these scopes by tenant; this section is global role-based access.

### 3.2 Scheduled key rotation

A key created or patched with `rotate_every_seconds` (≥ 1 h) and `rotation_grace_seconds` (default 3600) is rotated
by the worker once the interval has elapsed since its creation: a new key with the same scope, limits, metadata and
schedule is issued, the previous key stays valid for the grace period (`grace_until`), the config version is bumped
and an audit event `key.rotate.scheduled` (actor `worker`) plus an outbox event `key.rotated` are written. Because
no operator is present to copy the plaintext, it is sealed with `AIGW_KEY_PICKUP_SECRET` (Fernet) into `key_pickups`
for `AIGW_KEY_PICKUP_TTL_SECONDS` (default 24 h); `POST /keys/{id}/pickup` returns it exactly once (audited
`key.pickup`) and deletes it, and `GET /keys/{id}` shows `pickup_available` / `pickup_expires_at` / `next_rotation_at`.
Without the secret the worker skips due keys with a warning instead of rotating keys nobody can retrieve. Manual
`POST /keys/{id}/rotate` keeps working and carries the schedule over to the new key.

### 3.3 Delegated roles (tenant-scoped)

Global roles (§3.1) grant scopes everywhere. A **role binding** grants a role to a subject inside one tenant:

| Role | Bound to | Grants inside that tenant |
|------|----------|---------------------------|
| `org_owner` | organization | everything below it: teams, projects, keys, budgets, usage, requests, audit, org-scoped models and deployments, role bindings of equal or lower rank |
| `team_owner` | team | the team record, its projects, keys, budgets, usage, requests, `team_owner`/`project_member` bindings within the team |
| `project_member` | project | the project record (read), its keys (read/write), its budgets, usage and requests (read) |

A *subject* is matched against the token's `sub`, `preferred_username` and `email` (exact); Keycloak service
accounts (`service-account-<client>`) bind the same way with `subject_kind: service_account`. Bindings are rows in
`role_bindings` (docs/spec/02), looked up on every control-API request, so a revoke is immediate. Global models
(`org_id` null) and prices stay read-only for delegates; writing them needs a global scope.

Authorization is evaluated in two steps. The route's `require_scope` gate passes when either a global scope or
any binding's role includes the scope. The route then calls `actor.require(scope, org_id, team_id, project_id)`
with the target's tenant ids: a global scope passes unconditionally; otherwise some binding must both include the
scope and *cover* the target (an organization binding covers its org, a team binding its team, a project binding
its project). Failure is 403 `tenant_forbidden`. List endpoints apply the same rule as a SQL predicate so a
delegate only sees rows inside their tenants. A subject with neither a mapped global role nor an active binding
is refused with 403 `missing_role`.

Creating a binding requires `role_bindings:write` on the target tenant and, for delegates, a rank at or above
the role being granted at that target (`org_owner` > `team_owner` > `project_member`), so a team owner can add
project members but cannot make org owners. Every binding write is audited; `GET /me` lists the caller's grants.

Portal login: a dedicated `/login` page offers the admin key (bootstrap credential, verified through `GET /me`
before entry) and *Sign in with SSO*, greyed out until `GET /auth/config` reports an issuer; unauthenticated pages
redirect to `/login?redirect_to=…`. SSO is the authorization-code flow with PKCE against the public client (`portal/src/lib/auth.ts`), tokens kept in
`sessionStorage`, refreshed with the refresh token 30 s before expiry; the portal discovers issuer and client id from
`GET /auth/config` and hides actions whose scope `GET /me` does not grant. The API, not the UI, is the enforcement point.

Pagination: `?limit&cursor` (opaque). IDs are UUIDv7 strings. Timestamps are RFC 3339 UTC.

## 4. Configuration snapshot contract (gateway ⇄ control plane)

The gateway does not query tenancy tables per request. It loads a **config snapshot** (models, deployments, prices, key→project/team/org scope, budgets metadata, rate limits) and refreshes it when `config_version` changes, at most every `AIGW_CONFIG_REFRESH_SECONDS` (default 5). Maximum staleness therefore equals the refresh interval plus one poll; key revocation additionally publishes an invalidation on the Valkey channel `aigw:invalidate:key` so revocation lands within roughly one second when Valkey is healthy. Budgets and spend are never cached; they are always checked transactionally in PostgreSQL.

# 01 — API contracts

## 1. Surfaces

| Surface | Path prefix | Auth | Role serving it |
|---------|-------------|------|-----------------|
| Inference API | `/v1/*` | Virtual key (`Authorization: Bearer aigw_…`) | `gateway` |
| Control API | `/admin/v1/*` | Admin key (`X-Admin-Key`) in alpha; OIDC/Keycloak JWT from Phase 2 | `admin` |
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
| keys | `POST /projects/{project}/keys` → returns plaintext once; `GET /keys/{id}`; `POST /keys/{id}/revoke`; `POST /keys/{id}/rotate` (grace period) | stored as SHA-256 hash + prefix |
| models | `POST/GET /models`, `PATCH /models/{id}` | logical models; `org_id` null = global |
| deployments | `POST /models/{model}/deployments`, `PATCH /deployments/{id}`, `POST /deployments/{id}/cooldown` | credential is a secret reference (`env:NAME` in alpha, `openbao:path#key` later) |
| prices | `POST/GET /prices` | insert-only, versioned by `effective_from` |
| budgets | `POST /budgets`, `GET /budgets?scope_type&scope_id`, `PATCH /budgets/{id}`, `POST /budgets/{id}/temporary-increase` | scope: organization/team/project/key |
| usage | `GET /usage?scope_type&scope_id&from&to&group_by=model|day|key` | reads `usage_events` |
| requests | `GET /requests/{request_id}` | attempts, routing decision, usage — the developer diagnostics view |
| audit | `GET /audit?target_type&target_id&from&to` | |
| config | `GET /config/version` | monotonically increasing version the gateway polls |

Pagination: `?limit&cursor` (opaque). IDs are UUIDv7 strings. Timestamps are RFC 3339 UTC.

## 4. Configuration snapshot contract (gateway ⇄ control plane)

The gateway does not query tenancy tables per request. It loads a **config snapshot** (models, deployments, prices, key→project/team/org scope, budgets metadata, rate limits) and refreshes it when `config_version` changes, at most every `AIGW_CONFIG_REFRESH_SECONDS` (default 5). Maximum staleness therefore equals the refresh interval plus one poll; key revocation additionally publishes an invalidation on the Valkey channel `aigw:invalidate:key` so revocation lands within roughly one second when Valkey is healthy. Budgets and spend are never cached; they are always checked transactionally in PostgreSQL.

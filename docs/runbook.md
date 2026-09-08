# Runbook — Independent AI Gateway

Operational how-to for running, checking and troubleshooting the stack. Kept current with every change
(README = what it is; this file = how to operate it; `docs/spec/` = what it must do).

## 1. Components and ports

| Component | Role | Default port | Health |
|-----------|------|--------------|--------|
| gateway | OpenAI-compatible ingress `/v1/*` | 8080 | `GET /healthz`, `GET /readyz` (503 while config snapshot missing/stale) |
| admin | control API `/admin/v1/*` + React portal at `/` | 8081 | `GET /healthz`; `GET /admin/v1/config/version` with a credential |
| worker | outbox, reconciliation, key expiry, active health checks | — | process logs (`health sweep: …`); catalog *Health* column in the portal |
| PostgreSQL 16 | only stateful authority | 5432 (compose) | `pg_isready -U aigw` |
| Valkey 8 | rate limits, cooldowns, key invalidation | 6379 | `redis-cli ping`; gateway degrades open/closed per `AIGW_RATELIMIT_FAIL_MODE` |
| mock-upstream | OpenAI/Anthropic-compatible fake provider | 9000 | `GET /healthz` (also `GET /v1/models`) |
| Keycloak 26 (opt-in, `--profile oidc`) | identity provider for the control API and portal | 8180 | `GET /realms/aigw/.well-known/openid-configuration` |

## 2. Start / stop

```bash
cd deploy/compose
docker compose up -d --build                   # postgres, valkey, mock-upstream, migrate+bootstrap, gateway, admin, worker
docker compose --profile oidc up -d keycloak   # optional SSO (dev realm imported from keycloak/realm-aigw.json)
docker compose logs migrate | grep key:        # bootstrap virtual key (shown once)
docker compose down                            # keep data;  docker compose down -v  wipes Postgres
```

Native (dev loop) against the compose database:

```bash
export AIGW_DATABASE_URL=postgresql+asyncpg://aigw:aigw@localhost:5432/aigw AIGW_VALKEY_URL=redis://localhost:6379/0
export AIGW_ADMIN_KEY=change-me-admin
export AIGW_OIDC_ISSUER=http://localhost:8180/realms/aigw AIGW_OIDC_AUDIENCE=aigw-portal   # only with Keycloak
aigw migrate && aigw serve --role admin --port 8081        # serves portal/dist when it exists (cd portal && npm run build)
aigw serve --role gateway --port 8080
aigw worker
```

## 3. Check it works

```bash
curl localhost:8080/healthz && curl localhost:8081/healthz
curl localhost:8081/admin/v1/overview -H 'x-admin-key: change-me-admin'
curl -N localhost:8080/v1/chat/completions -H "authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"local-chat","stream":true,"messages":[{"role":"user","content":"hello"}]}'
```

Portal (graphical UI): open http://localhost:8081 (or `cd portal && npm run dev` → http://localhost:5173, proxying
`/admin` to 8081). Sign in with Keycloak when the `oidc` profile runs (users `alice`/`bob`/`carol`/`dan`, password =
username, roles admin/operator/viewer/none), otherwise paste the admin key in *Settings*. Pages: Overview, Models &
deployments, Organizations & keys, Budgets, Requests (per-request diagnostics), Audit, Settings. Write buttons are
rendered only for scopes the caller holds; the control API enforces them regardless.

SSO end-to-end check (needs Keycloak and the admin role):

```bash
python scripts/oidc_smoke.py        # alice 201, bob/carol 403 insufficient_scope, dan 403 missing_role, audit actor=alice
```

Tests and gates:

```bash
pytest -q                                       # 40 tests; needs Postgres db aigw_test (user aigw/aigw) + Valkey
pytest tests/test_admin_oidc.py --noconftest -k "not api"   # OIDC verifier tests, no database
ruff check src tests scripts && ruff format --check src tests scripts
python scripts/independence_gate.py             # fails on any litellm artefact, including pip freeze of the environment
cd portal && npx tsc -b && npm run build
python scripts/loadtest.py --key $KEY --requests 500 --concurrency 32 --upstream http://localhost:9000   # load + added latency
```

## 4. Configuration quick reference

All variables are `AIGW_*` (full table: `docs/spec/05-deployment.md` §4). Most often touched:

| Variable | Purpose |
|----------|---------|
| `AIGW_ROLE` | gateway / admin / worker / all |
| `AIGW_DATABASE_URL`, `AIGW_VALKEY_URL` | connections |
| `AIGW_ADMIN_KEY` | bootstrap control-API credential (every scope) |
| `AIGW_OIDC_ISSUER`, `AIGW_OIDC_AUDIENCE`, `AIGW_OIDC_CLIENT_ID`, `AIGW_OIDC_JWKS_URL` | Keycloak bearer auth; JWKS URL override when the issuer host is not reachable from the admin container |
| `AIGW_OIDC_ROLE_SCOPES` | JSON: Keycloak role → scope patterns (default admin `*`, operator catalog writes, viewer `*:read`) |
| `AIGW_CONFIG_REFRESH_SECONDS`, `AIGW_CONFIG_MAX_STALENESS_SECONDS` | snapshot poll and fail-closed bound |
| `AIGW_RATELIMIT_FAIL_MODE` | open / closed when Valkey is down |
| `AIGW_HEALTH_CHECK_INTERVAL_SECONDS`, `AIGW_HEALTH_FAILURE_THRESHOLD`, `AIGW_HEALTH_COOLDOWN_SECONDS` | active health checks: sweep interval (0 = off), failures before cooldown, cooldown length |
| `AIGW_ROUTING_STRATEGY` | `adaptive` (default: weights × latency × queue × in-flight) or `weighted` (weights only, docs/spec/04 §5.1) |
| `AIGW_CACHE_MAX_ENTRY_BYTES` | largest answer the exact response cache stores (default 256 KiB) |
| `AIGW_ROUTING_LATENCY_REF_MS`, `AIGW_ROUTING_QUEUE_REF`, `AIGW_ROUTING_INFLIGHT_REF` | load at which each signal halves a deployment's draw weight (1000 ms, 8 queued, 4 in flight) |

Provider credentials are secret references on deployments (`env:NAME`), never stored in the database.

## 5. Common operations

| Task | How |
|------|-----|
| Revoke a key immediately | portal → Organizations & keys → revoke, or `POST /admin/v1/keys/{id}/revoke`; lands on gateways within ~1 s via Valkey, worst case one refresh interval |
| Rotate a key with grace | `POST /admin/v1/keys/{id}/rotate {"grace_seconds": 3600}` |
| Add an Azure OpenAI deployment | `POST /admin/v1/models/{model}/deployments {"provider": "azure_openai", "provider_model": "<azure deployment name>", "base_url": "https://<resource>.openai.azure.com", "credential_ref": "env:AZURE_OPENAI_KEY", "capabilities": {"api_version": "2024-10-21"}}`; add a price row for `(azure_openai, <deployment name>)`; Entra auth: `"capabilities": {"auth": "bearer"}` with a token in the credential ref |
| Add a Gemini deployment (Vertex AI) | `POST /admin/v1/models/{model}/deployments {"provider": "gemini", "provider_model": "gemini-2.5-pro", "credential_ref": "env:GOOGLE_SA_JSON", "capabilities": {"project": "<gcp project>", "location": "europe-west1", "auth": "service_account"}}` (credential = service-account key JSON; or `auth: bearer` with a ready access token); Google AI Studio: `"capabilities": {"api": "google_ai"}` with the API key as credential; price row `(gemini, gemini-2.5-pro)` |
| Add a Bedrock deployment | `POST /admin/v1/models/{model}/deployments {"provider": "bedrock", "provider_model": "anthropic.claude-3-5-sonnet-20241022-v2:0", "credential_ref": "env:AWS_BEDROCK_CREDS", "capabilities": {"region": "eu-west-1"}}` with `AWS_BEDROCK_CREDS=ACCESS_KEY_ID:SECRET[:SESSION_TOKEN]`; Bedrock API key instead: `"capabilities": {"auth": "bearer"}`; embeddings: `amazon.titan-embed-text-v2:0` or `cohere.embed-*`; price row `(bedrock, <model id>)` |
| Take a deployment out of rotation | `POST /admin/v1/deployments/{id}/cooldown {"seconds": 600}` or PATCH `status: disabled` |
| See upstream health | portal → Models & deployments → *Health* column (status, latency, error, failures, vLLM queue depth and KV-cache use), or `GET /admin/v1/models` → `deployments[].health` |
| Make a vLLM deployment GPU-aware | set `capabilities: {"engine": "vllm"}` on the deployment (metrics URL derived from `base_url` minus `/v1`) or an explicit `"metrics_url"`; the worker scrapes it every health sweep |
| Cap concurrent requests to one upstream per gateway replica | `capabilities: {"max_concurrency": N}`; beyond N the router rejects it with reason `saturated` and falls back to the next deployment |
| Turn on the response cache for a project | `PATCH /admin/v1/projects/{id} {"settings": {...existing..., "cache": {"enabled": true, "ttl_seconds": 300, "deterministic_only": false}}}` (settings are replaced whole: keep `allowed_tags`); takes effect on the next snapshot refresh (≤ 5 s) |
| Check whether a response came from cache | response header `X-AIGW-Cache` (`hit`/`miss`/`refresh`/`bypass`/`off`), body `aigw.cached`, Requests page status *cached* with cost 0; metric `aigw_cache_total` |
| Force a fresh answer | send `X-AIGW-Cache: no-cache` (refreshes the entry) or `no-store` (leaves the cache untouched) |
| Measure gateway overhead / capacity | `python scripts/loadtest.py --key <virtual key> --duration 60 --concurrency 64 --stream-ratio 0.8 --upstream http://localhost:9000 --json run.json`; compare *added* p95 between builds; `--steer slow` makes the mock answer in ~400 ms like a real model; thresholds (`--max-added-p95-ms`, `--max-error-rate`, `--min-rps`) exit 1 for CI |
| Reconcile a provider bill | export the bill as CSV (`provider_model,day,amount,prompt_tokens,completion_tokens`), then `curl -X POST 'localhost:8081/admin/v1/invoices/import?provider=openai&period_start=2026-09-01&period_end=2026-09-30' -H 'x-admin-key: …' -H 'content-type: text/csv' --data-binary @bill.csv`, then `POST /admin/v1/invoices/{id}/reconcile {"tolerance_pct": 1}`; read `status`, `delta_amount`, `report.missing_in_invoice` and each line's `status`/`delta_amount` |
| Turn on guardrails for a project | `PATCH /admin/v1/projects/{id} {"settings": {...existing..., "guardrails": {"pre": [{"detector": "pii", "action": "redact"}], "post": [{"detector": "http", "url": "http://presidio-sidecar/check", "action": "block", "fail": "open", "timeout_ms": 500}], "post_stream": "buffer"}}}` (settings are replaced whole); takes effect on the next snapshot refresh |
| Review guardrail hits | `GET /admin/v1/guardrail-events?project_id=&action=block` or the `guardrails` list in `GET /admin/v1/requests/{id}`; metric `aigw_guardrail_total{detector,direction,action}` |
| Explain a routing decision | `GET /admin/v1/requests/{request_id}` → `attempts[].routing`: `strategy`, `candidates`, `rejected` (reason per deployment) and `signals` (per-candidate `ttfb_ms`, `queue_waiting`, `inflight`, `weight`, `score`) |
| Raise a budget temporarily | `POST /admin/v1/budgets/{id}/temporary-increase {"amount": "50", "until": "<RFC3339>"}` |
| Add a price version | `POST /admin/v1/prices` (insert-only, versioned by `effective_from`) |
| Grant portal access (global) | assign realm role `aigw-admin` / `aigw-operator` / `aigw-viewer` in Keycloak; scopes take effect on the next token |
| Delegate one tenant | portal → Organizations & keys → select org/team/project → *Members* → add username/email, or `POST /admin/v1/role-bindings {"subject":"carol","role":"team_owner","scope_type":"team","scope_id":…}`; immediate, no new token needed |
| Remove a delegate | *Members* → remove, or `POST /admin/v1/role-bindings/{id}/revoke`; their next request fails with 403 `missing_role` unless a global role remains |
| See who can act on a tenant | `GET /admin/v1/role-bindings?scope_type=&scope_id=` (`include_revoked=true` for history); a user's own view: `GET /admin/v1/me` → `grants` |
| Who did what | `GET /admin/v1/audit?target_type=&target_id=` — `actor_type=user` + Keycloak username, or `admin_key` |
| Explain a request | `GET /admin/v1/requests/{request_id}` — attempts, routing decision, usage |
| Verify a restore | `aigw verify-restore` (row counts, latest audit event, budget totals = Σ usage) |

## 6. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `role "aigw" does not exist` or password failure while the compose Postgres is up | another Postgres already owns the host port; Docker binds it but the native listener wins. Map the container elsewhere (`ports: ["55432:5432"]` override) and set `AIGW_TEST_DATABASE_URL` / `AIGW_DATABASE_URL` accordingly |
| 401 `invalid_token: Token is missing the "sub" claim` | Keycloak ≥ 24 emits `sub` through the `basic` client scope; add it to the client's default scopes |
| 401 `invalid_token` mentioning issuer or audience | `AIGW_OIDC_ISSUER` must equal the token's `iss` exactly (pin Keycloak's `KC_HOSTNAME`); set `AIGW_OIDC_AUDIENCE` to the value the audience mapper emits or unset it |
| 503 `oidc_unavailable` | admin role cannot fetch the JWKS: check `AIGW_OIDC_JWKS_URL` (inside compose use `http://keycloak:8080/...`) |
| 403 `missing_role` | token valid but no role maps to a scope: assign a realm role or extend `AIGW_OIDC_ROLE_SCOPES` |
| 403 `insufficient_scope` | caller lacks `<resource>:write`; the portal hides such actions, curl calls do not |
| 403 `tenant_forbidden` | the caller has the scope somewhere, but not on this organization/team/project (delegated role on another tenant, or a global-only action such as creating an organization, a global model or a price) |
| 403 `rank_exceeded` | a delegate tried to grant or revoke a role above their own at that tenant (team owners manage team owners and project members, not org owners) |
| 400 `role_scope_mismatch` | `org_owner` binds to an organization, `team_owner` to a team, `project_member` to a project |
| Portal login loops back to Settings with "State mismatch" | sessionStorage cleared mid-flow (private window closed, different tab); sign in again from one tab |
| Gateway `readyz` 503 `config` | no snapshot yet or staleness bound exceeded: check database reachability and `aigw migrate` |
| `independence_gate.py` fails on `pip freeze` | litellm installed in the *environment*, not the repo; use a clean venv |
| Rate limits not enforced | Valkey down and `AIGW_RATELIMIT_FAIL_MODE=open` (default); metric `aigw_ratelimit_degraded` = 1 |
| 503 `no_eligible_deployment` for a model that has deployments | every deployment is disabled, in cooldown (portal → Models & deployments → *Cooldown* column; the snowflake button sets 10 min, click again to clear, or `POST /deployments/{id}/cooldown {"seconds": 0}`) or lacks a capability the request needs (tools, json_schema, context window); `GET /admin/v1/requests/{request_id}` shows the rejection reasons when the request carried `X-AIGW-Request-Id` |
| `docker ps` shows worker or mock-upstream *unhealthy* on images built before 2026-09-08 | the image default healthcheck probes :8080; rebuild (`docker compose up -d --build`) — worker now has no healthcheck, mock-upstream probes :9000 |
| Portal shows *Sign in to continue* | no credential in this browser: sign in with Keycloak or paste the admin key in Settings |
| Deployment shows *unhealthy* and requests avoid it | the worker's probe failed `AIGW_HEALTH_FAILURE_THRESHOLD` times (`error` says why: `timeout`, `transport: ConnectError`, `http_503`, `credential_missing`); fix the upstream or credential — the next healthy probe lifts the cooldown, or clear it by hand with `POST /deployments/{id}/cooldown {"seconds": 0}` (the worker will re-apply it while the probe keeps failing) |
| Health column says *not probed* | the worker is not running or `AIGW_HEALTH_CHECK_INTERVAL_SECONDS=0`; check `docker compose logs worker` |
| Traffic skews away from one deployment although it is healthy | adaptive routing: check its `signals` in a request's diagnostics (high `ttfb_ms`, `queue_waiting` or `inflight`); raise the matching `AIGW_ROUTING_*_REF` or set `AIGW_ROUTING_STRATEGY=weighted` to disable |
| Cache never hits (`X-AIGW-Cache: miss` every time) | different `provider_model` chosen by routing (entries are per deployment provider model), a field such as `temperature` differs, `deterministic_only` is on and the request is not pinned, or Valkey is down (every lookup misses) |
| `X-AIGW-Cache: off` although the project enabled it | snapshot not refreshed yet (≤ 5 s), the key belongs to another project, or `deterministic_only` excludes the request |
| 503 `azure_base_url_required` | an `azure_openai` deployment has no `base_url`; set it to the resource endpoint |
| Azure answers 401 `Access denied due to invalid subscription key` | wrong `credential_ref` value or the key belongs to another resource; with `capabilities.auth=bearer` the token must be an Entra token for `https://cognitiveservices.azure.com` |
| 503 `gemini_project_required` | a Vertex `gemini` deployment lacks `capabilities.project` |
| Gemini deployment unhealthy with `auth: authentication` | the service-account JWT grant was refused: check the key file (`client_email`, `private_key`), the account's Vertex AI role, and `token_url` if overridden |
| Bedrock answers 403 *signature does not match* | the credential's secret is wrong, the clock is off by more than 15 min, or `capabilities.region` differs from the endpoint region |
| Load test shows 429 `rate_limited` / `budget_exceeded` | the key/project RPM/TPM or budget is the ceiling, not the gateway: raise them for the test key or read the errors as the intended admission behaviour |
| Load test added p95 jumps between builds | run both builds with the same `--seed`, `--concurrency` and `--steer`; check Valkey health (rate-limit and cooldown round trips) and `AIGW_ROUTING_STRATEGY` before blaming code |
| A price added seconds ago is not applied (cost 0 on the next requests) | prices are filtered by `effective_from <= now` using the application clock (the one that stamps them); a future `effective_from` is honoured literally — check the value, not the database clock |
| Invoice line `unmatched` although traffic existed | the bill's `provider_model` must equal the deployment's `provider_model` (Azure: deployment name; Bedrock: model id); or the usage was cached (never billed) |
| Invoice `mismatch` only on `ledger_total` > `invoice_total` | `report.ambiguous_attempts` > 0: ambiguous attempts settle at the reserved maximum by design; the provider bills actual usage |
| 400 `guardrail_blocked` | a detector in the project's policy matched (message names detector and categories) or a `fail: closed` detector failed (`detail.error` in the event: `timeout`, HTTP status); raise `timeout_ms`, fix the sidecar, or set `fail: open` to degrade to logging |
| 503 `guardrail_config_invalid` | the project's `settings.guardrails` does not parse (unknown detector, bad regex, missing `url`); the project fails closed until fixed |
| Streamed answer ends with a `guardrail_blocked` error event | `post_stream: tail`: the client already received the content; switch the project to `post_stream: buffer` for hard blocking at the cost of no first byte until the answer completes |
| 503 `no_eligible_deployment` with reason `saturated` | this replica has `max_concurrency` requests open on every eligible deployment; raise the cap, add deployments or gateway replicas |

## 7. Change log of operational behaviour

- 2026-09-07 — Keycloak OIDC roles → scopes on the control API; every route declares a scope.
- 2026-09-08 — `oidc` compose profile with a dev realm; `scripts/oidc_smoke.py`; portal signs in with Keycloak
  (PKCE) and hides actions by scope; `GET /admin/v1/auth/config`; signed-out pages show a sign-in prompt;
  mock-upstream `GET /healthz` and correct compose healthchecks for mock-upstream and worker.
- 2026-09-08 — delegated tenant roles: `role_bindings` table (migration `f1eac2b1733d`, run `aigw migrate`),
  `POST/GET /admin/v1/role-bindings`, `…/revoke`; every route checks the target tenant, lists are filtered;
  portal *Members* panel; new error codes `tenant_forbidden`, `rank_exceeded`, `role_scope_mismatch`.
- 2026-09-08 — `GET /me` gains `global_scopes`; the portal hides global-only actions (new organization, global
  model, price, deployments on global models) from delegates.
- 2026-09-08 — active health checks in the worker (docs/spec/04 §6): `deployment_health` table (migration
  `e1a97b43dd62`), `AIGW_HEALTH_*` settings, automatic cooldown/recovery through Valkey, `health` on
  `GET /admin/v1/models`, catalog *Health* column.
- 2026-09-08 — adaptive routing (docs/spec/04 §5.1): latency EWMA, in-flight admission control
  (`capabilities.max_concurrency`), vLLM queue pressure scraped by the worker (`capabilities.engine: vllm`,
  migration `0b94f242d83b`), `AIGW_ROUTING_*` settings, per-candidate signals in request diagnostics.
- 2026-09-08 — exact response cache (docs/spec/04 §9): per-project `settings.cache`, Valkey `rc:` entries,
  `X-AIGW-Cache` request/response header, zero-cost `cached` attempts, `aigw_cache_total` metric.
- 2026-09-08 — `azure_openai` adapter (docs/spec/03 §4.1): deployment-name addressing, `api-version`, `api-key` or
  Entra bearer auth, health probe on `/openai/models`; mock upstream serves the Azure routes.
- 2026-09-08 — `gemini` adapter (docs/spec/03 §4.2): Vertex AI and Google AI Studio, full request/response
  translation incl. tools and JSON modes, SSE streaming, embeddings, service-account token exchange; mock upstream
  serves the Gemini routes and a token endpoint.
- 2026-09-08 — `bedrock` adapter (docs/spec/03 §4.3): Converse / ConverseStream / InvokeModel, owned SigV4 and
  event-stream decoder, Titan + Cohere embeddings; mock upstream verifies SigV4 signatures.
- 2026-09-08 — load-test harness (docs/spec/06): `scripts/loadtest.py` / `aigw.testing.loadtest`, added-latency
  baseline against the upstream, threshold gate; `tests/test_load.py` asserts ledger invariants under concurrency.
- 2026-09-08 — price resolution compares `effective_from` with the application clock instead of Postgres `now()`
  (a clock skew between host and database container made a just-created price invisible for a moment).
- 2026-09-08 — provider invoice reconciliation (docs/spec/04 §10): `invoices` / `invoice_lines` (migration),
  JSON and CSV import, `…/reconcile` with amount and token tolerances, `invoices:*` global scopes.
- 2026-09-08 — guardrail pipeline (docs/spec/04 §11): per-project pre/post rules, `pii` / `regex` / `keyword` /
  `http` detectors, block/redact/flag, fail-open/closed, tail or buffered streams, `guardrail_events` (migration),
  `GET /admin/v1/guardrail-events`, `aigw_guardrail_total`.

# Runbook — Independent AI Gateway

Operational how-to for running, checking and troubleshooting the stack. Kept current with every change
(README = what it is; this file = how to operate it; `docs/spec/` = what it must do).

## 1. Components and ports

| Component | Role | Default port | Health |
|-----------|------|--------------|--------|
| gateway | OpenAI-compatible ingress `/v1/*` | 8080 | `GET /healthz`, `GET /readyz` (503 while config snapshot missing/stale) |
| admin | control API `/admin/v1/*` + React portal at `/` | 8081 | `GET /healthz`; `GET /admin/v1/config/version` with a credential |
| worker | outbox, reconciliation, key expiry | — | process logs |
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

Provider credentials are secret references on deployments (`env:NAME`), never stored in the database.

## 5. Common operations

| Task | How |
|------|-----|
| Revoke a key immediately | portal → Organizations & keys → revoke, or `POST /admin/v1/keys/{id}/revoke`; lands on gateways within ~1 s via Valkey, worst case one refresh interval |
| Rotate a key with grace | `POST /admin/v1/keys/{id}/rotate {"grace_seconds": 3600}` |
| Take a deployment out of rotation | `POST /admin/v1/deployments/{id}/cooldown {"seconds": 600}` or PATCH `status: disabled` |
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

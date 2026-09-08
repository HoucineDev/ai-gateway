# CLAUDE.md — Independent AI Gateway

Self-hosted LLM gateway + management platform with **zero LiteLLM dependency**. Python 3.11+/FastAPI backend, React portal.
Read `docs/spec/00-overview.md` first; the spec is the source of truth and code must follow it (cite section numbers in commits).
Every change also updates `README.md` (if user-facing) and `docs/runbook.md` (operations, checks, troubleshooting, change log).

## Hard rules

- Never add `litellm` (package, import, copied code, pricing catalog). `python scripts/independence_gate.py` must pass; CI enforces it.
- Money is `Decimal` / `NUMERIC(20,8)` in PostgreSQL only. Valkey never decides a monetary limit.
- Never fail over to another deployment after the first client-visible byte. Ambiguous upstream outcomes settle at the reservation.
- Every control-API write must call `audit(...)` and, if it changes the gateway snapshot, `bump_config(...)`.
- Every control-API route declares its scope with `require_scope("<resource>:<read|write>")` (`tests/test_admin_oidc.py` fails otherwise), then calls `actor.require(scope, org_id=…, team_id=…, project_id=…)` on the target and filters lists with `tenancy.visible(...)` so delegated roles stay inside their tenant (`tests/test_admin_delegation.py`).
- Every tenant-owned query carries the org/project scope in its WHERE clause.
- Adapters reject unsupported parameters (`UnsupportedParameter`); never silently drop them.

## Commands

```bash
pip install -e ".[dev]"                     # backend deps
pytest -q                                   # needs Postgres (aigw_test, aigw/aigw) + Redis/Valkey on localhost
ruff check src tests && ruff format src tests
python scripts/independence_gate.py
aigw migrate | aigw bootstrap deploy/compose/bootstrap.yaml | aigw serve --role all | aigw worker | aigw verify-restore
alembic revision --autogenerate -m "..."    # after editing src/aigw/db/models.py
cd portal && npm install && npm run dev     # portal on :5173, proxies /admin to :8081
cd portal && npm run build                  # admin role serves portal/dist at /
cd deploy/compose && docker compose up -d --build
docker compose --profile oidc up -d keycloak && python scripts/oidc_smoke.py   # Keycloak dev realm + control-API bearer smoke test
python scripts/loadtest.py --key $KEY --upstream http://localhost:9000        # load test + gateway-added latency (docs/spec/06)
```

Local dev env: `AIGW_DATABASE_URL`, `AIGW_VALKEY_URL`, `AIGW_ADMIN_KEY` (see `.env.example`). Mock upstream:
`python -m aigw.testing.mock_upstream` (port 9000; OpenAI + Anthropic compatible; steer behaviour with `user` field: `fail:429`, `cut`, `nousage`, `tool`, `slow`).

## Layout

| Path | Role |
|------|------|
| `src/aigw/core` | canonical types (`types.py`), errors/classification, pricing, token estimation, secret refs |
| `src/aigw/adapters` | `base.py` contract + `openai_compat`, `openai`, `anthropic`, `azure_openai`, `gemini`, `bedrock` (+ `sigv4`, `eventstream`); `registry.py` |
| `src/aigw/gateway` | `snapshot` (config cache), `auth`, `ratelimit` (Valkey Lua), `accounting` (reserve/settle ledger), `router` (priority groups + adaptive weights), `signals` (latency EWMA, in-flight, queue pressure), `cache` (exact response cache, per-project policy), `guardrails` (pre/post detector pipeline), `pipeline` (orchestration), `routes`, `metrics` |
| `src/aigw/admin` | control API `/admin/v1`, `auth` (admin key / Keycloak OIDC → global scopes + delegated grants, `require_scope`, `Actor.require`), `tenancy` (grant loading, SQL visibility predicates), `reconcile` (provider invoices vs ledger), `service` (audit, config bump) |
| `src/aigw/worker` | outbox consumer, pending-attempt reconciliation, key expiry, `health` (active probes + vLLM `/metrics` scrape → `deployment_health`, Valkey cooldown and `dq:` pressure) |
| `src/aigw/db` | SQLAlchemy models, Alembic migrations (`src/aigw/migrations`) |
| `portal/` | React 18 + Vite + Tailwind v4 + TanStack Query; tokens in `src/index.css`; API client `src/lib/api.ts`; Keycloak PKCE login `src/lib/auth.ts`, scopes via `src/lib/session.tsx` (`useSession().can`, `<Can>`) |
| `deploy/` | Compose, Dockerfile (multi-stage: portal + python), Helm chart, ArgoCD example |
| `.claude/skills/` | vendored `ui-ux-pro-max`, `design-system`, `ui-styling` — use for any portal/UI work |

## Testing conventions

- Tests hit a real Postgres + Redis (`tests/conftest.py`); upstream is the mock app via `httpx.ASGITransport`.
- Provision tenants through the control API in tests (see the `tenant` fixture), then call `refresh(app)` to reload the snapshot.
- Add a fixture-backed contract test for every new adapter behaviour before implementing it (`docs/spec/03 §9`).
- Gate tests: `test_journey.py` (alpha), `test_accounting.py` + `test_routing.py` (pilot).

## Roadmap (docs/spec/07-parity-backlog.md)

Phase 2 done: Keycloak OIDC roles on control API, delegated tenant roles, portal SSO, active health checks, adaptive
routing (latency EWMA + vLLM queue pressure + local admission control), exact response cache, Azure OpenAI, Gemini (Vertex/Google AI) and Bedrock adapters, load-test harness, provider invoice reconciliation. Phase 2 is complete.
Phase 3 done: guardrail pipeline. Next: SCIM, scheduled key rotation, scoped logging exporters, approvals, retention.
Phase 4: native Anthropic/Gemini/Bedrock ingress, media APIs, files/batches, MCP/A2A, agent run limits, multi-region.

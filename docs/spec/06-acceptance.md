# 06 — Acceptance criteria and validation suite

## 1. Release gates (from the proposal)

### Alpha — "the complete user journey works for the initial local endpoint"

Journey (automated in `tests/test_journey.py`):

1. Administrator creates an organization, registers a logical model with one OpenAI-compatible deployment and a price row.
2. Administrator creates a team; team owner creates a project, sets a project budget, issues a key.
3. Developer sends a streaming chat request with the key; the gateway enforces access, chooses the deployment, streams chunks, and records usage with cost.
4. `GET /admin/v1/requests/{id}` shows the attempt, routing decision and cost; `GET /admin/v1/usage` aggregates it.
5. Key revocation blocks the next request within the agreed interval (≤ `AIGW_CONFIG_REFRESH_SECONDS` + 1 s, or ~1 s with Valkey invalidation).
6. Embeddings request succeeds and is accounted.
7. Restore: `pg_dump` → fresh database → `aigw verify-restore` passes and the journey runs again.

Alpha exit also requires: independence gate green; Compose stack boots from a clean checkout; Anthropic and OpenAI adapters pass their contract fixtures.

### Pilot — "isolation, budgets, revocation and recovery pass under concurrency and failure"

- Budget race: N concurrent requests against a budget that fits N/2 → exactly ⌊budget/reserve⌋ succeed, the rest 429; Σ spent ≤ limit; no pending reservations remain.
- Duplicate settlement (same `dedup_key`) is idempotent.
- Upstream timeout after dispatch → attempt `ambiguous`, settled at reservation, visible in diagnostics.
- Worker restart mid-outbox → no lost or duplicated alerts.
- Database outage < staleness bound → gateway keeps serving with snapshot; budget-checked requests fail closed (503) while the database is down; recovery is automatic.
- Cross-tenant: key of org A cannot see or use org B's models, keys, usage, or budgets (adversarial test matrix over every control endpoint).
- Policy version rollback: `config_versions` allows re-publishing a prior snapshot; provider credential rotation via `credential_ref` change takes effect within the refresh interval.

### Governance — "provisioning, delegated policies and retention are auditable"

SCIM provisioning, delegated roles, key rotation schedules, scoped logging exporters, guardrail pipeline, retention jobs and approval workflows each have an audit trail test and a retention test (Phase 3).

### Broad parity — "every claimed provider/endpoint combination has documented compatibility evidence"

`docs/compatibility/<provider>.md` generated from fixture test results; a combination is only marked supported when its fixtures pass in CI.

## 2. Minimum validation suite (proposal § "Minimum validation suite")

| Area | Tests |
|------|-------|
| Provider contract fixtures | streaming, tools, JSON schema, media, errors, token usage, unsupported parameters — per adapter |
| Concurrency and failure | budget races, duplicate events, upstream timeouts, worker restart, datastore outage |
| Isolation and lifecycle | tenant isolation matrix, key revocation, restore, policy rollback, credential rotation |

## 3. Performance targets

Not asserted by this proposal. The load test harness (`scripts/loadtest.py`, Phase 2) reports RPS, concurrent streams, payload sizes and p95/p99 **added** gateway latency (gateway TTFB minus upstream TTFB) against the mock upstream so the gateway's own overhead is measured in isolation. Targets are set with the workload inputs from 00-overview § 5.

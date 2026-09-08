# 05 — Deployment topology and configuration

## 1. Roles

One container image, three roles selected by `AIGW_ROLE`:

| Role | Serves | Scales on |
|------|--------|-----------|
| `gateway` | `/v1/*`, `/healthz`, `/metrics` | concurrent streams / RPS |
| `admin` | `/admin/v1/*` (control API; later the portal's backend) | admin traffic (small) |
| `worker` | outbox consumer, pending-attempt reconciliation, budget period roll, soft alerts, active health checks (docs/spec/04 §6), (Phase 2) exports | queue depth |

A portal outage (admin role down) does not affect gateway traffic: the gateway reads its config snapshot straight from PostgreSQL and continues with the last snapshot if the database is briefly unavailable (bounded by `AIGW_CONFIG_MAX_STALENESS_SECONDS`, default 300, after which the gateway returns 503 for new requests rather than enforcing stale policy).

## 2. Local development — Docker Compose (`deploy/compose`)

Services: `postgres:16`, `valkey/valkey:8`, `gateway`, `admin`, `worker`, optional `mock-upstream` (the test OpenAI-compatible server) so the whole user journey runs without external credentials.

```
docker compose up -d
docker compose exec admin aigw migrate
docker compose exec admin aigw bootstrap   # creates org/team/project/model/deployment/key from bootstrap.yaml
docker compose --profile oidc up -d keycloak   # optional: Keycloak 26 + dev realm for control-API bearer auth (01 §3.1)
python scripts/oidc_smoke.py                   # end-to-end check: tokens → /admin/v1/me, scope denial, audit actor
```

## 3. Kubernetes — Helm + ArgoCD (`deploy/helm/ai-gateway`)

- Deployments per role with independent HPA; gateway `Service` behind the cluster ingress; admin on a private ingress class (proposal: "private admin ingress").
- Config: `ConfigMap` for non-secret settings; secrets from `ExternalSecrets`/OpenBao in Phase 2 (alpha: Kubernetes `Secret` with `env:` credential refs).
- `PodDisruptionBudget` for gateway; readiness gate = DB reachable **or** snapshot within staleness bound.
- Migrations as an Argo pre-sync hook `Job` running `aigw migrate`.
- Chart values expose image, replicas, resources, `AIGW_*` env, ingress hosts, and an `argocd` block with a sample multi-source `Application` for RKE2 clusters.

## 4. Configuration (`AIGW_*`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `AIGW_ROLE` | `all` | gateway / admin / worker / all |
| `AIGW_DATABASE_URL` | postgresql+asyncpg://… | durable authority |
| `AIGW_VALKEY_URL` | redis://valkey:6379/0 | counters, cooldowns, invalidation |
| `AIGW_ADMIN_KEY` | — | alpha control-API credential |
| `AIGW_OIDC_ISSUER`, `AIGW_OIDC_AUDIENCE` | — | Keycloak realm URL and expected `aud`; enables bearer JWTs on the control API (docs/spec/01 §3.1) |
| `AIGW_OIDC_CLIENT_ID` | audience | Keycloak client whose `resource_access` roles are honoured |
| `AIGW_OIDC_JWKS_URL` | `{issuer}/protocol/openid-connect/certs` | JWKS override |
| `AIGW_OIDC_ROLE_SCOPES` | admin/operator/viewer | JSON role → scope patterns |
| `AIGW_OIDC_LEEWAY_SECONDS` | 30 | clock-skew tolerance on `exp`/`iat` |
| `AIGW_OIDC_JWKS_CACHE_SECONDS`, `AIGW_OIDC_JWKS_MIN_REFRESH_SECONDS` | 3600, 30 | signing-key cache TTL; refetch cooldown on unknown `kid` |
| `AIGW_CONFIG_REFRESH_SECONDS` | 5 | snapshot poll |
| `AIGW_HEALTH_CHECK_INTERVAL_SECONDS`, `AIGW_HEALTH_CHECK_TIMEOUT_SECONDS` | 30, 5 | worker active health checks (docs/spec/04 §6); 0 disables |
| `AIGW_HEALTH_FAILURE_THRESHOLD`, `AIGW_HEALTH_COOLDOWN_SECONDS` | 2, 90 | consecutive failed probes before cooldown; cooldown length (refreshed while down) |
| `AIGW_KEY_PICKUP_SECRET`, `AIGW_KEY_PICKUP_TTL_SECONDS` | —, 86400 | Fernet key sealing scheduled-rotation plaintexts until pickup (docs/spec/01 §3.2); pickup TTL |
| `AIGW_CACHE_MAX_ENTRY_BYTES` | 262144 | exact response cache: largest stored answer (docs/spec/04 §9) |
| `AIGW_ROUTING_STRATEGY` | adaptive | `weighted` (weights only) or `adaptive` (docs/spec/04 §5.1) |
| `AIGW_ROUTING_EWMA_ALPHA`, `AIGW_ROUTING_LATENCY_REF_MS`, `AIGW_ROUTING_QUEUE_REF`, `AIGW_ROUTING_INFLIGHT_REF` | 0.2, 1000, 8, 4 | adaptive routing smoothing and half-weight references |
| `AIGW_CONFIG_MAX_STALENESS_SECONDS` | 300 | fail-closed bound |
| `AIGW_RATELIMIT_FAIL_MODE` | open | open / closed when Valkey unavailable |
| `AIGW_MAX_ATTEMPTS` | 3 | per request |
| `AIGW_ATTEMPT_TIMEOUT_SECONDS` | 900 | pending → ambiguous |
| `AIGW_DEFAULT_MAX_OUTPUT_TOKENS` | 4096 | reservation bound when request omits max_tokens |
| `AIGW_LOG_CONTENT` | false | include prompts/completions in logs (tenant opt-in later) |
| `AIGW_OTEL_EXPORTER_OTLP_ENDPOINT` | — | traces/metrics export |

## 5. Independence acceptance gate (CI)

`scripts/independence_gate.py` fails when any of these contains `litellm` (case-insensitive): `pyproject.toml`, the lockfile, `pip freeze` output of the build environment, image layer file list (`docker image save | tar -t`), and the CycloneDX SBOM produced by `syft` in CI. The workflow `ci.yml` runs it on every pull request; it is a required check.

## 6. Backup and restore

PostgreSQL is the only stateful authority; Valkey is disposable. Restore drill (pilot gate): restore a nightly base backup + WAL to a scratch instance, run `aigw verify-restore` (row counts, latest audit event, budget totals equal Σ usage_events), then bring a gateway up against it and run the journey test.

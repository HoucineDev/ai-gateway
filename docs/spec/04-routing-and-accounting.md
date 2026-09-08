# 04 — Routing, reliability and accounting

## 1. Request pipeline (gateway role)

```
ingress → authenticate(key) → resolve scope (org/team/project) → validate request
       → rate limit (Valkey RPM/TPM) → estimate max cost → reserve budgets (PostgreSQL tx)
       → select deployment → adapter call (stream or unary) → settle (PostgreSQL tx) → usage event → response
```

Every step is a function in `aigw.gateway.pipeline` with a single `RequestContext` object so the order is explicit and testable.

## 2. Authentication

- Header `Authorization: Bearer aigw_…`. Hash with SHA-256, look up in config snapshot (`key_hash → KeyScope`). Miss → 401.
- `status != active`, `expires_at < now`, or revoked with `grace_until < now` → 401 `key_revoked` / `key_expired`.
- Model allowed if in project-visible models ∩ (`allowed_models` or all).

## 3. Rate limiting (Valkey)

Sliding-window counters keyed `rl:{key_id}:rpm:{minute}` and `rl:{key_id}:tpm:{minute}` evaluated by one Lua script (atomic check-and-increment; TPM is incremented by the *estimated* prompt+max output tokens and corrected on settlement). Project/team limits use the same script with their own keys. Valkey unavailable → behaviour per `AIGW_RATELIMIT_FAIL_MODE=open|closed` (default `open`, logged as a warning metric `aigw_ratelimit_degraded`). Allowable overshoot: at most the number of gateway replicas × in-flight requests during a Valkey failover; documented, not hidden.

## 4. Cost estimation and reservation

```
est_prompt   = adapter.estimate_prompt_tokens(req)
est_output   = req.max_tokens or capabilities.default_max_tokens or model default (4096)
reserve      = est_prompt * price.input + est_output * price.output   (per million, Decimal)
```

Reservation transaction (`aigw.gateway.accounting.reserve`):

1. Collect applicable budgets in scope order key → project → team → org, active period only (roll period if `period_start` expired, inside the same transaction).
2. `SELECT … FOR UPDATE` in a fixed order (by `id`) to avoid deadlocks.
3. For each: `available = limit + temporary_increase(if temporary_until > now) - spent - reserved`; if `available < reserve` → rollback, 429 `budget_exceeded` naming the scope.
4. `reserved += reserve`; insert `request_attempts(status=pending, reserved_amount=reserve, dedup_key=request_id:attempt_no)`.
5. Commit before dispatching upstream.

Settlement transaction (`settle`):

1. Compute `actual` from reported/estimated usage and the pinned `price_id`. For `ambiguous`/`cancelled` without usage → `actual = reserved` (conservative).
2. For each budget: `reserved -= reserve; spent += actual` (`FOR UPDATE`, same order).
3. Update attempt status/usage; insert `usage_events`; enqueue `outbox(topic=usage.settled)` for alerts and exporters.
4. Soft alert: if `spent / limit >= soft_alert_pct` crossed in this transaction → `outbox(topic=budget.soft_alert)`.

Reconciliation worker: attempts still `pending` after `AIGW_ATTEMPT_TIMEOUT_SECONDS` (default 900) are settled as `ambiguous` at `reserved`. Provider-invoice reconciliation is Phase 2.

Reservation before the first upstream byte means budget enforcement is strict under concurrency (two concurrent requests cannot both pass on the last unit of budget). Throughput ceiling of this design is the row lock on the shared org budget; the proposal's mitigation (bounded regional/replica sub-allocations) is a Phase 3 item and the schema (`budgets` per scope) already accommodates child allocations.

## 5. Deployment selection

Input: logical model, request, key scope, config snapshot, health state.

1. Candidates = active deployments of the model, `cooldown_until < now`, capabilities satisfy the request (streaming, tools, json mode, vision, `max_input_tokens >= est_prompt`), region satisfies key/project residency tag if set.
2. Group by `priority` ascending; within the best group choose weighted-random by `weight` (Phase 1). Phase 2 adds latency-EWMA and vLLM queue-pressure inputs (`GPU-aware routing`).
3. Ordered fallback list = remaining candidates in the same order rule.
4. None eligible → 503 `no_eligible_deployment` with the reasons per deployment in the diagnostics record (never in the client body beyond a summary).

## 6. Retries, cooldown and fallback

| Error class | Same deployment retry | Fallback to next | Cooldown |
|-------------|-----------------------|------------------|----------|
| rate_limited | no | yes | `Retry-After` or 10 s |
| overloaded | no | yes | 10 s |
| timeout_before_send | once | yes | 5 s after 3 consecutive |
| upstream_error | once | yes | 30 s after 3 consecutive |
| authentication | no | yes | 300 s |
| invalid_request, content_filter | no | no | — |
| ambiguous | no | **no** | — |

Rules:

- Fallback is only attempted while **no client-visible byte** has been sent. Once streaming starts, an upstream failure ends the stream with an error event; the attempt is `ambiguous` unless usage was received.
- Every attempt is its own `request_attempts` row and its own reservation: the previous attempt's reservation is released (settled at 0 or at reported usage) before the next one is reserved. Max attempts per request: `AIGW_MAX_ATTEMPTS` (default 3).
- Cooldown state lives in Valkey (`cd:{deployment_id}` with TTL) and is mirrored in-process; persistent operator cooldowns use `deployments.cooldown_until`.
- Health: passive (error classes above) plus active checks (§6).

## 7. Streaming guarantees

- Backpressure: the gateway awaits the client write before pulling the next upstream chunk (no unbounded buffering).
- Tool-call fragments and `finish` events are forwarded in order; the gateway appends a synthetic usage chunk if the adapter reports `usage` after `finish` or not at all.
- Client disconnect → upstream request cancelled → settlement with whatever usage is known, else conservative.
- Full-response moderation (Phase 3 guardrails) buffers and is an explicit per-tenant policy; incremental moderation is a distinct policy with documented detection lag.

## 8. Observability

Per request: structured log line (request_id, key_id, org/project, model, deployment, status, tokens, cost, latency, ttfb) with content excluded by default; OpenTelemetry span per pipeline stage; Prometheus counters/histograms `aigw_requests_total{model,provider,status}`, `aigw_request_latency_seconds`, `aigw_ttfb_seconds`, `aigw_tokens_total{direction}`, `aigw_cost_total`, `aigw_budget_rejections_total`, `aigw_deployment_cooldowns_total`.

## 6. Active health checks (worker)

Every `AIGW_HEALTH_CHECK_INTERVAL_SECONDS` (default 30; 0 disables) the worker probes each *active* deployment with an
authenticated `GET {base_url}/models` (Anthropic: `/v1/models`) using the deployment's credential reference and extra
headers, bounded by `AIGW_HEALTH_CHECK_TIMEOUT_SECONDS`. Transport errors, timeouts, an unresolvable credential and
5xx answers are failures; any other status means the upstream is reachable (servers without `/models` answer 404).

Results land in `deployment_health` (docs/spec/02): `status` healthy | degraded (failing, below threshold) |
unhealthy, `consecutive_failures`, `latency_ms`, `error`, `checked_at`. After `AIGW_HEALTH_FAILURE_THRESHOLD`
consecutive failures the worker sets the deployment's Valkey cooldown (`cd:{id}`, `AIGW_HEALTH_COOLDOWN_SECONDS`,
refreshed each sweep while it stays down), which the router already honours (§4, rejection reason `cooldown`); a
healthy probe clears it immediately. PostgreSQL remains the authority; Valkey only carries the signal to gateways,
so with Valkey down health is still recorded and visible but does not steer routing (same fail-open stance as rate
limits). `GET /admin/v1/models` returns each deployment's latest `health`; the portal shows it in the catalog.

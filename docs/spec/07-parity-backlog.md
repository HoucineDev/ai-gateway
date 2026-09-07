# 07 — Parity backlog

Feature families from the proposal's reference inventory, with the phase in which each is implemented. "Ref" indicates the reference documentation family (LiteLLM docs, pinned 2026-09-07) used purely as an inventory. Status: **P1** = in the alpha codebase, **P2/P3/P4** = planned, per the proposal's roadmap.

## Access, identity and tenancy

| Item | Phase | Notes |
|------|-------|-------|
| Virtual keys, hashing, prefix display, expiry | P1 | |
| Organizations / teams / projects hierarchy | P1 | |
| Key `allowed_models`, per-key RPM/TPM | P1 | |
| Key revocation with invalidation | P1 | Valkey pub/sub + snapshot refresh |
| Key rotation with grace period | P1 (API) / P3 (scheduled) | |
| Admin key auth | P1 | |
| Keycloak OIDC JWT on control API | P2 ✓ | JWKS validation, roles claim → scopes (docs/spec/01 §3.1, `tests/test_admin_oidc.py`) |
| Delegated roles (org owner, team owner, project member, service account) | P2 | |
| SCIM 2.0 provisioning service | P3 | separate deliverable |
| CIDR restrictions, private admin ingress, required metadata | P2 | |

## Spend, budgets and limits

| Item | Phase | Notes |
|------|-------|-------|
| Owned pricing registry with versions/effective dates | P1 | |
| Reservation/settlement ledger, fixed-point | P1 | |
| Budgets per org/team/project/key; total/daily/monthly | P1 | |
| Soft alerts (outbox) | P1 | delivery adapters (email/webhook) P2 |
| Temporary budget increases with expiry | P1 | |
| Validated allocation tags | P1 | `metadata` keys validated against project settings |
| Provider invoice reconciliation | P2 | |
| Bounded regional/replica sub-allocations | P3 | |
| Local GPU allocation vs provider charges (differentiator) | P2 | price rows with `source=internal-allocation` |

## Routing, reliability and caching

| Item | Phase | Notes |
|------|-------|-------|
| Weighted/priority deployment selection | P1 | |
| Capability-aware eligibility (tools, json, vision, context) | P1 | |
| Retries, cooldowns, fallback chains, no post-first-byte failover | P1 | |
| Active health checks | P2 | worker |
| Latency-EWMA and vLLM queue-pressure routing (GPU-aware) | P2 | reads vLLM `/metrics`; admission control stays local |
| Budget-, tag-, priority-based routing | P3 | |
| Exact response cache (tenant/policy scoped) | P2 | |
| Semantic cache (opt-in, single-turn) | P4 | |
| Routing explanations and policy simulation (differentiator) | P3 | diagnostics record already stores the decision |
| Multi-region config distribution | P4 | |

## APIs

| Item | Phase | Notes |
|------|-------|-------|
| Chat completions (stream/unary, tools, JSON modes) | P1 | |
| Embeddings | P1 | |
| Models listing | P1 | |
| Token counting endpoint | P2 | |
| Responses API | P2 | |
| Anthropic Messages native wire format on ingress | P2 | |
| Gemini generateContent / Bedrock Converse native ingress | P4 | |
| Rerank, image generation, audio (transcription/speech), realtime, video, OCR | P4 | |
| Files, batches, fine-tuning, evaluations passthrough | P4 | resource ownership table |
| Passthrough / legacy compatibility | P4 | only on demonstrated demand |

## Providers

| Provider | Phase |
|----------|-------|
| OpenAI-compatible local (vLLM, KServe, TGI…) | P1 |
| OpenAI | P1 |
| Anthropic | P1 |
| Azure OpenAI | P2 |
| Gemini / Vertex | P2 |
| Bedrock | P2 |
| Mistral, Cohere, others | on demand |

## Logging, audit and observability

| Item | Phase | Notes |
|------|-------|-------|
| Append-only audit events for all control writes | P1 | |
| Structured request logs without content by default | P1 | |
| Prometheus metrics | P1 | |
| OpenTelemetry traces | P1 (pipeline spans) | exporter config P2 |
| Per-tenant/key logging destinations with redaction (S3/GCS/Azure) | P3 | |
| Retention jobs and export | P3 | |
| Immutable archive | P3 | |

## Guardrails and agents

| Item | Phase | Notes |
|------|-------|-------|
| Pre/post policy pipeline with replaceable detectors | P3 | fail-open/closed per tenant, timeouts |
| Presidio / LLM Guard / provider moderation adapters | P3 | |
| MCP server registry, namespaced tools, tool permissions, accounting | P4 | Streamable HTTP first |
| Agent run limits (cost, tool calls, retries, elapsed) | P4 | |
| Approval workflows | P3 | |
| Task-level evaluation, multilingual (FR/AR/Darija) benchmarks | P4 | |

## Portal

| Item | Phase |
|------|-------|
| Control API sufficient for a UI | P1 |
| React admin portal (overview, management, governance, developer portal) | P2 |
| Branding, custom docs, notification templates | P3 |

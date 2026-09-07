# Independent AI Gateway — Implementation Specification (Phase 0)

Version 0.1 — 7 September 2026 — derived from *Independent AI Gateway: Feature analysis and implementation proposal* v1.0.

## 1. Decision recap

Build a self-hosted AI gateway and management platform whose code, provider adapters, policy integrations and data model are independent of LiteLLM. LiteLLM is a competitive reference inventory only. No LiteLLM package, proxy, SDK, container, copied adapter code, runtime service, or automatic download of its pricing catalog. No artificial licensing caps on users, organizations, models or requests.

General-purpose open-source components (FastAPI, SQLAlchemy, PostgreSQL, Valkey, Keycloak, OpenBao, OpenTelemetry) and official provider SDKs remain acceptable subject to dependency and license review. The alpha uses direct HTTP through HTTPX rather than provider SDKs, which keeps the dependency tree small and the adapter behaviour fully owned.

## 2. Scope of this specification

This document set covers what the proposal's "Next" section asked for:

| Doc | Content |
|-----|---------|
| 01-api-contracts.md | Public inference API, control (admin) API, error envelope, streaming format |
| 02-data-model.md | PostgreSQL schema: tenancy, keys, models/deployments, budgets, ledger, audit, outbox |
| 03-adapter-contract.md | Provider adapter interface, capability declarations, canonical types, error classes |
| 04-routing-and-accounting.md | Deployment selection, retries/fallback/cooldown, reservation-settlement ledger, rate limits |
| 05-deployment.md | Roles (gateway/admin/worker), Compose, Helm/ArgoCD, configuration and staleness policy |
| 06-acceptance.md | Release gates (alpha, pilot, governance, parity) and the minimum validation suite |
| 07-parity-backlog.md | Feature-family backlog against the LiteLLM reference inventory, with phase assignment |

Everything not listed as *Phase 1* in 07-parity-backlog.md is out of scope for the alpha codebase, but the schema and contracts are designed so those items are additive rather than breaking.

## 3. Reference release pinning

The proposal requires pinning the LiteLLM reference release before implementation so feature classifications are stable. Pin: **LiteLLM documentation as of 2026-09-07** (the date the proposal was reviewed). The backlog records the documented feature family, not release-level behaviour; behaviour parity for each provider/endpoint pair is established by our own contract fixtures, never by reading LiteLLM code.

## 4. Non-negotiable properties

1. **Independence gate** — CI fails if `litellm` (or any package whose name contains `litellm`) appears in the lockfile, installed environment, container image, or SBOM. See `scripts/independence_gate.py`.
2. **Tenant scope on every query** — every read/write of tenant-owned rows carries the tenant id in the predicate; row-level security is enabled on tenant tables in Phase 2.
3. **Durable money** — budgets and spend live in PostgreSQL using fixed-point `NUMERIC(20,8)`; Valkey only accelerates RPM/TPM counters and never decides a monetary limit.
4. **No silent stream restart** — once a byte of content reaches the client, the gateway never fails over to another deployment for that request.
5. **Conservative uncertainty** — if the upstream outcome is ambiguous (timeout after dispatch, stream cut without usage), the attempt settles at the reserved maximum and is flagged `ambiguous` for reconciliation.
6. **Owned pricing registry** — prices are rows we curate with `effective_from` and a `version`; every ledger row references the price version used.

## 5. Inputs still required (from the proposal)

Confirm: initial providers and APIs (default: OpenAI-compatible local, OpenAI, Anthropic); anticipated RPS and concurrent streams; tenant count; identity provider (default: Keycloak); data residency and retention; engineering capacity; availability/recovery targets; internal-only vs external customers (default: internal first, tenant-aware).

Defaults above are encoded as configuration, so changing an answer changes settings rather than architecture.

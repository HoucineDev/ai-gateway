/** Typed client for the control API (/admin/v1). Credentials: Keycloak bearer token from lib/auth when signed in,
 * otherwise the admin key from localStorage (docs/spec/01 §3.1). */
import { auth, type OidcConfig } from "./auth";

export type Org = { id: string; name: string; slug: string; status: string; created_at: string };
export type Team = { id: string; org_id: string; name: string; status: string };
export type Project = { id: string; org_id: string; team_id: string; name: string; status: string; settings: Record<string, unknown> };
export type Key = {
  id: string; project_id: string; name: string; key_prefix: string; status: string; expires_at: string | null;
  allowed_models: string[] | null; rpm_limit: number | null; tpm_limit: number | null; created_at: string; grace_until: string | null;
  key?: string;
};
export type DeploymentHealth = {
  status: "healthy" | "degraded" | "unhealthy" | "unknown"; consecutive_failures: number; latency_ms: number | null;
  error: string | null; checked_at: string | null;
  queue_waiting: number | null; queue_running: number | null; kv_cache_usage: number | null; metrics_at: string | null; // vLLM /metrics
};
export type Deployment = {
  id: string; model_id: string; name: string; provider: string; provider_model: string; base_url: string | null;
  credential_ref: string; weight: number; priority: number; status: string; cooldown_until: string | null; region: string | null;
  health: DeploymentHealth | null; // latest active probe (docs/spec/04 §6); null until the worker has probed it
};
export type Model = {
  id: string; org_id: string | null; name: string; display_name: string | null; modalities: string[]; context_window: number | null;
  supports_tools: boolean; supports_json_schema: boolean; supports_vision: boolean; status: string; deployments: Deployment[];
};
export type Budget = {
  id: string; scope_type: string; scope_id: string; limit_amount: string; period: string; spent_amount: string; reserved_amount: string;
  available_amount: string; soft_alert_pct: number | null; temporary_increase: string; temporary_until: string | null; status: string;
};
export type UsageEvent = {
  id: string; request_id: string; org_id: string; project_id: string; key_id: string; model_name: string; provider: string;
  endpoint: string; prompt_tokens: number; completion_tokens: number; cost: string; usage_source: string; status: string;
  latency_ms: number | null; ttfb_ms: number | null; stream: boolean; tags: Record<string, string>; created_at: string;
};
export type Attempt = {
  id: string; attempt_no: number; deployment_id: string; provider: string; provider_model: string; status: string;
  reserved_amount: string; settled_amount: string | null; prompt_tokens: number | null; completion_tokens: number | null;
  usage_source: string | null; upstream_request_id: string | null; error_class: string | null; error_message: string | null;
  routing: { candidates?: string[]; rejected?: Record<string, string>; chosen?: string; attempt?: number };
  started_at: string; first_byte_at: string | null; ended_at: string | null;
};
export type AuditEvent = {
  id: string; actor_type: string; actor_id: string; action: string; target_type: string; target_id: string | null;
  before: unknown; after: unknown; created_at: string;
};
export type Grant = { role: string; scope_type: string; scope_id: string; org_id: string; team_id: string | null; project_id: string | null };
export type Me = { actor_type: "admin_key" | "user"; actor_id: string; roles: string[]; scopes: string[]; global_scopes: string[]; grants: Grant[] };
export type RoleBinding = {
  id: string; subject: string; subject_kind: string; role: "org_owner" | "team_owner" | "project_member";
  scope_type: "organization" | "team" | "project"; scope_id: string; org_id: string; team_id: string | null; project_id: string | null;
  status: string; created_by: string; created_at: string; revoked_at: string | null;
};
export type AuthConfig = { admin_key: boolean; oidc: OidcConfig | null };
export type Overview = {
  since: string; requests: number; cost: string; tokens: number; p95_latency_ms: number | null; failed: number;
  by_model: { model: string; requests: number; cost: string }[]; by_status: Record<string, number>;
};

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public param?: string) {
    super(message);
  }
}

const KEY = "aigw.adminKey";
const BASE = "aigw.baseUrl";

export const settings = {
  get adminKey() { return localStorage.getItem(KEY) ?? ""; },
  set adminKey(v: string) { localStorage.setItem(KEY, v); },
  get baseUrl() { return localStorage.getItem(BASE) ?? ""; },
  set baseUrl(v: string) { localStorage.setItem(BASE, v); },
};

let oidcConfig: OidcConfig | null = null;
/** Set by SessionProvider once GET /auth/config answers; needed for silent token refresh. */
export const setOidcConfig = (c: OidcConfig | null) => { oidcConfig = c; };

async function credentials(): Promise<Record<string, string>> {
  const token = await auth.accessToken(oidcConfig);
  if (token) return { authorization: `Bearer ${token}` };
  return settings.adminKey ? { "x-admin-key": settings.adminKey } : {};
}

async function call<T>(method: string, path: string, body?: unknown, params?: Record<string, string | undefined>, unauthenticated = false): Promise<T> {
  const url = new URL(settings.baseUrl + "/admin/v1" + path, window.location.origin);
  for (const [k, v] of Object.entries(params ?? {})) if (v !== undefined && v !== "") url.searchParams.set(k, v);
  const creds = unauthenticated ? {} : await credentials();
  const res = await fetch(url, {
    method,
    headers: { "content-type": "application/json", ...creds },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let err = { message: res.statusText, code: "http_" + res.status, param: undefined as string | undefined };
    try { err = (await res.json()).error ?? err; } catch { /* non-JSON */ }
    if (res.status === 401 && "authorization" in creds) auth.clear(); // token rejected: drop the session, UI falls back to sign-in
    throw new ApiError(res.status, err.code, err.message, err.param);
  }
  return res.json() as Promise<T>;
}

export const api = {
  authConfig: () => call<AuthConfig>("GET", "/auth/config", undefined, undefined, true),
  me: () => call<Me>("GET", "/me"),
  overview: (hours = 24, org_id?: string) => call<Overview>("GET", "/overview", undefined, { hours: String(hours), org_id }),
  orgs: () => call<{ data: Org[] }>("GET", "/organizations"),
  createOrg: (b: { name: string; slug: string }) => call<Org>("POST", "/organizations", b),
  teams: (org: string) => call<{ data: Team[] }>("GET", `/organizations/${org}/teams`),
  createTeam: (org: string, b: { name: string }) => call<Team>("POST", `/organizations/${org}/teams`, b),
  projects: (team: string) => call<{ data: Project[] }>("GET", `/teams/${team}/projects`),
  createProject: (team: string, b: { name: string; settings?: Record<string, unknown> }) => call<Project>("POST", `/teams/${team}/projects`, b),
  keys: (project: string) => call<{ data: Key[] }>("GET", `/projects/${project}/keys`),
  createKey: (project: string, b: Partial<Key> & { name: string }) => call<Key>("POST", `/projects/${project}/keys`, b),
  revokeKey: (id: string) => call<Key>("POST", `/keys/${id}/revoke`),
  rotateKey: (id: string, grace_seconds = 3600) => call<Key & { previous_key_grace_until: string }>("POST", `/keys/${id}/rotate`, { grace_seconds }),
  models: (org_id?: string) => call<{ data: Model[] }>("GET", "/models", undefined, { org_id }),
  createModel: (b: Partial<Model> & { name: string }) => call<Model>("POST", "/models", b),
  patchModel: (id: string, b: Partial<Model>) => call<Model>("PATCH", `/models/${id}`, b),
  createDeployment: (model: string, b: Partial<Deployment> & { name: string; provider: string; provider_model: string }) =>
    call<Deployment>("POST", `/models/${model}/deployments`, b),
  patchDeployment: (id: string, b: Partial<Deployment>) => call<Deployment>("PATCH", `/deployments/${id}`, b),
  cooldown: (id: string, seconds: number) => call<Deployment>("POST", `/deployments/${id}/cooldown`, { seconds }),
  prices: () => call<{ data: { id: string; provider: string; provider_model: string; version: number; input_per_million: string; output_per_million: string; effective_from: string; source: string | null }[] }>("GET", "/prices"),
  createPrice: (b: { provider: string; provider_model: string; input_per_million: string; output_per_million: string; source?: string }) => call("POST", "/prices", b),
  budgets: (p: { scope_type?: string; scope_id?: string; org_id?: string }) => call<{ data: Budget[] }>("GET", "/budgets", undefined, p),
  createBudget: (b: { scope_type: string; scope_id: string; limit_amount: string; period: string; soft_alert_pct?: number }) => call<Budget>("POST", "/budgets", b),
  patchBudget: (id: string, b: Partial<{ limit_amount: string; soft_alert_pct: number; status: string }>) => call<Budget>("PATCH", `/budgets/${id}`, b),
  tempIncrease: (id: string, b: { amount: string; until: string }) => call<Budget>("POST", `/budgets/${id}/temporary-increase`, b),
  usage: (p: { scope_type: string; scope_id: string; group_by?: string; from?: string; to?: string }) =>
    call<{ data: { group: string; requests: number; prompt_tokens: number; completion_tokens: number; cost: string }[]; total_cost: string }>("GET", "/usage", undefined, p),
  requests: (p: { org_id?: string; project_id?: string; key_id?: string; limit?: string }) => call<{ data: UsageEvent[] }>("GET", "/requests", undefined, p),
  request: (id: string) => call<{ request_id: string; attempts: Attempt[]; usage: UsageEvent[] }>("GET", `/requests/${id}`),
  audit: (p: { org_id?: string; target_type?: string; target_id?: string; limit?: string }) => call<{ data: AuditEvent[] }>("GET", "/audit", undefined, p),
  configVersion: () => call<{ version: number }>("GET", "/config/version"),
  roleBindings: (p: { scope_type?: string; scope_id?: string; org_id?: string; subject?: string }) => call<{ data: RoleBinding[] }>("GET", "/role-bindings", undefined, p),
  createRoleBinding: (b: { subject: string; role: RoleBinding["role"]; scope_type: RoleBinding["scope_type"]; scope_id: string; subject_kind?: string }) =>
    call<RoleBinding>("POST", "/role-bindings", b),
  revokeRoleBinding: (id: string) => call<RoleBinding>("POST", `/role-bindings/${id}/revoke`),
};

export const fmtMoney = (v: string | number | null | undefined, digits = 4) =>
  v === null || v === undefined ? "—" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
export const fmtInt = (v: number | null | undefined) => (v === null || v === undefined ? "—" : v.toLocaleString());
export const fmtDate = (v: string | null | undefined) => (v ? new Date(v).toLocaleString() : "—");
export const short = (id: string) => "…" + id.slice(-8);  // UUIDv7 prefixes are time-based; the tail is the distinctive part

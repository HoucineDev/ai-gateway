/** Keycloak login for the portal: OIDC authorization-code flow with PKCE (docs/spec/01 §3.1).
 *
 * No library: discovery document + WebCrypto for the code challenge. Tokens live in sessionStorage (per tab,
 * gone on close); the access token is refreshed with the refresh token shortly before it expires. The control
 * API tells us the issuer and client id through GET /admin/v1/auth/config, so the portal build has no
 * environment-specific configuration.
 */

export type OidcConfig = { issuer: string; client_id: string; audience: string | null };
type Discovery = { authorization_endpoint: string; token_endpoint: string; end_session_endpoint?: string };
type Tokens = { access_token: string; refresh_token?: string; id_token?: string; expires_at: number };

const TOKENS = "aigw.oidc.tokens";
const PENDING = "aigw.oidc.pending";
const REFRESH_SKEW_MS = 30_000;
export const AUTH_CHANGED = "aigw:auth-changed";

const discoveryCache = new Map<string, Promise<Discovery>>();
function discover(issuer: string): Promise<Discovery> {
  let p = discoveryCache.get(issuer);
  if (!p) {
    p = fetch(`${issuer}/.well-known/openid-configuration`).then((r) => {
      if (!r.ok) throw new Error(`OIDC discovery failed (${r.status}) for ${issuer}`);
      return r.json() as Promise<Discovery>;
    });
    discoveryCache.set(issuer, p);
  }
  return p;
}

const b64url = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const randomString = (n = 32) => b64url(crypto.getRandomValues(new Uint8Array(n)));
async function challengeFor(verifier: string) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return b64url(new Uint8Array(digest));
}

const redirectUri = () => `${window.location.origin}/callback`;

function read(): Tokens | null {
  try { const raw = sessionStorage.getItem(TOKENS); return raw ? (JSON.parse(raw) as Tokens) : null; } catch { return null; }
}
function write(t: Tokens | null) {
  if (t) sessionStorage.setItem(TOKENS, JSON.stringify(t)); else sessionStorage.removeItem(TOKENS);
  notifyAuthChanged();
}

/** Tell SessionProvider that credentials changed (OIDC tokens or the admin key). */
export function notifyAuthChanged(): void {
  window.dispatchEvent(new Event(AUTH_CHANGED));
}

async function tokenRequest(cfg: OidcConfig, params: Record<string, string>): Promise<Tokens> {
  const { token_endpoint } = await discover(cfg.issuer);
  const res = await fetch(token_endpoint, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: cfg.client_id, ...params }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error_description ?? body.error ?? `token request failed (${res.status})`);
  return {
    access_token: body.access_token,
    refresh_token: body.refresh_token,
    id_token: body.id_token,
    expires_at: Date.now() + Number(body.expires_in ?? 300) * 1000,
  };
}

let refreshing: Promise<string | null> | null = null;

export const auth = {
  /** True when a session exists (the access token may still need a refresh). */
  isAuthenticated(): boolean {
    const t = read();
    return Boolean(t && (t.expires_at > Date.now() || t.refresh_token));
  },

  /** Current access token, refreshed when it expires within 30 s. Null when not signed in or refresh failed. */
  async accessToken(cfg?: OidcConfig | null): Promise<string | null> {
    const t = read();
    if (!t) return null;
    if (t.expires_at - Date.now() > REFRESH_SKEW_MS) return t.access_token;
    if (!t.refresh_token || !cfg) return t.expires_at > Date.now() ? t.access_token : null;
    refreshing ??= tokenRequest(cfg, { grant_type: "refresh_token", refresh_token: t.refresh_token })
      .then((n) => { write(n); return n.access_token; })
      .catch(() => { write(null); return null; })
      .finally(() => { refreshing = null; });
    return refreshing;
  },

  /** Redirect to Keycloak. `returnTo` is restored after the callback. */
  async login(cfg: OidcConfig, returnTo = window.location.pathname): Promise<void> {
    const { authorization_endpoint } = await discover(cfg.issuer);
    const verifier = randomString(48);
    const state = randomString(16);
    sessionStorage.setItem(PENDING, JSON.stringify({ verifier, state, returnTo }));
    const url = new URL(authorization_endpoint);
    Object.entries({
      response_type: "code", client_id: cfg.client_id, redirect_uri: redirectUri(), scope: "openid profile email",
      state, code_challenge: await challengeFor(verifier), code_challenge_method: "S256",
    }).forEach(([k, v]) => url.searchParams.set(k, v));
    window.location.assign(url.toString());
  },

  /** Exchange the code on /callback. Returns the path to navigate to. */
  async handleCallback(cfg: OidcConfig): Promise<string> {
    const q = new URLSearchParams(window.location.search);
    if (q.get("error")) throw new Error(q.get("error_description") ?? q.get("error") ?? "login failed");
    const raw = sessionStorage.getItem(PENDING);
    sessionStorage.removeItem(PENDING);
    if (!raw) throw new Error("No login in progress (sessionStorage was cleared or the callback was opened directly)");
    const pending = JSON.parse(raw) as { verifier: string; state: string; returnTo: string };
    if (q.get("state") !== pending.state) throw new Error("State mismatch — please sign in again");
    const code = q.get("code");
    if (!code) throw new Error("Missing authorization code");
    write(await tokenRequest(cfg, { grant_type: "authorization_code", code, redirect_uri: redirectUri(), code_verifier: pending.verifier }));
    return pending.returnTo && pending.returnTo !== "/callback" ? pending.returnTo : "/overview";
  },

  /** Drop the local session and end the Keycloak session (RP-initiated logout) when the issuer supports it. */
  async logout(cfg: OidcConfig | null): Promise<void> {
    const t = read();
    write(null);
    if (!cfg) return;
    const { end_session_endpoint } = await discover(cfg.issuer).catch(() => ({ end_session_endpoint: undefined }));
    if (!end_session_endpoint) return;
    const url = new URL(end_session_endpoint);
    url.searchParams.set("client_id", cfg.client_id);
    url.searchParams.set("post_logout_redirect_uri", `${window.location.origin}/login`);
    if (t?.id_token) url.searchParams.set("id_token_hint", t.id_token);
    window.location.assign(url.toString());
  },

  clear() { write(null); },
};

import { type FormEvent, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Info, KeyRound, LogIn } from "lucide-react";
import { ApiError, api, settings } from "../lib/api";
import { notifyAuthChanged } from "../lib/auth";
import { useSession } from "../lib/session";

/** Dedicated sign-in page (docs/spec/01 §3.1): admin key as the bootstrap credential, Keycloak as single sign-on.
 *  Unauthenticated routes redirect here with ?redirect_to=<path> and return there after a successful sign-in. */
export default function Login() {
  const { config, login } = useSession();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const returnTo = params.get("redirect_to") || "/overview";
  const [key, setKey] = useState("");
  const [base, setBase] = useState(settings.baseUrl);
  const [advanced, setAdvanced] = useState(Boolean(settings.baseUrl));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const oidc = config?.oidc ?? null;
  const adminKeyEnabled = config ? config.admin_key : true;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    settings.baseUrl = base.trim();
    settings.adminKey = key.trim();
    try {
      await api.me(); // proves the key before we let the user in
      notifyAuthChanged();
      await qc.invalidateQueries();
      navigate(returnTo, { replace: true });
    } catch (err) {
      settings.adminKey = "";
      setError(err instanceof ApiError ? `${err.message} (${err.code})` : err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <div className="card w-full max-w-md shadow-xl">
        <div className="mb-5 flex items-center justify-center gap-2">
          <span className="inline-block h-3 w-3 rounded-full bg-accent" aria-hidden="true" />
          <span className="text-xl font-semibold tracking-tight">AI Gateway</span>
        </div>
        <h1 className="text-center text-lg font-semibold">Sign in</h1>
        <p className="mb-5 text-center text-sm text-muted-foreground">Access the gateway control plane.</p>

        <div className="mb-5 rounded-md border border-info/40 bg-info/10 p-3 text-sm" role="note">
          <div className="flex items-center gap-2 font-medium"><Info size={16} aria-hidden="true" /> Default credential</div>
          <p className="mt-1 text-muted-foreground">
            The bootstrap credential is the <span className="mono">AIGW_ADMIN_KEY</span> of the admin role (compose default{" "}
            <span className="mono">change-me-admin</span>). Single sign-on is available once{" "}
            <span className="mono">AIGW_OIDC_ISSUER</span> points at your Keycloak realm — see <span className="mono">docs/runbook.md</span>.
          </p>
        </div>

        {adminKeyEnabled ? (
          <form onSubmit={submit} className="space-y-3">
            <div>
              <label htmlFor="login-key" className="label">Admin key</label>
              <input id="login-key" className="input mono" type="password" autoComplete="off" autoFocus required value={key} onChange={(e) => setKey(e.target.value)} />
            </div>
            {advanced ? (
              <div>
                <label htmlFor="login-base" className="label">Control API base URL</label>
                <input id="login-base" className="input" placeholder="http://localhost:8081" value={base} onChange={(e) => setBase(e.target.value)} />
                <p className="mt-1 text-xs text-muted-foreground">Leave empty when the portal is served by the admin role or proxied by Vite.</p>
              </div>
            ) : (
              <button type="button" className="text-xs text-muted-foreground underline-offset-2 hover:underline" onClick={() => setAdvanced(true)}>Advanced: control API base URL</button>
            )}
            {error && <div role="alert" className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</div>}
            <button className="btn btn-primary w-full justify-center" disabled={busy || !key.trim()}>
              <KeyRound size={16} aria-hidden="true" /> {busy ? "Checking…" : "Sign in"}
            </button>
          </form>
        ) : (
          <p className="text-sm text-muted-foreground">This control plane accepts single sign-on only.</p>
        )}

        <button
          className="btn btn-secondary mt-3 w-full justify-center"
          disabled={!oidc}
          title={oidc ? `Sign in through ${oidc.issuer}` : "Single sign-on is not configured on this control plane"}
          onClick={() => void login(returnTo)}
        >
          <LogIn size={16} aria-hidden="true" /> Sign in with SSO
        </button>
        {config && !oidc && <p className="mt-2 text-center text-xs text-muted-foreground">SSO not configured</p>}
      </div>
    </div>
  );
}

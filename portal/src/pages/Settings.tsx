import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, settings } from "../lib/api";
import { useSession } from "../lib/session";
import { ErrorBanner, Field, Page } from "../components/ui";

export default function SettingsPage() {
  const [key, setKey] = useState(settings.adminKey);
  const [base, setBase] = useState(settings.baseUrl);
  const [saved, setSaved] = useState(false);
  const { config, me, signedIn, login, logout, error } = useSession();
  const version = useQuery({ queryKey: ["config-version", saved, signedIn], queryFn: api.configVersion, enabled: signedIn || Boolean(settings.adminKey) });
  return (
    <Page title="Settings">
      <div className="card max-w-xl">
        <h2 className="mb-1 text-base font-semibold">Single sign-on</h2>
        {!config && <p className="text-sm text-muted-foreground">Checking the control plane…</p>}
        {config && !config.oidc && <p className="text-sm text-muted-foreground">Not configured. Set <span className="mono">AIGW_OIDC_ISSUER</span> and <span className="mono">AIGW_OIDC_AUDIENCE</span> on the admin role to sign in with Keycloak; the admin key below keeps working.</p>}
        {config?.oidc && (
          <div className="space-y-3 text-sm">
            <p className="text-muted-foreground">Issuer <span className="mono text-foreground">{config.oidc.issuer}</span> · client <span className="mono text-foreground">{config.oidc.client_id}</span></p>
            {signedIn && me?.actor_type === "user" && (
              <p>Signed in as <span className="font-medium">{me.actor_id}</span>{me.roles.length > 0 && <> with roles <span className="mono">{me.roles.join(", ")}</span></>} — {me.scopes.length} scopes.</p>
            )}
            {signedIn && me === null && <ErrorBanner error={error ?? new Error("Your token was rejected by the control API")} />}
            <div className="flex gap-2">
              {!signedIn && <button className="btn btn-primary" onClick={() => void login("/overview")}>Sign in with Keycloak</button>}
              {signedIn && <button className="btn btn-secondary" onClick={() => void logout()}>Sign out</button>}
            </div>
          </div>
        )}
      </div>
      <div className="card max-w-xl">
        <h2 className="mb-1 text-base font-semibold">Admin key</h2>
        <form onSubmit={(e) => { e.preventDefault(); settings.adminKey = key; settings.baseUrl = base; setSaved(true); }}>
          <Field label="Control API base URL" hint="Leave empty when the portal is served by the admin role or proxied by Vite.">
            {(id) => <input id={id} className="input" value={base} onChange={(e) => setBase(e.target.value)} placeholder="http://localhost:8081" />}
          </Field>
          <Field label="Admin key" hint="AIGW_ADMIN_KEY of the admin role, stored in this browser only. Bootstrap credential with every scope; ignored while you are signed in with Keycloak.">
            {(id) => <input id={id} className="input mono" type="password" value={key} onChange={(e) => setKey(e.target.value)} autoComplete="off" />}
          </Field>
          <div className="flex items-center gap-3">
            <button type="submit" className="btn btn-primary">Save</button>
            {saved && <span className="text-sm text-accent" role="status">Saved</span>}
          </div>
        </form>
        <div className="mt-5 border-t border-border pt-4 text-sm">
          {version.isError && <ErrorBanner error={version.error} />}
          {version.data && <p className="text-muted-foreground">Connected — configuration version <span className="mono text-foreground">{version.data.version}</span></p>}
        </div>
      </div>
    </Page>
  );
}

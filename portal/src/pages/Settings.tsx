import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, settings } from "../lib/api";
import { ErrorBanner, Field, Page } from "../components/ui";

export default function SettingsPage() {
  const [key, setKey] = useState(settings.adminKey);
  const [base, setBase] = useState(settings.baseUrl);
  const [saved, setSaved] = useState(false);
  const version = useQuery({ queryKey: ["config-version", saved], queryFn: api.configVersion, enabled: Boolean(settings.adminKey) });
  return (
    <Page title="Settings">
      <div className="card max-w-xl">
        <form onSubmit={(e) => { e.preventDefault(); settings.adminKey = key; settings.baseUrl = base; setSaved(true); }}>
          <Field label="Control API base URL" hint="Leave empty when the portal is served by the admin role or proxied by Vite.">
            {(id) => <input id={id} className="input" value={base} onChange={(e) => setBase(e.target.value)} placeholder="http://localhost:8081" />}
          </Field>
          <Field label="Admin key" hint="AIGW_ADMIN_KEY of the admin role. Stored in this browser only (alpha); Keycloak SSO replaces it in Phase 2.">
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

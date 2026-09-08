import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { auth } from "../lib/auth";
import { useSession } from "../lib/session";
import { ErrorBanner, Loading, Page } from "../components/ui";

/** OIDC redirect target (/callback): exchanges the code, then returns to where login started. */
export default function Callback() {
  const { config } = useSession();
  const navigate = useNavigate();
  const [error, setError] = useState<unknown>(null);
  useEffect(() => {
    if (!config) return;
    if (!config.oidc) { setError(new Error("Single sign-on is not configured on this control plane")); return; }
    auth.handleCallback(config.oidc).then((to) => navigate(to, { replace: true })).catch(setError);
  }, [config, navigate]);
  return (
    <Page title="Signing in">
      {error ? (
        <div className="card max-w-xl space-y-3">
          <ErrorBanner error={error} />
          <button className="btn btn-secondary" onClick={() => navigate("/settings", { replace: true })}>Back to settings</button>
        </div>
      ) : <Loading />}
    </Page>
  );
}

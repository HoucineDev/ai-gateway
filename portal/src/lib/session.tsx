/** Who is using the portal and what they may do. Wraps GET /auth/config + GET /me (docs/spec/01 §3.1).
 *
 * `can(scope)` drives which actions are rendered; the control API enforces the same scopes, so hiding a button is a
 * courtesy, never the security boundary.
 */
import { createContext, type ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type AuthConfig, type Me, settings, setOidcConfig } from "./api";
import { auth, AUTH_CHANGED } from "./auth";

type Session = {
  config: AuthConfig | undefined;
  me: Me | null | undefined; // undefined = loading, null = not signed in / rejected
  error: unknown;
  signedIn: boolean; // OIDC session present in this tab
  can: (scope: string) => boolean;
  login: (returnTo?: string) => Promise<void>;
  logout: () => Promise<void>;
};

const Ctx = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [signedIn, setSignedIn] = useState(auth.isAuthenticated());
  useEffect(() => {
    const on = () => { setSignedIn(auth.isAuthenticated()); qc.invalidateQueries(); };
    window.addEventListener(AUTH_CHANGED, on);
    return () => window.removeEventListener(AUTH_CHANGED, on);
  }, [qc]);

  const config = useQuery({ queryKey: ["auth-config"], queryFn: api.authConfig, staleTime: Infinity, retry: 1 });
  useEffect(() => { setOidcConfig(config.data?.oidc ?? null); }, [config.data]);

  const hasCredential = signedIn || Boolean(settings.adminKey);
  const me = useQuery({ queryKey: ["me", signedIn, settings.adminKey], queryFn: api.me, enabled: hasCredential, retry: false });

  const can = useCallback((scope: string) => Boolean(me.data?.scopes.includes(scope)), [me.data]);
  const login = useCallback(async (returnTo?: string) => {
    if (!config.data?.oidc) throw new Error("Single sign-on is not configured on this control plane");
    await auth.login(config.data.oidc, returnTo);
  }, [config.data]);
  const logout = useCallback(async () => { await auth.logout(config.data?.oidc ?? null); }, [config.data]);

  const value: Session = {
    config: config.data,
    me: !hasCredential ? null : me.isPending ? undefined : me.data ?? null,
    error: me.error ?? config.error,
    signedIn,
    can,
    login,
    logout,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): Session {
  const s = useContext(Ctx);
  if (!s) throw new Error("useSession outside SessionProvider");
  return s;
}

/** Render children only when the caller holds `scope`. */
export function Can({ scope, children, fallback = null }: { scope: string; children: ReactNode; fallback?: ReactNode }) {
  const { can } = useSession();
  return <>{can(scope) ? children : fallback}</>;
}

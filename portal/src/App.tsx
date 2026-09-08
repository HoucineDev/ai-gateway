import type React from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Activity, Boxes, Building2, KeyRound, LogIn, LogOut, ScrollText, Settings, ListTree, UserRound, Wallet } from "lucide-react";
import Overview from "./pages/Overview";
import Catalog from "./pages/Catalog";
import Tenancy from "./pages/Tenancy";
import Budgets from "./pages/Budgets";
import Requests from "./pages/Requests";
import Audit from "./pages/Audit";
import SettingsPage from "./pages/Settings";
import Callback from "./pages/Callback";
import { settings } from "./lib/api";
import { SessionProvider, useSession } from "./lib/session";

const nav = [
  { to: "/overview", label: "Overview", icon: Activity },
  { to: "/catalog", label: "Models & deployments", icon: Boxes },
  { to: "/tenancy", label: "Organizations & keys", icon: Building2 },
  { to: "/budgets", label: "Budgets", icon: Wallet },
  { to: "/requests", label: "Requests", icon: ListTree },
  { to: "/audit", label: "Audit", icon: ScrollText },
  { to: "/settings", label: "Settings", icon: Settings },
];

/** Sidebar identity block: who is signed in (Keycloak user or admin key) and the sign-in / sign-out action. */
function Identity() {
  const { config, me, signedIn, login, logout } = useSession();
  const oidc = config?.oidc ?? null;
  if (me) {
    return (
      <div className="rounded-md border border-border bg-card p-3 text-sm">
        <div className="flex items-center gap-2">
          {me.actor_type === "user" ? <UserRound size={16} aria-hidden="true" /> : <KeyRound size={16} aria-hidden="true" />}
          <span className="truncate font-medium" title={me.actor_id}>{me.actor_id}</span>
        </div>
        <div className="mt-1 flex flex-wrap gap-1">
          {me.actor_type === "admin_key" && <span className="badge border-border text-muted-foreground bg-muted">admin key</span>}
          {me.roles.map((r) => <span key={r} className="badge border-accent/40 text-accent bg-accent/10">{r}</span>)}
        </div>
        {signedIn && <button className="btn btn-ghost mt-2 w-full justify-start px-1" onClick={() => void logout()}><LogOut size={16} aria-hidden="true" /> Sign out</button>}
      </div>
    );
  }
  if (oidc) return <button className="btn btn-secondary w-full" onClick={() => void login()}><LogIn size={16} aria-hidden="true" /> Sign in with Keycloak</button>;
  return null;
}

/** Without any credential every page would just render the API's 401; show what to do instead. */
function SignInGate({ children }: { children: React.ReactNode }) {
  const { config, signedIn, login } = useSession();
  const { pathname } = useLocation();
  const configured = signedIn || Boolean(settings.adminKey);
  if (configured || !config || pathname === "/settings" || pathname === "/callback") return <>{children}</>;
  return (
    <div className="card mx-auto mt-16 max-w-md text-center">
      <h1 className="text-lg font-semibold">Sign in to continue</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        The control API needs a credential.{" "}
        {config.oidc ? "Use your Keycloak account, or paste the admin key in Settings." : "Paste the admin key in Settings."}
      </p>
      <div className="mt-4 flex justify-center gap-2">
        {config.oidc && <button className="btn btn-primary" onClick={() => void login(pathname)}><LogIn size={16} aria-hidden="true" /> Sign in with Keycloak</button>}
        <Link className="btn btn-secondary" to="/settings">Settings</Link>
      </div>
    </div>
  );
}

export default function App() {
  return <SessionProvider><Shell /></SessionProvider>;
}

function Shell() {
  const { signedIn } = useSession();
  const configured = signedIn || Boolean(settings.adminKey);
  return (
    <div className="flex min-h-full">
      <aside className="hidden w-64 shrink-0 border-r border-border bg-primary/60 p-4 md:flex md:flex-col">
        <div className="mb-6 flex items-center gap-2 px-2">
          <span className="inline-block h-2.5 w-2.5 rounded-full bg-accent" aria-hidden="true" />
          <span className="font-semibold tracking-tight">AI Gateway</span>
          <span className="ml-auto rounded bg-muted px-1.5 py-0.5 text-[10px] uppercase text-muted-foreground">alpha</span>
        </div>
        <nav aria-label="Main">
          {nav.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} className={({ isActive }) =>
              `mb-1 flex min-h-10 items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors duration-150 ${isActive ? "bg-muted text-foreground" : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"}`}>
              <Icon size={18} aria-hidden="true" /> {label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto pt-4"><Identity /></div>
      </aside>
      <main className="min-w-0 flex-1 p-4 md:p-8">
        <nav aria-label="Main (mobile)" className="mb-4 flex gap-1 overflow-x-auto md:hidden">
          {nav.map(({ to, label }) => (
            <NavLink key={to} to={to} className={({ isActive }) => `whitespace-nowrap rounded-md px-3 py-2 text-sm ${isActive ? "bg-muted" : "text-muted-foreground"}`}>{label}</NavLink>
          ))}
        </nav>
        <SignInGate>
        <Routes>
          <Route path="/" element={<Navigate to={configured ? "/overview" : "/settings"} replace />} />
          <Route path="/overview" element={<Overview />} />
          <Route path="/catalog" element={<Catalog />} />
          <Route path="/tenancy" element={<Tenancy />} />
          <Route path="/budgets" element={<Budgets />} />
          <Route path="/requests" element={<Requests />} />
          <Route path="/requests/:id" element={<Requests />} />
          <Route path="/audit" element={<Audit />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/callback" element={<Callback />} />
        </Routes>
        </SignInGate>
      </main>
    </div>
  );
}

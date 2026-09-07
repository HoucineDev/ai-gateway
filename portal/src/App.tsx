import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { Activity, Boxes, Building2, ScrollText, Settings, ListTree, Wallet } from "lucide-react";
import Overview from "./pages/Overview";
import Catalog from "./pages/Catalog";
import Tenancy from "./pages/Tenancy";
import Budgets from "./pages/Budgets";
import Requests from "./pages/Requests";
import Audit from "./pages/Audit";
import SettingsPage from "./pages/Settings";
import { settings } from "./lib/api";

const nav = [
  { to: "/overview", label: "Overview", icon: Activity },
  { to: "/catalog", label: "Models & deployments", icon: Boxes },
  { to: "/tenancy", label: "Organizations & keys", icon: Building2 },
  { to: "/budgets", label: "Budgets", icon: Wallet },
  { to: "/requests", label: "Requests", icon: ListTree },
  { to: "/audit", label: "Audit", icon: ScrollText },
  { to: "/settings", label: "Settings", icon: Settings },
];

export default function App() {
  const configured = Boolean(settings.adminKey);
  return (
    <div className="flex min-h-full">
      <aside className="hidden w-64 shrink-0 border-r border-border bg-primary/60 p-4 md:block">
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
      </aside>
      <main className="min-w-0 flex-1 p-4 md:p-8">
        <nav aria-label="Main (mobile)" className="mb-4 flex gap-1 overflow-x-auto md:hidden">
          {nav.map(({ to, label }) => (
            <NavLink key={to} to={to} className={({ isActive }) => `whitespace-nowrap rounded-md px-3 py-2 text-sm ${isActive ? "bg-muted" : "text-muted-foreground"}`}>{label}</NavLink>
          ))}
        </nav>
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
        </Routes>
      </main>
    </div>
  );
}

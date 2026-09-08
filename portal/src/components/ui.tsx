import { type ReactNode, useEffect, useId, useState } from "react";
import { X } from "lucide-react";
import { ApiError } from "../lib/api";

export function Page({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">{title}</h1>
        <div className="flex gap-2">{actions}</div>
      </div>
      {children}
    </div>
  );
}

export function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: string; tone?: "ok" | "warn" | "bad" }) {
  const color = tone === "ok" ? "text-accent" : tone === "warn" ? "text-warning" : tone === "bad" ? "text-destructive" : "text-foreground";
  return (
    <div className="card">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${color}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
    </div>
  );
}

const statusTone: Record<string, string> = {
  active: "border-accent/40 text-accent bg-accent/10",
  succeeded: "border-accent/40 text-accent bg-accent/10",
  pending: "border-info/40 text-info bg-info/10",
  cached: "border-info/40 text-info bg-info/10",
  revoked: "border-destructive/40 text-destructive bg-destructive/10",
  failed: "border-destructive/40 text-destructive bg-destructive/10",
  ambiguous: "border-warning/40 text-warning bg-warning/10",
  cancelled: "border-warning/40 text-warning bg-warning/10",
  expired: "border-border text-muted-foreground bg-muted",
  disabled: "border-border text-muted-foreground bg-muted",
};

export function Badge({ value }: { value: string }) {
  return <span className={`badge ${statusTone[value] ?? "border-border text-muted-foreground bg-muted"}`}>{value}</span>;
}

export function Modal({ title, open, onClose, children }: { title: string; open: boolean; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div role="dialog" aria-modal="true" aria-label={title} className="card w-full max-w-lg shadow-xl" onClick={(e) => e.stopPropagation()}>
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold">{title}</h2>
          <button className="btn btn-ghost" aria-label="Close dialog" onClick={onClose}><X size={18} /></button>
        </div>
        {children}
      </div>
    </div>
  );
}

export function Field({ label, hint, error, children }: { label: string; hint?: string; error?: string; children: (id: string, describedBy?: string) => ReactNode }) {
  const id = useId();
  const errId = error ? `${id}-error` : undefined;
  return (
    <div className="mb-3">
      <label htmlFor={id} className="label">{label}</label>
      {children(id, errId)}
      {hint && !error && <p className="mt-1 text-xs text-muted-foreground">{hint}</p>}
      {error && <p id={errId} className="mt-1 text-xs text-destructive" role="alert">{error}</p>}
    </div>
  );
}

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof ApiError ? `${error.message} (${error.code})` : error instanceof Error ? error.message : String(error);
  return <div role="alert" className="rounded-md border border-destructive/50 bg-destructive/10 px-3 py-2 text-sm text-destructive">{msg}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">{children}</div>;
}

export function Loading() {
  return <div className="text-sm text-muted-foreground" aria-live="polite">Loading…</div>;
}

export function CopyOnce({ secret }: { secret: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="rounded-md border border-accent/40 bg-accent/10 p-3">
      <p className="mb-2 text-sm text-foreground">Copy this key now — it is shown only once.</p>
      <div className="flex items-center gap-2">
        <code className="mono flex-1 break-all rounded bg-background px-2 py-1">{secret}</code>
        <button className="btn btn-secondary" onClick={() => { navigator.clipboard.writeText(secret); setCopied(true); }}>{copied ? "Copied" : "Copy"}</button>
      </div>
    </div>
  );
}

export function TableWrap({ children }: { children: ReactNode }) {
  return <div className="card overflow-x-auto p-0"><table className="table">{children}</table></div>;
}

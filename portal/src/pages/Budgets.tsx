import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { api, fmtDate, fmtMoney, short } from "../lib/api";
import { useSession } from "../lib/session";
import { Badge, Empty, ErrorBanner, Field, Loading, Modal, Page, TableWrap } from "../components/ui";

export default function Budgets() {
  const { can } = useSession();
  const qc = useQueryClient();
  const budgets = useQuery({ queryKey: ["budgets"], queryFn: () => api.budgets({}), refetchInterval: 10_000 });
  const [open, setOpen] = useState(false);
  const [inc, setInc] = useState<string | null>(null);
  const [form, setForm] = useState({ scope_type: "project", scope_id: "", limit_amount: "100", period: "monthly", soft_alert_pct: "80" });
  const [incForm, setIncForm] = useState({ amount: "50", until: "" });
  const create = useMutation({
    mutationFn: () => api.createBudget({ ...form, soft_alert_pct: form.soft_alert_pct ? Number(form.soft_alert_pct) : undefined }),
    onSuccess: () => { setOpen(false); qc.invalidateQueries({ queryKey: ["budgets"] }); },
  });
  const increase = useMutation({
    mutationFn: () => api.tempIncrease(inc!, { amount: incForm.amount, until: new Date(incForm.until).toISOString() }),
    onSuccess: () => { setInc(null); qc.invalidateQueries({ queryKey: ["budgets"] }); },
  });
  const toggle = useMutation({ mutationFn: ({ id, status }: { id: string; status: string }) => api.patchBudget(id, { status }), onSuccess: () => qc.invalidateQueries({ queryKey: ["budgets"] }) });

  return (
    <Page title="Budgets" actions={can("budgets:write") && <button className="btn btn-primary" onClick={() => setOpen(true)}><Plus size={16} aria-hidden="true" /> New budget</button>}>
      <p className="text-sm text-muted-foreground">Limits are enforced by transactional reservation before every upstream call: spend + reservations never exceed the limit, even under concurrency. Scope order: key → project → team → organization.</p>
      <ErrorBanner error={budgets.error ?? toggle.error} />
      {budgets.isPending && <Loading />}
      {budgets.data?.data.length === 0 && <Empty>No budgets. Without a budget, spend is recorded but unlimited.</Empty>}
      {budgets.data && budgets.data.data.length > 0 && (
        <TableWrap>
          <thead><tr><th>Scope</th><th>Period</th><th className="text-right">Limit</th><th className="text-right">Spent</th><th className="text-right">Reserved</th><th className="text-right">Available</th><th className="w-40">Usage</th><th>Alert</th><th>Temp. increase</th><th>Status</th><th><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>{budgets.data.data.map((b) => {
            const limit = Number(b.limit_amount) + (b.temporary_until && new Date(b.temporary_until) > new Date() ? Number(b.temporary_increase) : 0);
            const pct = limit > 0 ? Math.min(100, (Number(b.spent_amount) / limit) * 100) : 0;
            const tone = pct >= 100 ? "bg-destructive" : pct >= (b.soft_alert_pct ?? 80) ? "bg-warning" : "bg-accent";
            return (
              <tr key={b.id}>
                <td><span className="text-xs uppercase text-muted-foreground">{b.scope_type}</span> <span className="mono">{short(b.scope_id)}</span></td>
                <td>{b.period}{b.period !== "total" && <div className="text-xs text-muted-foreground">from {fmtDate((b as unknown as { period_start: string }).period_start)}</div>}</td>
                <td className="text-right tabular-nums">{fmtMoney(b.limit_amount, 2)}</td>
                <td className="text-right tabular-nums">{fmtMoney(b.spent_amount)}</td>
                <td className="text-right tabular-nums text-muted-foreground">{fmtMoney(b.reserved_amount)}</td>
                <td className="text-right tabular-nums font-medium">{fmtMoney(b.available_amount)}</td>
                <td><div className="h-2 rounded bg-muted" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100} aria-label="Budget used"><div className={`h-2 rounded ${tone}`} style={{ width: `${pct}%` }} /></div><div className="mt-1 text-xs text-muted-foreground">{pct.toFixed(0)}%</div></td>
                <td className="text-xs text-muted-foreground">{b.soft_alert_pct ? `${b.soft_alert_pct}%` : "—"}</td>
                <td className="text-xs text-muted-foreground">{b.temporary_until && new Date(b.temporary_until) > new Date() ? `+${fmtMoney(b.temporary_increase, 2)} until ${fmtDate(b.temporary_until)}` : "—"}</td>
                <td><Badge value={b.status} /></td>
                <td className="whitespace-nowrap text-right">
                  {can("budgets:write") && <>
                  <button className="btn btn-ghost" onClick={() => setInc(b.id)}>Increase</button>
                  <button className="btn btn-ghost" onClick={() => toggle.mutate({ id: b.id, status: b.status === "active" ? "disabled" : "active" })}>{b.status === "active" ? "Disable" : "Enable"}</button>
                  </>}
                </td>
              </tr>
            );
          })}</tbody>
        </TableWrap>
      )}

      <Modal title="New budget" open={open} onClose={() => setOpen(false)}>
        <form onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
          <ErrorBanner error={create.error} />
          <div className="grid grid-cols-2 gap-3">
            <Field label="Scope type">{(id) => <select id={id} className="input" value={form.scope_type} onChange={(e) => setForm({ ...form, scope_type: e.target.value })}>
              <option value="organization">organization</option><option value="team">team</option><option value="project">project</option><option value="key">key</option></select>}</Field>
            <Field label="Scope id" hint="UUID from the tenancy page">{(id) => <input id={id} className="input mono" required value={form.scope_id} onChange={(e) => setForm({ ...form, scope_id: e.target.value })} />}</Field>
            <Field label="Limit">{(id) => <input id={id} className="input" type="number" min={0} step="0.01" value={form.limit_amount} onChange={(e) => setForm({ ...form, limit_amount: e.target.value })} />}</Field>
            <Field label="Period">{(id) => <select id={id} className="input" value={form.period} onChange={(e) => setForm({ ...form, period: e.target.value })}><option value="total">total</option><option value="daily">daily</option><option value="monthly">monthly</option></select>}</Field>
            <Field label="Soft alert at %">{(id) => <input id={id} className="input" type="number" min={1} max={100} value={form.soft_alert_pct} onChange={(e) => setForm({ ...form, soft_alert_pct: e.target.value })} />}</Field>
          </div>
          <div className="flex justify-end gap-2"><button type="button" className="btn btn-secondary" onClick={() => setOpen(false)}>Cancel</button><button className="btn btn-primary" disabled={create.isPending}>Create</button></div>
        </form>
      </Modal>
      <Modal title="Temporary increase" open={inc !== null} onClose={() => setInc(null)}>
        <form onSubmit={(e) => { e.preventDefault(); increase.mutate(); }}>
          <ErrorBanner error={increase.error} />
          <Field label="Additional amount">{(id) => <input id={id} className="input" type="number" min={0} step="0.01" value={incForm.amount} onChange={(e) => setIncForm({ ...incForm, amount: e.target.value })} />}</Field>
          <Field label="Until">{(id) => <input id={id} className="input" type="datetime-local" required value={incForm.until} onChange={(e) => setIncForm({ ...incForm, until: e.target.value })} />}</Field>
          <div className="flex justify-end gap-2"><button type="button" className="btn btn-secondary" onClick={() => setInc(null)}>Cancel</button><button className="btn btn-primary" disabled={increase.isPending}>Apply</button></div>
        </form>
      </Modal>
    </Page>
  );
}

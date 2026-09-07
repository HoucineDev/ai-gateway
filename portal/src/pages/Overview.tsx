import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, fmtInt, fmtMoney } from "../lib/api";
import { Badge, ErrorBanner, Loading, Page, Stat, TableWrap } from "../components/ui";

export default function Overview() {
  const [hours, setHours] = useState(24);
  const q = useQuery({ queryKey: ["overview", hours], queryFn: () => api.overview(hours), refetchInterval: 15_000 });
  const d = q.data;
  const failRate = d && d.requests ? (d.failed / d.requests) * 100 : 0;
  return (
    <Page title="Overview" actions={
      <label className="flex items-center gap-2 text-sm text-muted-foreground">Window
        <select className="input w-auto" value={hours} onChange={(e) => setHours(Number(e.target.value))} aria-label="Time window">
          <option value={1}>1 h</option><option value={24}>24 h</option><option value={168}>7 d</option><option value={720}>30 d</option>
        </select>
      </label>}>
      <ErrorBanner error={q.error} />
      {q.isPending && <Loading />}
      {d && (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
            <Stat label="Requests" value={fmtInt(d.requests)} />
            <Stat label="Spend" value={fmtMoney(d.cost, 2)} hint="settled, ledger currency" />
            <Stat label="Tokens" value={fmtInt(d.tokens)} />
            <Stat label="p95 latency" value={d.p95_latency_ms === null ? "—" : `${Math.round(d.p95_latency_ms)} ms`} />
            <Stat label="Failure rate" value={`${failRate.toFixed(1)}%`} tone={failRate > 5 ? "bad" : failRate > 1 ? "warn" : "ok"} hint={`${d.failed} non-succeeded`} />
          </div>
          <div className="grid gap-4 lg:grid-cols-3">
            <div className="lg:col-span-2">
              <TableWrap>
                <thead><tr><th>Model</th><th className="text-right">Requests</th><th className="text-right">Spend</th><th className="w-1/3">Share</th></tr></thead>
                <tbody>
                  {d.by_model.length === 0 && <tr><td colSpan={4} className="text-center text-muted-foreground">No traffic in this window</td></tr>}
                  {d.by_model.map((m) => (
                    <tr key={m.model}>
                      <td className="mono">{m.model}</td>
                      <td className="text-right tabular-nums">{fmtInt(m.requests)}</td>
                      <td className="text-right tabular-nums">{fmtMoney(m.cost)}</td>
                      <td><div className="h-2 rounded bg-muted" role="img" aria-label={`${((m.requests / d.requests) * 100).toFixed(0)} percent of requests`}>
                        <div className="h-2 rounded bg-accent" style={{ width: `${(m.requests / d.requests) * 100}%` }} /></div></td>
                    </tr>
                  ))}
                </tbody>
              </TableWrap>
            </div>
            <div className="card">
              <h2 className="mb-3 text-sm font-medium text-muted-foreground">By outcome</h2>
              <ul className="space-y-2">
                {Object.entries(d.by_status).map(([s, n]) => (
                  <li key={s} className="flex items-center justify-between text-sm"><Badge value={s} /><span className="tabular-nums">{fmtInt(n)}</span></li>
                ))}
                {Object.keys(d.by_status).length === 0 && <li className="text-sm text-muted-foreground">—</li>}
              </ul>
            </div>
          </div>
        </>
      )}
    </Page>
  );
}

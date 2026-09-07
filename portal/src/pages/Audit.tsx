import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, fmtDate, short } from "../lib/api";
import { Empty, ErrorBanner, Loading, Page, TableWrap } from "../components/ui";

export default function Audit() {
  const [targetType, setTargetType] = useState("");
  const q = useQuery({ queryKey: ["audit", targetType], queryFn: () => api.audit({ target_type: targetType || undefined, limit: "200" }) });
  return (
    <Page title="Audit log" actions={
      <label className="flex items-center gap-2 text-sm text-muted-foreground">Target
        <select className="input w-auto" value={targetType} onChange={(e) => setTargetType(e.target.value)} aria-label="Filter by target type">
          <option value="">all</option>{["organization", "team", "project", "key", "model", "deployment", "price", "budget"].map((t) => <option key={t} value={t}>{t}</option>)}
        </select></label>}>
      <p className="text-sm text-muted-foreground">Append-only: the database rejects updates and deletes on this table.</p>
      <ErrorBanner error={q.error} />
      {q.isPending && <Loading />}
      {q.data?.data.length === 0 && <Empty>No audit events</Empty>}
      {q.data && q.data.data.length > 0 && (
        <TableWrap>
          <thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Target</th><th>Change</th></tr></thead>
          <tbody>{q.data.data.map((a) => (
            <tr key={a.id}>
              <td className="whitespace-nowrap text-xs text-muted-foreground">{fmtDate(a.created_at)}</td>
              <td className="text-xs">{a.actor_type}<div className="text-muted-foreground">{a.actor_id}</div></td>
              <td className="mono">{a.action}</td>
              <td>{a.target_type} <span className="mono text-muted-foreground">{a.target_id ? short(a.target_id) : ""}</span></td>
              <td><details><summary className="cursor-pointer text-xs text-muted-foreground">diff</summary>
                <pre className="mono mt-1 max-h-64 max-w-xl overflow-auto rounded bg-background p-2 text-xs">{JSON.stringify({ before: a.before, after: a.after }, null, 2)}</pre></details></td>
            </tr>
          ))}</tbody>
        </TableWrap>
      )}
    </Page>
  );
}

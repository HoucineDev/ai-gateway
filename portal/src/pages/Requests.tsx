import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { api, fmtDate, fmtInt, fmtMoney, short } from "../lib/api";
import { Badge, Empty, ErrorBanner, Loading, Page, TableWrap } from "../components/ui";

/** Developer diagnostics: recent requests and, per request, every attempt with the routing decision. */
export default function Requests() {
  const { id } = useParams();
  return id ? <RequestDetail id={id} /> : <RequestList />;
}

function RequestList() {
  const q = useQuery({ queryKey: ["requests"], queryFn: () => api.requests({ limit: "100" }), refetchInterval: 10_000 });
  return (
    <Page title="Requests">
      <ErrorBanner error={q.error} />
      {q.isPending && <Loading />}
      {q.data?.data.length === 0 && <Empty>No requests recorded yet</Empty>}
      {q.data && q.data.data.length > 0 && (
        <TableWrap>
          <thead><tr><th>Time</th><th>Request</th><th>Model</th><th>Provider</th><th>Endpoint</th><th>Status</th><th className="text-right">Tokens in/out</th><th className="text-right">Cost</th><th className="text-right">Latency</th><th>Tags</th></tr></thead>
          <tbody>{q.data.data.map((e) => (
            <tr key={e.id}>
              <td className="whitespace-nowrap text-xs text-muted-foreground">{fmtDate(e.created_at)}</td>
              <td><Link className="mono text-info underline-offset-2 hover:underline" to={`/requests/${e.request_id}`}>{short(e.request_id)}</Link></td>
              <td className="mono">{e.model_name}</td><td>{e.provider}</td><td>{e.endpoint}{e.stream ? " · stream" : ""}</td>
              <td><Badge value={e.status} />{e.usage_source === "estimated" && <span className="ml-1 text-xs text-warning" title="Usage estimated by the gateway">est.</span>}</td>
              <td className="text-right tabular-nums">{fmtInt(e.prompt_tokens)} / {fmtInt(e.completion_tokens)}</td>
              <td className="text-right tabular-nums">{fmtMoney(e.cost, 6)}</td>
              <td className="text-right tabular-nums">{e.latency_ms ?? "—"} ms{e.ttfb_ms !== null && <span className="text-xs text-muted-foreground"> · ttfb {e.ttfb_ms}</span>}</td>
              <td className="text-xs text-muted-foreground">{Object.entries(e.tags).map(([k, v]) => `${k}=${v}`).join(" ")}</td>
            </tr>
          ))}</tbody>
        </TableWrap>
      )}
    </Page>
  );
}

function RequestDetail({ id }: { id: string }) {
  const q = useQuery({ queryKey: ["request", id], queryFn: () => api.request(id) });
  return (
    <Page title={`Request ${short(id)}`} actions={<Link className="btn btn-secondary" to="/requests"><ArrowLeft size={16} aria-hidden="true" /> All requests</Link>}>
      <ErrorBanner error={q.error} />
      {q.isPending && <Loading />}
      {q.data && (
        <>
          <p className="mono text-xs text-muted-foreground">{id}</p>
          {q.data.attempts.map((a) => (
            <section key={a.id} className="card" aria-label={`Attempt ${a.attempt_no}`}>
              <div className="mb-3 flex flex-wrap items-center gap-3">
                <h2 className="text-base font-semibold">Attempt {a.attempt_no}</h2>
                <Badge value={a.status} />
                <span className="text-sm text-muted-foreground">{a.provider} · <span className="mono">{a.provider_model}</span></span>
                {a.error_class && <span className="badge border-destructive/40 bg-destructive/10 text-destructive">{a.error_class}</span>}
              </div>
              <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm md:grid-cols-4">
                <Item k="Chosen deployment" v={a.routing.chosen ?? "—"} />
                <Item k="Reserved" v={fmtMoney(a.reserved_amount, 6)} />
                <Item k="Settled" v={fmtMoney(a.settled_amount, 6)} />
                <Item k="Usage source" v={a.usage_source ?? "—"} />
                <Item k="Tokens in / out" v={`${fmtInt(a.prompt_tokens)} / ${fmtInt(a.completion_tokens)}`} />
                <Item k="Upstream id" v={a.upstream_request_id ?? "—"} mono />
                <Item k="Started" v={fmtDate(a.started_at)} />
                <Item k="First byte" v={fmtDate(a.first_byte_at)} />
              </dl>
              {a.error_message && <p className="mt-3 rounded bg-destructive/10 p-2 text-sm text-destructive">{a.error_message}</p>}
              <details className="mt-3">
                <summary className="cursor-pointer text-sm text-muted-foreground">Routing decision</summary>
                <div className="mt-2 grid gap-3 text-sm md:grid-cols-2">
                  <div><div className="label">Candidates (in order)</div><ol className="list-inside list-decimal">{(a.routing.candidates ?? []).map((c) => <li key={c} className={c === a.routing.chosen ? "text-accent" : ""}>{c}</li>)}</ol></div>
                  <div><div className="label">Rejected</div>{Object.keys(a.routing.rejected ?? {}).length === 0 ? <span className="text-muted-foreground">none</span> :
                    <ul>{Object.entries(a.routing.rejected ?? {}).map(([d, r]) => <li key={d}><span className="font-medium">{d}</span> <span className="text-muted-foreground">— {r}</span></li>)}</ul>}</div>
                </div>
              </details>
            </section>
          ))}
        </>
      )}
    </Page>
  );
}

function Item({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return <div><dt className="text-xs uppercase tracking-wide text-muted-foreground">{k}</dt><dd className={mono ? "mono break-all" : ""}>{v}</dd></div>;
}

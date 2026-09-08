import { useState, type ChangeEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Snowflake, Power } from "lucide-react";
import { ApiError, api, type Model, type Deployment } from "../lib/api";
import { useSession } from "../lib/session";
import { Badge, Empty, ErrorBanner, Field, Loading, Modal, Page, TableWrap } from "../components/ui";

export default function Catalog() {
  const { can, canGlobal } = useSession();
  // global models (org_id null) and the price registry are global-only; org-scoped models follow delegated grants
  const canWriteModel = (m: { org_id: string | null }) => (m.org_id ? can("deployments:write") : canGlobal("deployments:write"));
  const qc = useQueryClient();
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  const prices = useQuery({ queryKey: ["prices"], queryFn: api.prices });
  const [newModel, setNewModel] = useState(false);
  const [newDep, setNewDep] = useState<Model | null>(null);
  const [newPrice, setNewPrice] = useState(false);
  const invalidate = () => qc.invalidateQueries({ queryKey: ["models"] });

  const cooldown = useMutation({ mutationFn: ({ id, seconds }: { id: string; seconds: number }) => api.cooldown(id, seconds), onSuccess: invalidate });
  const toggle = useMutation({ mutationFn: ({ id, status }: { id: string; status: string }) => api.patchDeployment(id, { status }), onSuccess: invalidate });

  return (
    <Page title="Models & deployments" actions={<>
      {canGlobal("prices:write") && <button className="btn btn-secondary" onClick={() => setNewPrice(true)}>Add price</button>}
      {canGlobal("models:write") && <button className="btn btn-primary" onClick={() => setNewModel(true)}><Plus size={16} aria-hidden="true" /> Register model</button>}
    </>}>
      <ErrorBanner error={models.error ?? cooldown.error ?? toggle.error} />
      {models.isPending && <Loading />}
      {models.data?.data.length === 0 && <Empty>No models yet. Register a logical model, then add a deployment pointing at vLLM/KServe or a hosted provider.</Empty>}
      {models.data?.data.map((m) => (
        <section key={m.id} className="card" aria-labelledby={`m-${m.id}`}>
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <h2 id={`m-${m.id}`} className="mono text-base font-semibold">{m.name}</h2>
            {m.display_name && <span className="text-sm text-muted-foreground">{m.display_name}</span>}
            <Badge value={m.status} />
            <span className="text-xs text-muted-foreground">{m.org_id ? "org-scoped" : "global"} · {m.modalities.join(", ")}
              {m.context_window ? ` · ${m.context_window.toLocaleString()} ctx` : ""}{m.supports_tools ? " · tools" : ""}{m.supports_json_schema ? " · json_schema" : ""}{m.supports_vision ? " · vision" : ""}</span>
            {canWriteModel(m) && <button className="btn btn-secondary ml-auto" onClick={() => setNewDep(m)}><Plus size={16} aria-hidden="true" /> Deployment</button>}
          </div>
          {m.deployments.length === 0 ? <p className="text-sm text-muted-foreground">No deployments — requests to this model will get 503.</p> : (
            <div className="overflow-x-auto">
              <table className="table">
                <thead><tr><th>Name</th><th>Provider</th><th>Provider model</th><th>Base URL</th><th>Prio</th><th>Weight</th><th>Status</th><th>Health</th><th>Cooldown</th><th><span className="sr-only">Actions</span></th></tr></thead>
                <tbody>
                  {m.deployments.map((d) => {
                    const cooling = d.cooldown_until && new Date(d.cooldown_until) > new Date();
                    return (
                      <tr key={d.id}>
                        <td className="font-medium">{d.name}</td>
                        <td><span className="badge border-border text-muted-foreground">{d.provider}</span></td>
                        <td className="mono">{d.provider_model}</td>
                        <td className="mono max-w-[16rem] truncate text-muted-foreground" title={d.base_url ?? ""}>{d.base_url ?? "default"}</td>
                        <td className="tabular-nums">{d.priority}</td>
                        <td className="tabular-nums">{d.weight}</td>
                        <td><Badge value={d.status} /></td>
                        <td><Health h={d.health} /></td>
                        <td className="text-xs text-muted-foreground">{cooling ? `until ${new Date(d.cooldown_until!).toLocaleTimeString()}` : "—"}</td>
                        <td className="whitespace-nowrap text-right">
                          {canWriteModel(m) && <>
                          <button className="btn btn-ghost" aria-label={cooling ? `Clear cooldown for ${d.name}` : `Cool down ${d.name} for 10 minutes`}
                            onClick={() => cooldown.mutate({ id: d.id, seconds: cooling ? 0 : 600 })}><Snowflake size={16} /></button>
                          <button className="btn btn-ghost" aria-label={`${d.status === "active" ? "Disable" : "Enable"} ${d.name}`}
                            onClick={() => toggle.mutate({ id: d.id, status: d.status === "active" ? "disabled" : "active" })}><Power size={16} /></button>
                          </>}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      ))}

      <section className="card">
        <h2 className="mb-3 text-base font-semibold">Price registry</h2>
        <p className="mb-3 text-sm text-muted-foreground">Owned, versioned prices per provider model. Newest effective version is used for reservations; each ledger row records the version applied.</p>
        {prices.data && prices.data.data.length === 0 && <p className="text-sm text-warning">No prices — usage will be accounted at zero cost until you add one.</p>}
        {prices.data && prices.data.data.length > 0 && (
          <div className="overflow-x-auto"><table className="table">
            <thead><tr><th>Provider</th><th>Model</th><th>v</th><th className="text-right">Input / M</th><th className="text-right">Output / M</th><th>Effective</th><th>Source</th></tr></thead>
            <tbody>{prices.data.data.map((p) => (
              <tr key={p.id}><td>{p.provider}</td><td className="mono">{p.provider_model}</td><td className="tabular-nums">{p.version}</td>
                <td className="text-right tabular-nums">{Number(p.input_per_million).toFixed(4)}</td><td className="text-right tabular-nums">{Number(p.output_per_million).toFixed(4)}</td>
                <td className="text-xs text-muted-foreground">{new Date(p.effective_from).toLocaleDateString()}</td><td className="text-xs text-muted-foreground">{p.source ?? "—"}</td></tr>
            ))}</tbody>
          </table></div>
        )}
      </section>

      <ModelForm open={newModel} onClose={() => { setNewModel(false); invalidate(); }} />
      <DeploymentForm model={newDep} onClose={() => { setNewDep(null); invalidate(); }} />
      <PriceForm open={newPrice} onClose={() => { setNewPrice(false); qc.invalidateQueries({ queryKey: ["prices"] }); }} />
    </Page>
  );
}

function useForm<T extends Record<string, unknown>>(initial: T) {
  const [v, setV] = useState<T>(initial);
  const set = (k: keyof T) => (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setV((s) => ({ ...s, [k]: e.target.type === "checkbox" ? (e.target as HTMLInputElement).checked : e.target.value }));
  return { v, set, reset: () => setV(initial) };
}

function fieldError(err: unknown, param: string) {
  return err instanceof ApiError && err.param === param ? err.message : undefined;
}

function ModelForm({ open, onClose }: { open: boolean; onClose: () => void }) {
  const f = useForm({ name: "", display_name: "", modalities: "chat", context_window: "", supports_tools: true, supports_json_schema: false, supports_vision: false });
  const m = useMutation({
    mutationFn: () => api.createModel({
      name: f.v.name, display_name: f.v.display_name || null, modalities: [f.v.modalities],
      context_window: f.v.context_window ? Number(f.v.context_window) : null,
      supports_tools: f.v.supports_tools, supports_json_schema: f.v.supports_json_schema, supports_vision: f.v.supports_vision,
    }),
    onSuccess: () => { f.reset(); onClose(); },
  });
  return (
    <Modal title="Register logical model" open={open} onClose={onClose}>
      <form onSubmit={(e) => { e.preventDefault(); m.mutate(); }}>
        <ErrorBanner error={m.error && !(m.error instanceof ApiError && m.error.param) ? m.error : null} />
        <Field label="Name (what clients send as model)" error={fieldError(m.error, "name")}>
          {(id, d) => <input id={id} aria-describedby={d} className="input mono" required value={f.v.name} onChange={f.set("name")} placeholder="qwen2.5-32b" />}
        </Field>
        <Field label="Display name">{(id) => <input id={id} className="input" value={f.v.display_name} onChange={f.set("display_name")} />}</Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Modality">{(id) => <select id={id} className="input" value={f.v.modalities} onChange={f.set("modalities")}><option value="chat">chat</option><option value="embedding">embedding</option></select>}</Field>
          <Field label="Context window">{(id) => <input id={id} className="input" type="number" min={1} value={f.v.context_window} onChange={f.set("context_window")} />}</Field>
        </div>
        <fieldset className="mb-4 flex flex-wrap gap-4 text-sm"><legend className="label">Capabilities</legend>
          <label className="flex items-center gap-2"><input type="checkbox" checked={f.v.supports_tools} onChange={f.set("supports_tools")} /> tools</label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={f.v.supports_json_schema} onChange={f.set("supports_json_schema")} /> json_schema</label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={f.v.supports_vision} onChange={f.set("supports_vision")} /> vision</label>
        </fieldset>
        <div className="flex justify-end gap-2"><button type="button" className="btn btn-secondary" onClick={onClose}>Cancel</button><button className="btn btn-primary" disabled={m.isPending}>Create</button></div>
      </form>
    </Modal>
  );
}

function DeploymentForm({ model, onClose }: { model: Model | null; onClose: () => void }) {
  const f = useForm({ name: "", provider: "openai_compat", provider_model: "", base_url: "", credential_ref: "none", weight: "1", priority: "0", region: "" });
  const m = useMutation({
    mutationFn: () => api.createDeployment(model!.id, {
      name: f.v.name, provider: f.v.provider, provider_model: f.v.provider_model, base_url: f.v.base_url || null,
      credential_ref: f.v.credential_ref || "none", weight: Number(f.v.weight), priority: Number(f.v.priority), region: f.v.region || null,
    }),
    onSuccess: () => { f.reset(); onClose(); },
  });
  return (
    <Modal title={`Add deployment to ${model?.name ?? ""}`} open={Boolean(model)} onClose={onClose}>
      <form onSubmit={(e) => { e.preventDefault(); m.mutate(); }}>
        <ErrorBanner error={m.error} />
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name">{(id) => <input id={id} className="input" required value={f.v.name} onChange={f.set("name")} placeholder="vllm-a100-1" />}</Field>
          <Field label="Provider">{(id) => <select id={id} className="input" value={f.v.provider} onChange={f.set("provider")}>
            <option value="openai_compat">openai_compat (vLLM, KServe, TGI…)</option><option value="openai">openai</option><option value="anthropic">anthropic</option></select>}</Field>
        </div>
        <Field label="Provider model">{(id) => <input id={id} className="input mono" required value={f.v.provider_model} onChange={f.set("provider_model")} placeholder="Qwen/Qwen2.5-32B-Instruct" />}</Field>
        <Field label="Base URL" hint="OpenAI-compatible: include /v1. Empty = provider default.">{(id) => <input id={id} className="input mono" value={f.v.base_url} onChange={f.set("base_url")} placeholder="http://vllm.inference.svc:8000/v1" />}</Field>
        <Field label="Credential reference" hint="none · env:VAR_NAME · openbao:mount/path#key (Phase 2)">{(id) => <input id={id} className="input mono" value={f.v.credential_ref} onChange={f.set("credential_ref")} />}</Field>
        <div className="grid grid-cols-3 gap-3">
          <Field label="Priority" hint="lower wins">{(id) => <input id={id} className="input" type="number" value={f.v.priority} onChange={f.set("priority")} />}</Field>
          <Field label="Weight">{(id) => <input id={id} className="input" type="number" min={0} value={f.v.weight} onChange={f.set("weight")} />}</Field>
          <Field label="Region">{(id) => <input id={id} className="input" value={f.v.region} onChange={f.set("region")} placeholder="eu-fr" />}</Field>
        </div>
        <div className="flex justify-end gap-2"><button type="button" className="btn btn-secondary" onClick={onClose}>Cancel</button><button className="btn btn-primary" disabled={m.isPending}>Add</button></div>
      </form>
    </Modal>
  );
}

function PriceForm({ open, onClose }: { open: boolean; onClose: () => void }) {
  const f = useForm({ provider: "openai_compat", provider_model: "", input_per_million: "0", output_per_million: "0", source: "internal-allocation" });
  const m = useMutation({ mutationFn: () => api.createPrice(f.v), onSuccess: () => { f.reset(); onClose(); } });
  return (
    <Modal title="Add price version" open={open} onClose={onClose}>
      <form onSubmit={(e) => { e.preventDefault(); m.mutate(); }}>
        <ErrorBanner error={m.error} />
        <div className="grid grid-cols-2 gap-3">
          <Field label="Provider">{(id) => <select id={id} className="input" value={f.v.provider} onChange={f.set("provider")}><option value="openai_compat">openai_compat</option><option value="openai">openai</option><option value="anthropic">anthropic</option></select>}</Field>
          <Field label="Provider model">{(id) => <input id={id} className="input mono" required value={f.v.provider_model} onChange={f.set("provider_model")} />}</Field>
          <Field label="Input per 1M tokens">{(id) => <input id={id} className="input" type="number" step="0.0001" min={0} value={f.v.input_per_million} onChange={f.set("input_per_million")} />}</Field>
          <Field label="Output per 1M tokens">{(id) => <input id={id} className="input" type="number" step="0.0001" min={0} value={f.v.output_per_million} onChange={f.set("output_per_million")} />}</Field>
        </div>
        <Field label="Source" hint="Provider price page URL, or internal-allocation for local GPU cost">{(id) => <input id={id} className="input" value={f.v.source} onChange={f.set("source")} />}</Field>
        <div className="flex justify-end gap-2"><button type="button" className="btn btn-secondary" onClick={onClose}>Cancel</button><button className="btn btn-primary" disabled={m.isPending}>Add</button></div>
      </form>
    </Modal>
  );
}

/** Active health probe result (worker, docs/spec/04 §6). Unhealthy deployments are cooled down automatically. */
function Health({ h }: { h: Deployment["health"] }) {
  if (!h || h.status === "unknown") return <span className="text-xs text-muted-foreground" title="Not probed yet (worker not running?)">not probed</span>;
  const tone = h.status === "healthy" ? "bg-accent" : h.status === "degraded" ? "bg-warning" : "bg-destructive";
  const ago = h.checked_at ? `${Math.max(0, Math.round((Date.now() - new Date(h.checked_at).getTime()) / 1000))}s ago` : "";
  const detail = h.status === "healthy" ? `${h.latency_ms ?? "—"} ms` : `${h.error ?? "failing"} ×${h.consecutive_failures}`;
  return (
    <span className="inline-flex items-center gap-2 text-xs" title={`${h.status} · checked ${ago}`}>
      <span className={`inline-block h-2 w-2 rounded-full ${tone}`} aria-hidden="true" />
      <span className={h.status === "healthy" ? "text-muted-foreground" : h.status === "degraded" ? "text-warning" : "text-destructive"}>{h.status} · {detail}</span>
    </span>
  );
}

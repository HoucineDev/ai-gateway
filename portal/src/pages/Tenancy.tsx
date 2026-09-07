import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, KeyRound, RotateCw, Ban } from "lucide-react";
import { api, fmtDate, type Org, type Team, type Project } from "../lib/api";
import { Badge, CopyOnce, Empty, ErrorBanner, Field, Loading, Modal, Page, TableWrap } from "../components/ui";

/** Org → team → project → keys drill-down. Every write is audited server-side. */
export default function Tenancy() {
  const qc = useQueryClient();
  const orgs = useQuery({ queryKey: ["orgs"], queryFn: api.orgs });
  const [org, setOrg] = useState<Org | null>(null);
  const [team, setTeam] = useState<Team | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const teams = useQuery({ queryKey: ["teams", org?.id], queryFn: () => api.teams(org!.id), enabled: Boolean(org) });
  const projects = useQuery({ queryKey: ["projects", team?.id], queryFn: () => api.projects(team!.id), enabled: Boolean(team) });
  const keys = useQuery({ queryKey: ["keys", project?.id], queryFn: () => api.keys(project!.id), enabled: Boolean(project) });

  const [dialog, setDialog] = useState<"org" | "team" | "project" | "key" | null>(null);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [tags, setTags] = useState("env,feature,team");
  const [rpm, setRpm] = useState("");
  const [secret, setSecret] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: async () => {
      if (dialog === "org") return api.createOrg({ name, slug });
      if (dialog === "team") return api.createTeam(org!.id, { name });
      if (dialog === "project") return api.createProject(team!.id, { name, settings: { allowed_tags: tags.split(",").map((t) => t.trim()).filter(Boolean) } });
      return api.createKey(project!.id, { name, rpm_limit: rpm ? Number(rpm) : null });
    },
    onSuccess: (res) => {
      qc.invalidateQueries();
      if (dialog === "key" && "key" in res && res.key) setSecret(res.key);
      setDialog(null); setName(""); setSlug(""); setRpm("");
    },
  });
  const revoke = useMutation({ mutationFn: (id: string) => api.revokeKey(id), onSuccess: () => qc.invalidateQueries({ queryKey: ["keys"] }) });
  const rotate = useMutation({ mutationFn: (id: string) => api.rotateKey(id), onSuccess: (r) => { setSecret(r.key ?? null); qc.invalidateQueries({ queryKey: ["keys"] }); } });

  const col = "card min-w-0";
  return (
    <Page title="Organizations & keys">
      <ErrorBanner error={orgs.error ?? revoke.error ?? rotate.error} />
      {secret && <CopyOnce secret={secret} />}
      <div className="grid gap-4 lg:grid-cols-3">
        <section className={col} aria-label="Organizations">
          <Header title="Organizations" onAdd={() => setDialog("org")} />
          {orgs.isPending && <Loading />}
          {orgs.data?.data.length === 0 && <Empty>No organizations</Empty>}
          <ul>{orgs.data?.data.map((o) => (
            <li key={o.id}><button className={`flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm hover:bg-muted ${org?.id === o.id ? "bg-muted" : ""}`}
              aria-pressed={org?.id === o.id} onClick={() => { setOrg(o); setTeam(null); setProject(null); }}>
              <span>{o.name} <span className="ml-1 text-xs text-muted-foreground">{o.slug}</span></span><Badge value={o.status} /></button></li>
          ))}</ul>
        </section>
        <section className={col} aria-label="Teams">
          <Header title={org ? `Teams · ${org.name}` : "Teams"} onAdd={org ? () => setDialog("team") : undefined} />
          {!org && <Empty>Select an organization</Empty>}
          {org && teams.data?.data.length === 0 && <Empty>No teams</Empty>}
          <ul>{teams.data?.data.map((t) => (
            <li key={t.id}><button className={`flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm hover:bg-muted ${team?.id === t.id ? "bg-muted" : ""}`}
              aria-pressed={team?.id === t.id} onClick={() => { setTeam(t); setProject(null); }}>{t.name}<Badge value={t.status} /></button></li>
          ))}</ul>
        </section>
        <section className={col} aria-label="Projects">
          <Header title={team ? `Projects · ${team.name}` : "Projects"} onAdd={team ? () => setDialog("project") : undefined} />
          {!team && <Empty>Select a team</Empty>}
          {team && projects.data?.data.length === 0 && <Empty>No projects</Empty>}
          <ul>{projects.data?.data.map((p) => (
            <li key={p.id}><button className={`flex w-full items-center justify-between rounded-md px-3 py-2 text-left text-sm hover:bg-muted ${project?.id === p.id ? "bg-muted" : ""}`}
              aria-pressed={project?.id === p.id} onClick={() => setProject(p)}>{p.name}<Badge value={p.status} /></button></li>
          ))}</ul>
        </section>
      </div>

      <section aria-label="Virtual keys">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-base font-semibold">{project ? `Keys · ${project.name}` : "Keys"}</h2>
          {project && <button className="btn btn-primary" onClick={() => setDialog("key")}><KeyRound size={16} aria-hidden="true" /> Issue key</button>}
        </div>
        {!project && <Empty>Select a project to manage its keys and budget</Empty>}
        {project && keys.data && (
          <TableWrap>
            <thead><tr><th>Name</th><th>Prefix</th><th>Status</th><th>RPM</th><th>TPM</th><th>Models</th><th>Created</th><th>Grace until</th><th><span className="sr-only">Actions</span></th></tr></thead>
            <tbody>
              {keys.data.data.length === 0 && <tr><td colSpan={9} className="text-center text-muted-foreground">No keys</td></tr>}
              {keys.data.data.map((k) => (
                <tr key={k.id}>
                  <td className="font-medium">{k.name}</td><td className="mono">{k.key_prefix}…</td><td><Badge value={k.status} /></td>
                  <td className="tabular-nums">{k.rpm_limit ?? "—"}</td><td className="tabular-nums">{k.tpm_limit ?? "—"}</td>
                  <td className="text-xs text-muted-foreground">{k.allowed_models ? k.allowed_models.join(", ") : "all visible"}</td>
                  <td className="text-xs text-muted-foreground">{fmtDate(k.created_at)}</td><td className="text-xs text-muted-foreground">{fmtDate(k.grace_until)}</td>
                  <td className="whitespace-nowrap text-right">
                    {k.status === "active" && <>
                      <button className="btn btn-ghost" aria-label={`Rotate key ${k.name}`} onClick={() => rotate.mutate(k.id)}><RotateCw size={16} /></button>
                      <button className="btn btn-ghost text-destructive" aria-label={`Revoke key ${k.name}`} onClick={() => { if (confirm(`Revoke key "${k.name}"? Clients using it will get 401 within seconds.`)) revoke.mutate(k.id); }}><Ban size={16} /></button>
                    </>}
                  </td>
                </tr>
              ))}
            </tbody>
          </TableWrap>
        )}
      </section>

      <Modal title={{ org: "New organization", team: "New team", project: "New project", key: "Issue virtual key" }[dialog ?? "org"]} open={dialog !== null} onClose={() => setDialog(null)}>
        <form onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
          <ErrorBanner error={create.error} />
          <Field label="Name">{(id) => <input id={id} className="input" required autoFocus value={name} onChange={(e) => setName(e.target.value)} />}</Field>
          {dialog === "org" && <Field label="Slug" hint="lowercase, digits and dashes">{(id) => <input id={id} className="input mono" required pattern="^[a-z0-9][a-z0-9-]{1,60}$" value={slug} onChange={(e) => setSlug(e.target.value)} />}</Field>}
          {dialog === "project" && <Field label="Allowed allocation tags" hint="Comma-separated keys accepted in request.metadata">{(id) => <input id={id} className="input mono" value={tags} onChange={(e) => setTags(e.target.value)} />}</Field>}
          {dialog === "key" && <Field label="RPM limit" hint="Requests per minute; empty = unlimited">{(id) => <input id={id} className="input" type="number" min={1} value={rpm} onChange={(e) => setRpm(e.target.value)} />}</Field>}
          <div className="flex justify-end gap-2"><button type="button" className="btn btn-secondary" onClick={() => setDialog(null)}>Cancel</button><button className="btn btn-primary" disabled={create.isPending}>Create</button></div>
        </form>
      </Modal>
    </Page>
  );
}

function Header({ title, onAdd }: { title: string; onAdd?: () => void }) {
  return (
    <div className="mb-2 flex items-center justify-between">
      <h2 className="text-sm font-semibold">{title}</h2>
      {onAdd && <button className="btn btn-ghost" aria-label={`Add to ${title}`} onClick={onAdd}><Plus size={16} /></button>}
    </div>
  );
}

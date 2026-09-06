import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiFailure,
  createControlPlane,
  type ControlPlane,
  type RegistrySearch,
  type RunPreflight,
} from "./api/operations";
import {
  clearSession,
  isWorkspaceId,
  loadSession,
  saveSession,
  type WorkspaceSession,
} from "./session";

type View = "overview" | "registry" | "capabilities" | "runs";
type ApiFactory = (session: WorkspaceSession) => ControlPlane;
type QueryScope = readonly ["workspace", string, string];

const navItems: { id: View; label: string; marker: string }[] = [
  { id: "overview", label: "Overview", marker: "01" },
  { id: "registry", label: "Registry", marker: "02" },
  { id: "capabilities", label: "Capabilities", marker: "03" },
  { id: "runs", label: "Runs", marker: "04" },
];

function newIdentityId(): string {
  return crypto.randomUUID();
}

function formatTime(value: string | null): string {
  if (value === null) return "Not yet";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(value));
}

function shortId(value: string): string {
  return `${value.slice(0, 8)}…${value.slice(-4)}`;
}

function terminal(status: string): boolean {
  return ["succeeded", "failed", "cancelled", "timed_out", "indeterminate"].includes(status);
}

function mutationId(): string { return crypto.randomUUID(); }
function canOperate(role: WorkspaceSession["role"]): boolean { return role !== "viewer"; }
function isAdmin(role: WorkspaceSession["role"]): boolean { return role === "admin"; }

type Route = { view: View; selectedId: string | null };
function readRoute(): Route {
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts[0] === "connections") return { view: "registry", selectedId: parts[1] ?? null };
  if (parts[0] === "capabilities") return { view: "capabilities", selectedId: parts[1] ?? null };
  if (parts[0] === "runs") return { view: "runs", selectedId: parts[1] ?? null };
  if (parts[0] === "registry") return { view: "registry", selectedId: null };
  return { view: "overview", selectedId: null };
}

function routePath(view: View, selectedId: string | null = null): string {
  if (view === "overview") return "/";
  if (view === "registry") return selectedId ? `/connections/${selectedId}` : "/registry";
  return selectedId ? `/${view}/${selectedId}` : `/${view}`;
}

function failureMessage(error: unknown): string {
  if (error instanceof ApiFailure) {
    const reference = error.correlationId ? ` Reference ${shortId(error.correlationId)}.` : "";
    return `${error.message}${reference}`;
  }
  return "The control plane could not be reached. Check the API and try again.";
}

function formValue(data: FormData, name: string): string {
  const value = data.get(name);
  return typeof value === "string" ? value.trim() : "";
}

function queryKey(scope: QueryScope, ...resource: readonly unknown[]): readonly unknown[] {
  return [...scope, ...resource];
}

function SignIn({ onSignIn }: { onSignIn: (session: WorkspaceSession) => void }) {
  const configuredWorkspace = (import.meta.env as { VITE_WORKSPACE_ID?: string })
    .VITE_WORKSPACE_ID;
  const [workspaceId, setWorkspaceId] = useState(configuredWorkspace ?? "");
  const [workspaceLabel, setWorkspaceLabel] = useState("Pilot workspace");
  const [accessToken, setAccessToken] = useState("");
  const [role, setRole] = useState<WorkspaceSession["role"]>("admin");
  const [error, setError] = useState("");

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!isWorkspaceId(workspaceId.trim())) {
      setError("Enter the workspace UUID created during setup.");
      return;
    }
    const session: WorkspaceSession = {
      identityId: newIdentityId(),
      workspaceId: workspaceId.trim(),
      workspaceLabel: workspaceLabel.trim() || "Workspace",
      role,
      ...(accessToken.trim() ? { accessToken: accessToken.trim() } : {}),
    };
    saveSession(session);
    onSignIn(session);
  }

  return (
    <main className="auth-layout" id="main-content">
      <section className="auth-intro" aria-labelledby="welcome-title">
        <p className="wordmark">modall / registry</p>
        <div>
          <p className="kicker">Private capability control plane</p>
          <h1 id="welcome-title">Know what can run before it runs.</h1>
          <p className="lede">
            Review discovered tools, pin exact versions, and follow every invocation from
            admission to result.
          </p>
        </div>
        <p className="auth-footnote">Alpha · isolated workspaces · payload-safe audit</p>
      </section>
      <section className="auth-panel" aria-labelledby="signin-title">
        <div className="panel-heading">
          <span className="index">Access / 01</span>
          <h2 id="signin-title">Open a workspace</h2>
          <p>Use the identifier from the workspace bootstrap output.</p>
        </div>
        <form onSubmit={submit} noValidate>
          <label>
            Pilot role
            <select value={role} onChange={(event) => setRole(event.target.value as WorkspaceSession["role"])}>
              <option value="admin">Admin</option><option value="operator">Operator</option><option value="viewer">Viewer</option>
            </select>
          </label>
          <label>
            Workspace label
            <input
              value={workspaceLabel}
              onChange={(event) => setWorkspaceLabel(event.target.value)}
              autoComplete="organization"
            />
          </label>
          <label>
            Workspace UUID
            <input
              value={workspaceId}
              onChange={(event) => setWorkspaceId(event.target.value)}
              placeholder="00000000-0000-4000-8000-000000000000"
              aria-describedby={error ? "workspace-error" : "workspace-help"}
              aria-invalid={Boolean(error)}
              autoComplete="off"
            />
          </label>
          {error ? (
            <p className="field-error" id="workspace-error" role="alert">
              {error}
            </p>
          ) : (
            <p className="field-help" id="workspace-help">
              Local mode authenticates the configured developer identity.
            </p>
          )}
          <label>
            OIDC access token <span>optional in local mode</span>
            <input
              value={accessToken}
              onChange={(event) => setAccessToken(event.target.value)}
              type="password"
              autoComplete="off"
            />
          </label>
          <button className="primary-action" type="submit">
            Enter control plane <span aria-hidden="true">↗</span>
          </button>
        </form>
      </section>
    </main>
  );
}

function StatusMark({ value }: { value: string }) {
  return (
    <span className={`status-mark status-${value.replaceAll("_", "-")}`}>
      <span aria-hidden="true" />
      {value.replaceAll("_", " ")}
    </span>
  );
}

function QueryFailure({ error, retry }: { error: unknown; retry: () => void }) {
  return (
    <div className="state-block error-state" role="alert">
      <span className="state-glyph" aria-hidden="true">
        ×
      </span>
      <div>
        <h3>Request failed</h3>
        <p>{failureMessage(error)}</p>
        <button className="text-action" onClick={retry} type="button">
          Try again
        </button>
      </div>
    </div>
  );
}

function LoadingState({ label }: { label: string }) {
  return (
    <div className="loading-state" role="status" aria-label={label}>
      <span />
      <span />
      <span />
    </div>
  );
}

function EmptyState({ title, copy }: { title: string; copy: string }) {
  return (
    <div className="state-block empty-state">
      <span className="state-glyph" aria-hidden="true">
        +
      </span>
      <div>
        <h3>{title}</h3>
        <p>{copy}</p>
      </div>
    </div>
  );
}

function Overview({ api, open, scope }: { api: ControlPlane; open: (view: View) => void; scope: QueryScope }) {
  const query = useQuery({ queryKey: queryKey(scope, "overview"), queryFn: () => api.overview() });
  if (query.isPending) return <LoadingState label="Loading workspace overview" />;
  if (query.isError) return <QueryFailure error={query.error} retry={() => void query.refetch()} />;

  const active = query.data.connections.filter((item) => item.lifecycle === "active").length;
  const enabled = query.data.capabilities.filter((item) => item.status === "enabled").length;
  const running = query.data.runs.filter((item) => !terminal(item.status)).length;
  return (
    <div className="page-flow">
      <header className="page-heading overview-heading">
        <div>
          <p className="kicker">Operational posture</p>
          <h1>Registry overview</h1>
          <p>Current state across server trust, capability approval, and durable execution.</p>
        </div>
        <p className="timestamp">Updated {formatTime(new Date().toISOString())}</p>
      </header>
      <section className="metric-ribbon" aria-label="Workspace metrics">
        <button type="button" onClick={() => open("registry")}>
          <span>Active connections</span>
          <strong>{active.toString().padStart(2, "0")}</strong>
          <small>{query.data.connections.length} total</small>
        </button>
        <button type="button" onClick={() => open("capabilities")}>
          <span>Enabled capabilities</span>
          <strong>{enabled.toString().padStart(2, "0")}</strong>
          <small>{query.data.capabilities.length} discovered</small>
        </button>
        <button type="button" onClick={() => open("runs")}>
          <span>Runs in flight</span>
          <strong>{running.toString().padStart(2, "0")}</strong>
          <small>{query.data.runs.length} retained</small>
        </button>
      </section>
      <section className="overview-grid">
        <div className="section-block">
          <div className="section-heading">
            <div>
              <span className="index">Trust boundary / 02</span>
              <h2>Server status</h2>
            </div>
            <button className="text-action" type="button" onClick={() => open("registry")}>
              Manage registry
            </button>
          </div>
          {query.data.connections.length === 0 ? (
            <EmptyState title="No servers connected" copy="Add a server to begin discovery." />
          ) : (
            <ul className="row-list">
              {query.data.connections.slice(0, 5).map((connection) => (
                <li key={connection.id}>
                  <div>
                    <strong>{connection.name}</strong>
                    <span>Refresh {formatTime(connection.last_refresh_at)}</span>
                  </div>
                  <StatusMark value={connection.lifecycle} />
                </li>
              ))}
            </ul>
          )}
        </div>
        <aside className="attention-panel">
          <span className="index">Review queue / 03</span>
          <strong>
            {query.data.capabilities.filter((item) => item.status === "pending_review").length}
          </strong>
          <h2>Capabilities need a decision</h2>
          <p>Inspect immutable schema and metadata before enabling a version.</p>
          <button className="secondary-action" type="button" onClick={() => open("capabilities")}>
            Open review queue
          </button>
        </aside>
      </section>
    </div>
  );
}

function Registry({ api, scope, role, selectedId, select }: { api: ControlPlane; scope: QueryScope; role: WorkspaceSession["role"]; selectedId: string | null; select: (id: string | null) => void }) {
  const queryClient = useQueryClient();
  const mutationKeys = useRef(new Map<string, string>());
  const keyFor = (operation: string) => {
    const existing = mutationKeys.current.get(operation);
    if (existing) return existing;
    const key = mutationId(); mutationKeys.current.set(operation, key); return key;
  };
  const connections = useQuery({
    queryKey: queryKey(scope, "connections"),
    queryFn: () => api.listConnections(),
    refetchInterval: selectedId ? 5000 : false,
  });
  const entries = useQuery({ queryKey: queryKey(scope, "registry-entries"), queryFn: () => api.listRegistryEntries() });
  const detail = useQuery({
    queryKey: queryKey(scope, "connection", selectedId),
    queryFn: () => api.getConnection(selectedId as string),
    enabled: selectedId !== null,
    refetchInterval: selectedId ? 2000 : false,
  });
  const [searchResult, setSearchResult] = useState<RegistrySearch | null>(null);
  const search = useMutation({
    mutationFn: (query: string) => api.searchRegistry(query),
    onMutate: () => setSearchResult(null),
    onSuccess: setSearchResult,
  });
  const refreshLists = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKey(scope, "connections") }),
      queryClient.invalidateQueries({ queryKey: queryKey(scope, "registry-entries") }),
      queryClient.invalidateQueries({ queryKey: queryKey(scope, "overview") }),
    ]);
  const create = useMutation({
    mutationFn: ({ input, key }: { input: { name: string; endpointUrl: string; secretBindingId?: string }; key: string; operation: string }) => api.createConnection(input, key),
    onSuccess: async (created, variables) => {
      mutationKeys.current.delete(variables.operation);
      select(created.id);
      await refreshLists();
    },
  });
  const action = useMutation({
    mutationFn: ({ id, verb, key }: { id: string; verb: "verify" | "refresh" | "enable" | "disable"; key: string }) =>
      api.connectionAction(id, verb, key),
    onSuccess: async (_data, variables) => {
      mutationKeys.current.delete(`connection:${variables.id}:${variables.verb}`);
      await refreshLists();
      await queryClient.invalidateQueries({ queryKey: queryKey(scope, "connection", selectedId) });
    },
  });
  const importEntry = useMutation({
    mutationFn: ({ cacheId, digest, key }: { cacheId: string; digest: string; key: string }) =>
      api.importRegistry(cacheId, digest, key),
    onSuccess: async (_data, variables) => { mutationKeys.current.delete(`import:${variables.digest}`); await refreshLists(); },
  });
  const append = useMutation({
    mutationFn: ({ endpointUrl, secretBindingId, key }: { endpointUrl: string; secretBindingId?: string; key: string; operation: string }) =>
      api.appendConnectionVersion(selectedId as string, { endpointUrl, secretBindingId }, key),
    onSuccess: async (_data, variables) => {
      mutationKeys.current.delete(variables.operation);
      await refreshLists();
      await queryClient.invalidateQueries({ queryKey: queryKey(scope, "connection", selectedId) });
    },
  });

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const query = formValue(data, "query");
    if (query) search.mutate(query);
  }

  function submitConnection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const input = {
      name: formValue(data, "name"),
      endpointUrl: formValue(data, "endpoint"),
      ...(formValue(data, "secret-binding") ? { secretBindingId: formValue(data, "secret-binding") } : {}),
    };
    const operation = `create:${JSON.stringify(input)}`;
    create.mutate({ input, operation, key: keyFor(operation) });
  }

  function submitVersion(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const secretBindingId = formValue(data, "version-secret-binding");
    const input = { endpointUrl: formValue(data, "version-endpoint"), ...(secretBindingId ? { secretBindingId } : {}) };
    const operation = `append:${selectedId}:${JSON.stringify(input)}`;
    append.mutate({ ...input, operation, key: keyFor(operation) });
  }

  return (
    <div className="page-flow">
      <header className="page-heading">
        <div>
          <p className="kicker">Discovery and trust</p>
          <h1>Server registry</h1>
          <p>Import screened metadata or connect an endpoint directly, then verify it.</p>
        </div>
      </header>
      <section className="workbench-grid">
        <div className="section-block discovery-block">
          <div className="section-heading">
            <div><span className="index">Official registry / 01</span><h2>Find a server</h2></div>
          </div>
          <form className="inline-form" onSubmit={submitSearch}>
            <label className="sr-only" htmlFor="registry-query">Registry search</label>
            <input id="registry-query" name="query" placeholder="Search by server or capability" required />
            <button className="secondary-action" disabled={search.isPending} type="submit">
              {search.isPending ? "Searching…" : "Search"}
            </button>
          </form>
          {search.isError && <QueryFailure error={search.error} retry={() => search.reset()} />}
          {importEntry.isError && <p className="field-error" role="alert">{failureMessage(importEntry.error)}</p>}
          {searchResult && (
            <div className="search-results" aria-live="polite">
              <p>{searchResult.items.length} screened results</p>
              {searchResult.items.length === 0 ? (
                <EmptyState title="No matching servers" copy="Try a more specific capability name." />
              ) : (
                <ul className="row-list">
                  {searchResult.items.map((item) => (
                    <li key={item.provenance_digest}>
                      <div><strong>{item.name}</strong><span>{item.description ?? item.external_id}</span></div>
                      <button
                        className="text-action"
                        type="button"
                        disabled={!canOperate(role) || importEntry.isPending}
                        onClick={() => importEntry.mutate({ cacheId: searchResult.cache_id, digest: item.provenance_digest, key: keyFor(`import:${item.provenance_digest}`) })}
                      >
                        Import
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </div>
        {isAdmin(role) && <div className="section-block manual-block">
          <div className="section-heading"><div><span className="index">Direct endpoint / 02</span><h2>Add manually</h2></div></div>
          <form className="stacked-form" onSubmit={submitConnection}>
            <label>Name<input name="name" placeholder="Internal developer tools" required maxLength={128} /></label>
            <label>HTTPS endpoint<input name="endpoint" type="url" placeholder="https://mcp.example.com/tools" required /></label>
            <label>Secret binding UUID <span>optional</span><input name="secret-binding" pattern="[0-9a-fA-F-]{36}" placeholder="Opaque binding identifier" /></label>
            {create.isError && <p className="field-error" role="alert">{failureMessage(create.error)}</p>}
            <button className="primary-action" disabled={create.isPending} type="submit">
              {create.isPending ? "Adding…" : "Add and verify"}
            </button>
          </form>
        </div>}
      </section>
      <section className="split-detail">
        <div className="section-block">
          <div className="section-heading"><div><span className="index">Connections / 03</span><h2>Trust inventory</h2></div><span>{entries.data?.length ?? 0} catalog entries</span></div>
          {connections.isPending ? <LoadingState label="Loading connections" /> : connections.isError ? (
            <QueryFailure error={connections.error} retry={() => void connections.refetch()} />
          ) : connections.data.length === 0 ? (
            <EmptyState title="No connection records" copy="Search the official registry or add an endpoint above." />
          ) : (
            <ul className="select-list">
              {connections.data.map((connection) => (
                <li key={connection.id}>
                  <button className={selectedId === connection.id ? "selected" : ""} type="button" onClick={() => select(connection.id)}>
                    <span><strong>{connection.name}</strong><small>{shortId(connection.id)}</small></span>
                    <StatusMark value={connection.lifecycle} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
        <aside className="detail-panel" aria-live="polite">
          {selectedId === null ? (
            <EmptyState title="Select a connection" copy="Inspect pinned versions and lifecycle controls." />
          ) : detail.isPending ? <LoadingState label="Loading connection detail" /> : detail.isError ? (
            <QueryFailure error={detail.error} retry={() => void detail.refetch()} />
          ) : (
            <>
              <span className="index">Connection detail</span>
              <div className="detail-title"><h2>{detail.data.name}</h2><StatusMark value={detail.data.lifecycle} /></div>
              <dl className="detail-facts">
                <div><dt>Control epoch</dt><dd>{detail.data.control_epoch}</dd></div>
                <div><dt>Refresh generation</dt><dd>{detail.data.refresh_generation}</dd></div>
                <div><dt>Last refresh</dt><dd>{formatTime(detail.data.last_refresh_at)}</dd></div>
              </dl>
              {detail.data.last_refresh_error_code && <p className="incident-note">Last refresh: {detail.data.last_refresh_error_code}</p>}
              {action.isError && <p className="field-error" role="alert">{failureMessage(action.error)}</p>}
              <div className="action-strip">
                {canOperate(role) && detail.data.pending_version_id && <button type="button" onClick={() => action.mutate({ id: detail.data.id, verb: "verify", key: keyFor(`connection:${detail.data.id}:verify`) })}>Verify pending</button>}
                {canOperate(role) && detail.data.lifecycle !== "disabled" && <button type="button" onClick={() => action.mutate({ id: detail.data.id, verb: "refresh", key: keyFor(`connection:${detail.data.id}:refresh`) })}>Refresh</button>}
                {canOperate(role) && detail.data.lifecycle !== "disabled" && <button type="button" onClick={() => action.mutate({ id: detail.data.id, verb: "disable", key: keyFor(`connection:${detail.data.id}:disable`) })}>Disable</button>}
                {isAdmin(role) && detail.data.lifecycle === "disabled" && <button type="button" onClick={() => action.mutate({ id: detail.data.id, verb: "enable", key: keyFor(`connection:${detail.data.id}:enable`) })}>
                  {detail.data.lifecycle === "disabled" ? "Re-enable" : "Disable"}
                </button>}
              </div>
              {isAdmin(role) && <form key={detail.data.id} className="stacked-form version-form" onSubmit={submitVersion}>
                <h3>Append immutable version</h3>
                <label>HTTPS endpoint<input name="version-endpoint" type="url" required defaultValue={detail.data.versions[0]?.endpoint_url} /></label>
                <label>Secret binding UUID <span>optional</span><input name="version-secret-binding" pattern="[0-9a-fA-F-]{36}" placeholder="Opaque binding identifier" /></label>
                {append.isError && <p className="field-error" role="alert">{failureMessage(append.error)}</p>}
                <button className="secondary-action" disabled={append.isPending} type="submit">{append.isPending ? "Appending…" : "Append version"}</button>
              </form>}
              <ol className="version-list">
                {detail.data.versions.map((version) => <li key={version.id}><span>v{version.sequence}</span><code>{version.endpoint_url}</code><small>{version.transport}</small></li>)}
              </ol>
              {detail.data.versions_truncated && <p className="field-help">Showing the 100 most recent versions.</p>}
            </>
          )}
        </aside>
      </section>
    </div>
  );
}

function Capabilities({ api, scope, role, selectedId, select }: { api: ControlPlane; scope: QueryScope; role: WorkspaceSession["role"]; selectedId: string | null; select: (id: string | null) => void }) {
  const queryClient = useQueryClient();
  const mutationKeys = useRef(new Map<string, string>());
  const keyFor = (operation: string) => mutationKeys.current.get(operation) ?? (() => { const key = mutationId(); mutationKeys.current.set(operation, key); return key; })();
  const capabilities = useQuery({ queryKey: queryKey(scope, "capabilities"), queryFn: () => api.listCapabilities() });
  const detail = useQuery({
    queryKey: queryKey(scope, "capability", selectedId),
    queryFn: () => api.getCapability(selectedId as string),
    enabled: selectedId !== null,
  });
  const action = useMutation({
    mutationFn: ({ versionId, verb, key }: { versionId: string; verb: "enable" | "disable"; key: string }) => api.capabilityAction(versionId, verb, key),
    onSuccess: async (_data, variables) => {
      mutationKeys.current.delete(`capability:${variables.versionId}:${variables.verb}`);
      await queryClient.invalidateQueries({ queryKey: queryKey(scope, "capabilities") });
      await queryClient.invalidateQueries({ queryKey: queryKey(scope, "capability", selectedId) });
      await queryClient.invalidateQueries({ queryKey: queryKey(scope, "overview") });
    },
  });
  return (
    <div className="page-flow">
      <header className="page-heading"><div><p className="kicker">Review and approval</p><h1>Capabilities</h1><p>Approve exact immutable versions. New schema never inherits old trust.</p></div></header>
      <section className="split-detail capability-layout">
        <div className="section-block">
          <div className="section-heading"><div><span className="index">Review queue / 01</span><h2>Discovered tools</h2></div></div>
          {capabilities.isPending ? <LoadingState label="Loading capabilities" /> : capabilities.isError ? (
            <QueryFailure error={capabilities.error} retry={() => void capabilities.refetch()} />
          ) : capabilities.data.length === 0 ? (
            <EmptyState title="No capabilities discovered" copy="Verify and refresh a connection first." />
          ) : (
            <ul className="select-list">
              {capabilities.data.map((capability) => (
                <li key={capability.id}><button className={selectedId === capability.id ? "selected" : ""} type="button" onClick={() => select(capability.id)}><span><strong>{capability.tool_identity}</strong><small>epoch {capability.status_epoch}</small></span><StatusMark value={capability.status} /></button></li>
              ))}
            </ul>
          )}
        </div>
        <aside className="detail-panel capability-detail">
          {selectedId === null ? <EmptyState title="Select a capability" copy="Compare metadata, schema, and version history." /> : detail.isPending ? (
            <LoadingState label="Loading capability detail" />
          ) : detail.isError ? <QueryFailure error={detail.error} retry={() => void detail.refetch()} /> : (
            <>
              <span className="index">Immutable capability</span>
              <div className="detail-title"><h2>{detail.data.tool_identity}</h2><StatusMark value={detail.data.status} /></div>
              {action.isError && <p className="field-error" role="alert">{failureMessage(action.error)}</p>}
              {detail.data.versions.map((version, index) => {
                const enabled = detail.data.enabled_version_id === version.id;
                const actionable = enabled || detail.data.pending_version_id === version.id;
                const verb = enabled ? "disable" : "enable";
                return (
                <article className="schema-version" key={version.id}>
                  <header><div><span>Version {version.sequence}</span><h3>{version.display_name}</h3></div>{index === 0 && <small>Latest observed</small>}</header>
                  <p>{version.description ?? "No description supplied."}</p>
                  <div className="digest-line"><span>metadata</span><code>{version.metadata_digest}</code></div>
                  <details><summary>Input schema</summary><pre>{JSON.stringify(version.input_schema, null, 2)}</pre></details>
                  <button
                    className={enabled ? "danger-action" : "secondary-action"}
                    type="button"
                    disabled={!canOperate(role) || !actionable || !version.schema_supported || action.isPending}
                    onClick={() => action.mutate({ versionId: version.id, verb, key: keyFor(`capability:${version.id}:${verb}`) })}
                  >
                    {!actionable ? "Historical version" : enabled ? "Disable version" : "Enable exact version"}
                  </button>
                </article>
              );})}
              {detail.data.versions_truncated && <p className="field-help">Older versions are retained but omitted from this response.</p>}
            </>
          )}
        </aside>
      </section>
    </div>
  );
}

function Runs({ api, scope, role, selectedId, select }: { api: ControlPlane; scope: QueryScope; role: WorkspaceSession["role"]; selectedId: string | null; select: (id: string | null) => void }) {
  const queryClient = useQueryClient();
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const runKey = useRef(mutationId());
  const cancelKeys = useRef(new Map<string, string>());
  const runs = useQuery({ queryKey: queryKey(scope, "runs"), queryFn: () => api.listRuns(), refetchInterval: 5000 });
  const capabilities = useQuery({ queryKey: queryKey(scope, "capabilities"), queryFn: () => api.listCapabilities() });
  const [argumentsText, setArgumentsText] = useState("{\n  \"query\": \"status\"\n}");
  const [argumentsError, setArgumentsError] = useState("");
  const [pendingArguments, setPendingArguments] = useState<Record<string, unknown> | null>(null);
  const [preflight, setPreflight] = useState<RunPreflight | null>(null);
  const runDetail = useQuery({
    queryKey: queryKey(scope, "run", selectedId),
    queryFn: () => api.getRun(selectedId as string),
    enabled: selectedId !== null,
    refetchInterval: (query) => query.state.data && terminal(query.state.data.status) ? false : 1500,
  });
  const events = useQuery({
    queryKey: queryKey(scope, "run-events", selectedId),
    queryFn: () => api.listRunEvents(selectedId as string),
    enabled: selectedId !== null,
    refetchInterval: runDetail.data && terminal(runDetail.data.status) ? false : 1500,
  });
  const prepare = useMutation({
    mutationFn: ({ versionId, args }: { versionId: string; args: Record<string, unknown> }) => api.preflight(versionId, args),
    onSuccess: setPreflight,
  });
  const invoke = useMutation({
    mutationFn: ({ prepared, args }: { prepared: RunPreflight; args: Record<string, unknown> }) => api.createRun(prepared, args, runKey.current),
    onSuccess: async (run) => {
      runKey.current = mutationId();
      select(run.id);
      setPreflight(null);
      setPendingArguments(null);
      await queryClient.invalidateQueries({ queryKey: queryKey(scope, "runs") });
    },
    onError: (error) => {
      if (error instanceof ApiFailure && ["invalid_confirmation", "confirmation_expired", "confirmation_replayed"].includes(error.code)) {
        setPreflight(null); setPendingArguments(null); runKey.current = mutationId();
      }
    },
  });
  const cancel = useMutation({
    mutationFn: ({ id, key }: { id: string; key: string }) => api.cancelRun(id, key),
    onSuccess: async (_data, variables) => {
      cancelKeys.current.delete(variables.id);
      await queryClient.invalidateQueries({ queryKey: queryKey(scope, "run", selectedId) });
      await queryClient.invalidateQueries({ queryKey: queryKey(scope, "runs") });
    },
  });
  const enabledCapabilities = (capabilities.data ?? []).filter((item) => item.status === "enabled" && item.enabled_version_id);

  function invalidatePreparedRun() {
    setPreflight(null); setPendingArguments(null); prepare.reset(); invoke.reset(); runKey.current = mutationId();
  }

  function submitPreflight(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const versionId = selectedVersionId;
    if (!versionId) {
      setArgumentsError("Select an enabled capability.");
      return;
    }
    try {
      const parsed = JSON.parse(argumentsText) as unknown;
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error();
      setArgumentsError("");
      setPendingArguments(parsed as Record<string, unknown>);
      prepare.mutate({ versionId, args: parsed as Record<string, unknown> });
    } catch {
      setArgumentsError("Arguments must be a JSON object.");
    }
  }

  return (
    <div className="page-flow">
      <header className="page-heading"><div><p className="kicker">Durable invocation</p><h1>Runs</h1><p>Preflight exact inputs, confirm once, then follow the append-only timeline.</p></div></header>
      <section className="run-grid">
        <div className="section-block playground">
          <div className="section-heading"><div><span className="index">Playground / 01</span><h2>Prepare a run</h2></div></div>
          <form className="stacked-form" onSubmit={submitPreflight} noValidate>
            <label>Enabled capability<select name="capability" required value={selectedVersionId} disabled={prepare.isPending || Boolean(preflight)} onChange={(event) => { invalidatePreparedRun(); setSelectedVersionId(event.target.value); }}><option value="" disabled>Select a capability</option>{enabledCapabilities.map((item) => <option key={item.id} value={item.enabled_version_id ?? ""}>{item.tool_identity}</option>)}</select></label>
            <label>Arguments<textarea value={argumentsText} disabled={prepare.isPending || Boolean(preflight)} onChange={(event) => { invalidatePreparedRun(); setArgumentsText(event.target.value); }} rows={8} spellCheck={false} aria-invalid={Boolean(argumentsError)} /></label>
            <p className="incident-note">Only public, synthetic, or explicitly non-confidential data.</p>
            {argumentsError && <p className="field-error" role="alert">{argumentsError}</p>}
            {prepare.isError && <p className="field-error" role="alert">{failureMessage(prepare.error)}</p>}
            <button className="primary-action" disabled={!canOperate(role) || prepare.isPending || enabledCapabilities.length === 0} type="submit">{prepare.isPending ? "Checking…" : "Review invocation"}</button>
          </form>
          {enabledCapabilities.length === 0 && <p className="field-help">Enable a capability version before opening a run.</p>}
        </div>
        <div className="section-block run-inventory">
          <div className="section-heading"><div><span className="index">Ledger / 02</span><h2>Recent runs</h2></div></div>
          {runs.isPending ? <LoadingState label="Loading runs" /> : runs.isError ? <QueryFailure error={runs.error} retry={() => void runs.refetch()} /> : runs.data.length === 0 ? <EmptyState title="No runs retained" copy="Completed and in-flight work will appear here." /> : (
            <ul className="select-list">{runs.data.map((run) => <li key={run.id}><button className={selectedId === run.id ? "selected" : ""} type="button" onClick={() => select(run.id)}><span><strong>{shortId(run.id)}</strong><small>{formatTime(run.created_at)}</small></span><StatusMark value={run.status} /></button></li>)}</ul>
          )}
        </div>
      </section>
      {preflight && pendingArguments && (
        <section className="confirmation-panel" aria-labelledby="confirmation-title">
          <div><span className="index">One-time confirmation</span><h2 id="confirmation-title">Confirm exact invocation</h2><p>This token expires {formatTime(preflight.expires_at)} and cannot move to another request.</p></div>
          <p className="incident-note">Only public, synthetic, or explicitly non-confidential data.</p>
          <dl><div><dt>Capability version</dt><dd><code>{shortId(preflight.capability_version_id)}</code></dd></div><div><dt>Argument digest</dt><dd><code>{shortId(preflight.argument_digest)}</code></dd></div></dl>
          <details open><summary>Exact prepared arguments</summary><pre>{JSON.stringify(pendingArguments, null, 2)}</pre></details>
          {invoke.isError && <p className="field-error" role="alert">{failureMessage(invoke.error)}{invoke.error instanceof ApiFailure && ["invalid_confirmation", "confirmation_expired", "confirmation_replayed"].includes(invoke.error.code) ? " Run preflight again." : " Retry to safely reuse this request."}</p>}
          <div className="action-strip"><button type="button" onClick={invalidatePreparedRun}>Back</button><button className="primary-action" type="button" disabled={invoke.isPending} onClick={() => invoke.mutate({ prepared: preflight, args: pendingArguments })}>{invoke.isPending ? "Submitting…" : "Confirm and run"}</button></div>
        </section>
      )}
      {invoke.isError && !preflight && <p className="field-error" role="alert">{failureMessage(invoke.error)} Run preflight again.</p>}
      <section className="detail-panel run-detail">
        {selectedId === null ? <EmptyState title="Select a run" copy="Inspect lineage, safe output, and status events." /> : runDetail.isPending || events.isPending ? <LoadingState label="Loading run timeline" /> : runDetail.isError ? <QueryFailure error={runDetail.error} retry={() => void runDetail.refetch()} /> : events.isError ? <QueryFailure error={events.error} retry={() => void events.refetch()} /> : (
          <>
            <div className="detail-title"><div><span className="index">Run {shortId(runDetail.data.id)}</span><h2>Execution timeline</h2></div><StatusMark value={runDetail.data.status} /></div>
            <dl className="detail-facts"><div><dt>Capability</dt><dd>{shortId(runDetail.data.capability_version_id)}</dd></div><div><dt>Connection version</dt><dd>{shortId(runDetail.data.connection_version_id)}</dd></div><div><dt>Deadline</dt><dd>{formatTime(runDetail.data.deadline)}</dd></div><div><dt>Updated</dt><dd>{formatTime(runDetail.data.updated_at)}</dd></div></dl>
            {canOperate(role) && !terminal(runDetail.data.status) && <button className="danger-action" disabled={cancel.isPending} type="button" onClick={() => { const key = cancelKeys.current.get(runDetail.data.id) ?? mutationId(); cancelKeys.current.set(runDetail.data.id, key); cancel.mutate({ id: runDetail.data.id, key }); }}>Request cancellation</button>}
            {cancel.isError && <p className="field-error" role="alert">{failureMessage(cancel.error)}</p>}
            <ol className="timeline">{events.data.map((event) => <li key={event.id}><span aria-hidden="true" /><div><strong>{event.event_type.replaceAll("_", " ")}</strong><small>{formatTime(event.occurred_at)} · {event.status}</small>{event.safe_error_code && <code>{event.safe_error_code}</code>}</div></li>)}</ol>
            <details><summary>Retained arguments</summary><pre>{JSON.stringify(runDetail.data.arguments, null, 2)}</pre></details>
            {runDetail.data.result && <details open><summary>Validated result</summary><pre>{JSON.stringify(runDetail.data.result, null, 2)}</pre></details>}
            {runDetail.data.safe_error_code && <p className="incident-note">Run ended with {runDetail.data.safe_error_code}.</p>}
          </>
        )}
      </section>
    </div>
  );
}

function WorkspaceApp({ session, api, onLogout }: { session: WorkspaceSession; api: ControlPlane; onLogout: () => void }) {
  const [route, setRoute] = useState<Route>(() => readRoute());
  useEffect(() => {
    const handlePopState = () => setRoute(readRoute());
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);
  function navigate(view: View, selectedId: string | null = null) {
    window.history.pushState({}, "", routePath(view, selectedId));
    setRoute({ view, selectedId });
  }
  const view = route.view;
  const scope: QueryScope = ["workspace", session.identityId, session.workspaceId];
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <header className="topbar">
        <button className="brand" type="button" onClick={() => navigate("overview")} aria-label="Modall overview"><span>m</span><strong>modall</strong></button>
        <nav aria-label="Primary navigation">{navItems.map((item) => <button key={item.id} className={view === item.id ? "active" : ""} type="button" onClick={() => navigate(item.id)} aria-current={view === item.id ? "page" : undefined}><span>{item.marker}</span>{item.label}</button>)}</nav>
        <div className="workspace-menu"><div><span>{session.workspaceLabel} · {session.role}</span><code>{shortId(session.workspaceId)}</code></div><button className="text-action" type="button" onClick={onLogout}>Log out</button></div>
      </header>
      <main id="main-content" tabIndex={-1}>
        {view === "overview" && <Overview api={api} open={(next) => navigate(next)} scope={scope} />}
        {view === "registry" && <Registry api={api} scope={scope} role={session.role} selectedId={route.selectedId} select={(id) => navigate("registry", id)} />}
        {view === "capabilities" && <Capabilities api={api} scope={scope} role={session.role} selectedId={route.selectedId} select={(id) => navigate("capabilities", id)} />}
        {view === "runs" && <Runs api={api} scope={scope} role={session.role} selectedId={route.selectedId} select={(id) => navigate("runs", id)} />}
      </main>
      <footer><span>Modall Registry Alpha</span><span>Exact lineage · bounded retention · fail closed</span></footer>
    </div>
  );
}

export function App({ apiFactory = createControlPlane }: { apiFactory?: ApiFactory }) {
  const [session, setSession] = useState<WorkspaceSession | null>(() => loadSession());
  const api = useMemo(() => (session ? apiFactory(session) : null), [apiFactory, session]);

  function logout() {
    clearSession();
    setSession(null);
  }

  return session && api ? (
    <WorkspaceApp session={session} api={api} onLogout={logout} />
  ) : (
    <SignIn onSignIn={setSession} />
  );
}

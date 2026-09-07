import { FormEvent, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  ApiFailure,
  createControlPlane,
  type AuditFilters,
  type CapabilityStatus,
  type ControlPlane,
  type RegistrySearch,
  type RunFilters,
  type RunPreflight,
} from "./api/operations";
import {
  clearSession,
  isWorkspaceId,
  loadSession,
  saveSession,
  type WorkspaceSession,
} from "./session";

type View = "overview" | "registry" | "capabilities" | "runs" | "audit";
type Role = "admin" | "operator" | "viewer";
type CapabilityFilter = CapabilityStatus | "all";
type ApiFactory = (session: WorkspaceSession) => ControlPlane;
type QueryScope = readonly ["workspace", string, string];
type RunDraft = {
  selectedVersionId: string;
  argumentsText: string;
  pendingArguments: Record<string, unknown> | null;
  preflight: RunPreflight | null;
};

const navItems: { id: View; label: string; marker: string }[] = [
  { id: "overview", label: "Overview", marker: "01" },
  { id: "registry", label: "Registry", marker: "02" },
  { id: "capabilities", label: "Capabilities", marker: "03" },
  { id: "runs", label: "Runs", marker: "04" },
  { id: "audit", label: "Audit", marker: "05" },
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
    second: "2-digit",
  }).format(new Date(value));
}

function localDateTimeValue(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}

function shortId(value: string): string {
  return `${value.slice(0, 8)}…${value.slice(-4)}`;
}

function terminal(status: string): boolean {
  return ["succeeded", "failed", "cancelled", "timed_out", "indeterminate"].includes(status);
}

function mutationId(): string { return crypto.randomUUID(); }
function canOperate(role: Role): boolean { return role !== "viewer"; }
function isAdmin(role: Role): boolean { return role === "admin"; }

type Route = { view: View; selectedId: string | null };
function readRoute(): Route {
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts[0] === "connections") return { view: "registry", selectedId: parts[1] ?? null };
  if (parts[0] === "capabilities") return { view: "capabilities", selectedId: parts[1] ?? null };
  if (parts[0] === "runs") return { view: "runs", selectedId: parts[1] ?? null };
  if (parts[0] === "audit") return { view: "audit", selectedId: null };
  if (parts[0] === "registry") return { view: "registry", selectedId: null };
  return { view: "overview", selectedId: null };
}

function routePath(view: View, selectedId: string | null = null): string {
  if (view === "overview") return "/";
  if (view === "registry") return selectedId ? `/connections/${selectedId}` : "/registry";
  return selectedId ? `/${view}/${selectedId}` : `/${view}`;
}

function capabilityFilterFromLocation(): CapabilityFilter {
  const status = new URLSearchParams(window.location.search).get("status");
  return ["pending_review", "enabled", "disabled", "unavailable"].includes(status ?? "")
    ? status as CapabilityStatus
    : "all";
}

function capabilityPath(filter: CapabilityFilter, selectedId: string | null = null): string {
  const path = routePath("capabilities", selectedId);
  return filter === "all" ? path : `${path}?status=${encodeURIComponent(filter)}`;
}

function failureMessage(error: unknown): string {
  if (error instanceof ApiFailure) {
    const reference = error.correlationId ? ` Reference ${shortId(error.correlationId)}.` : "";
    return `${error.message} Code ${error.code}.${reference}`;
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

function Overview({ api, open, scope }: { api: ControlPlane; open: (view: View, capabilityFilter?: CapabilityFilter) => void; scope: QueryScope }) {
  const query = useQuery({ queryKey: queryKey(scope, "overview"), queryFn: () => api.overview(), refetchInterval: (current) => current.state.status === "error" ? false : 5000 });
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
        <p className="timestamp">Updated {formatTime(new Date(query.dataUpdatedAt).toISOString())}</p>
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
          <small>{query.data.runs.length} visible</small>
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
          <button className="secondary-action" type="button" onClick={() => open("capabilities", "pending_review")}>
            Open review queue
          </button>
        </aside>
      </section>
    </div>
  );
}

function Registry({ api, scope, role, selectedId, select, mutationKeys }: { api: ControlPlane; scope: QueryScope; role: Role; selectedId: string | null; select: (id: string | null) => void; mutationKeys: { current: Map<string, string> } }) {
  const queryClient = useQueryClient();
  const keyFor = (operation: string) => {
    const existing = mutationKeys.current.get(operation);
    if (existing) return existing;
    const key = mutationId(); mutationKeys.current.set(operation, key); return key;
  };
  const connections = useQuery({
    queryKey: queryKey(scope, "connections"),
    queryFn: () => api.listConnections(),
    refetchInterval: (query) => query.state.status === "error" ? false : 5000,
  });
  const entries = useQuery({
    queryKey: queryKey(scope, "registry-entries"),
    queryFn: () => api.listRegistryEntries(),
    refetchInterval: (query) => query.state.status === "error" ? false : 5000,
  });
  const detail = useQuery({
    queryKey: queryKey(scope, "connection", selectedId),
    queryFn: () => api.getConnection(selectedId as string),
    enabled: selectedId !== null,
    refetchInterval: (query) => selectedId && query.state.status !== "error" ? 2000 : false,
  });
  const [searchResult, setSearchResult] = useState<RegistrySearch | null>(null);
  const [lastSearchQuery, setLastSearchQuery] = useState("");
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    if (!searchResult) return;
    const delay = Math.max(0, Date.parse(searchResult.expires_at) - Date.now());
    const timer = window.setTimeout(() => setClock(Date.now()), delay + 1);
    return () => window.clearTimeout(timer);
  }, [searchResult]);
  const searchExpired = searchResult ? Date.parse(searchResult.expires_at) <= clock : false;
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
    mutationFn: ({ id, verb, key }: { id: string; verb: "verify" | "refresh" | "enable" | "disable"; key: string; operation: string }) =>
      api.connectionAction(id, verb, key),
    onSuccess: async (_data, variables) => {
      mutationKeys.current.delete(variables.operation);
      await refreshLists();
      await queryClient.invalidateQueries({ queryKey: queryKey(scope, "connection", selectedId) });
    },
  });
  const resetAction = action.reset;
  useEffect(() => resetAction(), [resetAction, selectedId]);
  const importEntry = useMutation({
    mutationFn: ({ cacheId, digest, key }: { cacheId: string; digest: string; key: string }) =>
      api.importRegistry(cacheId, digest, key),
    onSuccess: async (_data, variables) => { mutationKeys.current.delete(`import:${variables.cacheId}:${variables.digest}`); await refreshLists(); },
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
    if (query) { setLastSearchQuery(query); search.mutate(query); }
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
    const baseVersionId = detail.data?.versions[0]?.id ?? "none";
    const operation = `append:${selectedId}:${baseVersionId}:${JSON.stringify(input)}`;
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
            <input id="registry-query" name="query" placeholder="Search by server or capability" required disabled={!canOperate(role)} />
            <button className="secondary-action" disabled={!canOperate(role) || search.isPending} type="submit">
              {search.isPending ? "Searching…" : "Search"}
            </button>
          </form>
          {!canOperate(role) && <p className="field-help">Registry search requires an Operator or Admin role.</p>}
          {search.isError && <QueryFailure error={search.error} retry={() => search.mutate(lastSearchQuery)} />}
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
                      <div><strong>{item.name}</strong><span>{item.description ?? "No description supplied."}</span><code>{item.external_id} @ {item.source_version}</code>{item.advertised_urls.map((url) => <code key={url}>{url}</code>)}</div>
                      <button
                        className="text-action"
                        type="button"
                        disabled={!canOperate(role) || searchExpired || importEntry.isPending}
                        onClick={() => importEntry.mutate({ cacheId: searchResult.cache_id, digest: item.provenance_digest, key: keyFor(`import:${searchResult.cache_id}:${item.provenance_digest}`) })}
                      >
                        Import
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          {searchExpired && <p className="field-help">These results expired. Search again before importing.</p>}
        </div>
        {isAdmin(role) && <div className="section-block manual-block">
          <div className="section-heading"><div><span className="index">Direct endpoint / 02</span><h2>Add manually</h2></div></div>
          <form className="stacked-form" onSubmit={submitConnection}>
            <label>Name<input name="name" placeholder="Internal developer tools" required maxLength={128} /></label>
            <label>HTTPS endpoint<input name="endpoint" type="url" placeholder="https://mcp.example.com/tools" required /></label>
            <label>Secret binding UUID <span>optional</span><input name="secret-binding" pattern="[0-9a-fA-F-]{36}" placeholder="Opaque binding identifier" /></label>
            {create.isError && <p className="field-error" role="alert">{failureMessage(create.error)}</p>}
            <button className="primary-action" disabled={create.isPending} type="submit">
              {create.isPending ? "Adding…" : "Add connection"}
            </button>
          </form>
        </div>}
      </section>
      <section className="split-detail">
        <div className="section-block">
          <div className="section-heading"><div><span className="index">Connections / 03</span><h2>Trust inventory</h2></div><span>{entries.isError ? "—" : entries.data?.length ?? 0} catalog entries</span></div>
          {entries.isError && <QueryFailure error={entries.error} retry={() => void entries.refetch()} />}
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
                {canOperate(role) && detail.data.lifecycle !== "disabled" && detail.data.pending_version_id && <button type="button" onClick={() => { const operation = `connection:${detail.data.id}:${detail.data.control_epoch}:${detail.data.refresh_generation}:verify`; action.mutate({ id: detail.data.id, verb: "verify", operation, key: keyFor(operation) }); }}>Verify pending</button>}
                {canOperate(role) && detail.data.lifecycle !== "disabled" && <button type="button" onClick={() => { const operation = `connection:${detail.data.id}:${detail.data.control_epoch}:${detail.data.refresh_generation}:refresh`; action.mutate({ id: detail.data.id, verb: "refresh", operation, key: keyFor(operation) }); }}>Refresh</button>}
                {canOperate(role) && detail.data.lifecycle !== "disabled" && <button type="button" onClick={() => { const operation = `connection:${detail.data.id}:${detail.data.control_epoch}:disable`; action.mutate({ id: detail.data.id, verb: "disable", operation, key: keyFor(operation) }); }}>Disable</button>}
                {isAdmin(role) && detail.data.lifecycle === "disabled" && <button type="button" onClick={() => { const operation = `connection:${detail.data.id}:${detail.data.control_epoch}:enable`; action.mutate({ id: detail.data.id, verb: "enable", operation, key: keyFor(operation) }); }}>
                  {detail.data.lifecycle === "disabled" ? "Re-enable" : "Disable"}
                </button>}
              </div>
              {isAdmin(role) && detail.data.lifecycle !== "disabled" && <form key={`${detail.data.id}:${detail.data.versions[0]?.id ?? "none"}`} className="stacked-form version-form" onSubmit={submitVersion}>
                <h3>Append immutable version</h3>
                <label>HTTPS endpoint<input name="version-endpoint" type="url" required defaultValue={detail.data.versions[0]?.endpoint_url} /></label>
                <label>Secret binding UUID <span>optional</span><input name="version-secret-binding" pattern="[0-9a-fA-F-]{36}" defaultValue={detail.data.versions[0]?.secret_binding_id ?? ""} placeholder="Opaque binding identifier" /></label>
                {append.isError && <p className="field-error" role="alert">{failureMessage(append.error)}</p>}
                <button className="secondary-action" disabled={append.isPending} type="submit">{append.isPending ? "Appending…" : "Append version"}</button>
              </form>}
              <ol className="version-list">
                {detail.data.versions.map((version) => <li key={version.id}><span>v{version.sequence}</span><code>{version.endpoint_url}</code><small>{version.transport} · policy {version.policy_version} · binding {version.secret_binding_id ? shortId(version.secret_binding_id) : "none"}</small></li>)}
              </ol>
              {detail.data.versions_truncated && <p className="field-help">Showing the 100 most recent versions.</p>}
            </>
          )}
        </aside>
      </section>
    </div>
  );
}

function Capabilities({ api, scope, role, selectedId, filter, select, setFilter, mutationKeys }: { api: ControlPlane; scope: QueryScope; role: Role; selectedId: string | null; filter: CapabilityFilter; select: (id: string | null) => void; setFilter: (filter: CapabilityFilter) => void; mutationKeys: { current: Map<string, string> } }) {
  const queryClient = useQueryClient();
  const keyFor = (operation: string) => mutationKeys.current.get(operation) ?? (() => { const key = mutationId(); mutationKeys.current.set(operation, key); return key; })();
  const capabilities = useInfiniteQuery({
    queryKey: queryKey(scope, "capabilities", filter),
    queryFn: ({ pageParam }) => api.listCapabilityPage(filter === "all" ? undefined : filter, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.nextCursor,
  });
  const connections = useQuery({ queryKey: queryKey(scope, "connections"), queryFn: () => api.listConnections() });
  const detail = useQuery({
    queryKey: queryKey(scope, "capability", selectedId),
    queryFn: () => api.getCapability(selectedId as string),
    enabled: selectedId !== null,
    refetchInterval: (query) => query.state.status === "error" ? false : 5000,
  });
  const action = useMutation({
    mutationFn: ({ versionId, verb, key }: { versionId: string; verb: "enable" | "disable"; key: string; operation: string }) => api.capabilityAction(versionId, verb, key),
    onSuccess: async (_data, variables) => {
      mutationKeys.current.delete(variables.operation);
      await queryClient.invalidateQueries({ queryKey: queryKey(scope, "capabilities") });
      await queryClient.invalidateQueries({ queryKey: queryKey(scope, "capability", selectedId) });
      await queryClient.invalidateQueries({ queryKey: queryKey(scope, "overview") });
    },
  });
  const capabilityRows = capabilities.data?.pages.flatMap((page) => page.items) ?? [];
  const connectionNames = new Map((connections.data ?? []).map((item) => [item.id, item.name]));
  return (
    <div className="page-flow">
      <header className="page-heading"><div><p className="kicker">Review and approval</p><h1>Capabilities</h1><p>Approve exact immutable versions. New schema never inherits old trust.</p></div></header>
      <section className="split-detail capability-layout">
        <div className="section-block">
          <div className="section-heading"><div><span className="index">Review queue / 01</span><h2>Discovered tools</h2></div><button className="secondary-action" type="button" disabled={capabilities.isFetching} onClick={() => void capabilities.refetch()}>{capabilities.isFetching ? "Refreshing…" : "Refresh tools"}</button></div>
          <form className="inline-form" onSubmit={(event) => event.preventDefault()}>
            <label>Status<select aria-label="Capability status" value={filter} onChange={(event) => setFilter(event.target.value as CapabilityFilter)}><option value="pending_review">Pending review</option><option value="enabled">Enabled</option><option value="disabled">Disabled</option><option value="unavailable">Unavailable</option><option value="all">All statuses</option></select></label>
          </form>
          {capabilities.isPending ? <LoadingState label="Loading capabilities" /> : capabilities.isError ? (
            <QueryFailure error={capabilities.error} retry={() => void capabilities.refetch()} />
          ) : capabilityRows.length === 0 ? (
            <EmptyState title="No capabilities discovered" copy="Verify and refresh a connection first." />
          ) : (
            <ul className="select-list">
              {capabilityRows.map((capability) => (
                <li key={capability.id}><button className={selectedId === capability.id ? "selected" : ""} type="button" onClick={() => select(capability.id)}><span><strong>{capability.tool_identity}</strong><small>{connectionNames.get(capability.connection_id) ?? shortId(capability.connection_id)} · epoch {capability.status_epoch}</small></span><StatusMark value={capability.status} /></button></li>
              ))}
            </ul>
          )}
          {capabilities.hasNextPage && <button className="secondary-action" disabled={capabilities.isFetchingNextPage} type="button" onClick={() => void capabilities.fetchNextPage()}>{capabilities.isFetchingNextPage ? "Loading…" : "Load older tools"}</button>}
          {connections.isError && <QueryFailure error={connections.error} retry={() => void connections.refetch()} />}
        </div>
        <aside className="detail-panel capability-detail">
          {selectedId === null ? <EmptyState title="Select a capability" copy="Compare metadata, schema, and version history." /> : detail.isPending ? (
            <LoadingState label="Loading capability detail" />
          ) : detail.isError ? <QueryFailure error={detail.error} retry={() => void detail.refetch()} /> : (
            <>
              <span className="index">Immutable capability</span>
              <div className="detail-title"><h2>{detail.data.tool_identity}</h2><StatusMark value={detail.data.status} /></div>
              <p className="field-help">Source connection: {connectionNames.get(detail.data.connection_id) ?? detail.data.connection_id}</p>
              {action.isError && <p className="field-error" role="alert">{failureMessage(action.error)}</p>}
              {detail.data.versions.map((version, index) => {
                const retained = detail.data.enabled_version_id === version.id;
                const pending = detail.data.pending_version_id === version.id;
                const disableable = (detail.data.status === "enabled" || detail.data.status === "unavailable") && retained;
                const reenable = detail.data.status === "disabled" && detail.data.pending_version_id === null && retained;
                const rejected = detail.data.status === "disabled" && pending;
                const actionable = disableable || reenable || pending;
                const verb = disableable ? "disable" : "enable";
                return (
                <article className="schema-version" key={version.id}>
                  <header><div><span>Version {version.sequence}</span><h3>{version.display_name}</h3></div>{index === 0 && <small>Latest observed</small>}</header>
                  <p>{version.description ?? "No description supplied."}</p>
                  <div className="digest-line"><span>metadata</span><code>{version.metadata_digest}</code></div>
                  <div className="digest-line"><span>connection version</span><code>{version.connection_version_id}</code></div>
                  <details><summary>Input schema</summary><pre>{JSON.stringify(version.input_schema, null, 2)}</pre></details>
                  {version.output_schema && <details><summary>Output schema</summary><pre>{JSON.stringify(version.output_schema, null, 2)}</pre></details>}
                  {pending && !rejected ? <div className="action-strip">
                    <button className="danger-action" type="button" disabled={!canOperate(role) || action.isPending} onClick={() => { const operation = `capability:${version.id}:${detail.data.status_epoch}:disable`; action.mutate({ versionId: version.id, verb: "disable", operation, key: keyFor(operation) }); }}>Reject version</button>
                    <button className="secondary-action" type="button" disabled={!canOperate(role) || detail.data.status === "unavailable" || detail.data.observed_in_current_snapshot === false || !version.schema_supported || action.isPending} onClick={() => { const operation = `capability:${version.id}:${detail.data.status_epoch}:enable`; action.mutate({ versionId: version.id, verb: "enable", operation, key: keyFor(operation) }); }}>Enable exact version</button>
                  </div> : <button
                    className={disableable ? "danger-action" : "secondary-action"}
                    type="button"
                    disabled={!canOperate(role) || !actionable || (verb === "enable" && detail.data.observed_in_current_snapshot === false) || !version.schema_supported || action.isPending}
                    onClick={() => { const operation = `capability:${version.id}:${detail.data.status_epoch}:${verb}`; action.mutate({ versionId: version.id, verb, operation, key: keyFor(operation) }); }}
                  >
                    {!actionable ? "Historical version" : disableable ? "Disable version" : rejected ? "Reconsider version" : "Re-enable version"}
                  </button>}
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

function Runs({ api, scope, role, selectedId, select, runKeys, cancelKeys, draft, setDraft }: { api: ControlPlane; scope: QueryScope; role: Role; selectedId: string | null; select: (id: string | null) => void; runKeys: { current: Map<string, string> }; cancelKeys: { current: Map<string, string> }; draft: RunDraft; setDraft: Dispatch<SetStateAction<RunDraft>> }) {
  const queryClient = useQueryClient();
  const { selectedVersionId, argumentsText, pendingArguments, preflight } = draft;
  const finalEventFetchRun = useRef<string | null>(null);
  const viewMounted = useRef(true);
  useEffect(() => () => { viewMounted.current = false; }, []);
  const runsKey = useMemo(() => queryKey(scope, "runs"), [scope]);
  const eventsKey = useMemo(() => queryKey(scope, "run-events", selectedId), [scope, selectedId]);
  const runs = useQuery({
    queryKey: runsKey,
    queryFn: () => api.listRuns(),
    refetchInterval: (query) => query.state.status === "error" ? false : 3000,
  });
  const [runFilters, setRunFilters] = useState<RunFilters>({});
  const [runFilterFormKey, setRunFilterFormKey] = useState(0);
  const history = useInfiniteQuery({
    queryKey: queryKey(scope, "run-history", runFilters),
    queryFn: ({ pageParam }) => api.listRunPage(runFilters, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.nextCursor,
    refetchInterval: (query) => query.state.status === "error" ? false : 3000,
  });
  const hasRunFilters = Object.keys(runFilters).length > 0;
  const capabilities = useQuery({
    queryKey: queryKey(scope, "capabilities"),
    queryFn: () => api.listCapabilities("enabled"),
    refetchInterval: (query) => query.state.status === "error" ? false : 5000,
  });
  const connections = useQuery({
    queryKey: queryKey(scope, "connections"),
    queryFn: () => api.listConnections(),
    refetchInterval: (query) => query.state.status === "error" ? false : 5000,
  });
  const [argumentsError, setArgumentsError] = useState("");
  const [confirmationClock, setConfirmationClock] = useState(() => Date.now());
  const runDetail = useQuery({
    queryKey: queryKey(scope, "run", selectedId),
    queryFn: () => api.getRun(selectedId as string),
    enabled: selectedId !== null,
    refetchInterval: (query) => {
      if (query.state.status === "error") return false;
      const current = query.state.data;
      if (!current || !terminal(current.status)) return 1500;
      return current.arguments !== null || current.result !== null ? 1000 : false;
    },
  });
  const events = useQuery({
    queryKey: eventsKey,
    queryFn: () => api.listRunEvents(selectedId as string),
    enabled: selectedId !== null,
    refetchInterval: (query) => {
      if (query.state.status === "error") return false;
      const current = runDetail.data;
      if (!current || !terminal(current.status)) return 1500;
      const argumentExpired = Date.parse(current.arguments_expires_at) <= Date.now();
      const expiryPublished = query.state.data?.some((event) => event.event_type === "content_expired");
      return argumentExpired && !expiryPublished ? 10_000 : false;
    },
  });
  useEffect(() => {
    const current = runDetail.data;
    if (!current) return;
    queryClient.setQueryData<Awaited<ReturnType<ControlPlane["listRuns"]>>>(runsKey, (previous) =>
      previous?.map((item) => item.id === current.id ? current : item),
    );
  }, [queryClient, runDetail.data, runsKey]);
  useEffect(() => {
    const current = runDetail.data;
    if (!current || !terminal(current.status) || finalEventFetchRun.current === current.id) return;
    finalEventFetchRun.current = current.id;
    void queryClient.refetchQueries({ queryKey: eventsKey, exact: true });
  }, [eventsKey, queryClient, runDetail.data]);
  useEffect(() => {
    if (!preflight) return;
    const delay = Math.max(0, Date.parse(preflight.expires_at) - Date.now());
    const timer = window.setTimeout(() => setConfirmationClock(Date.now()), delay + 1);
    return () => window.clearTimeout(timer);
  }, [preflight]);
  const prepare = useMutation({
    mutationFn: ({ versionId, args }: { versionId: string; args: Record<string, unknown> }) => api.preflight(versionId, args),
    onSuccess: (prepared) => { setConfirmationClock(Date.now()); setDraft((current) => ({ ...current, preflight: prepared })); },
  });
  const invoke = useMutation({
    mutationFn: ({ prepared, args }: { prepared: RunPreflight; args: Record<string, unknown> }) => {
      const operation = `${prepared.capability_version_id}:${prepared.argument_digest}`;
      const key = runKeys.current.get(operation) ?? mutationId();
      runKeys.current.set(operation, key);
      return api.createRun(prepared, args, key);
    },
    onSuccess: async (run, variables) => {
      runKeys.current.delete(`${variables.prepared.capability_version_id}:${variables.prepared.argument_digest}`);
      if (viewMounted.current) select(run.id);
      setDraft((current) => ({ ...current, preflight: null, pendingArguments: null }));
      await queryClient.invalidateQueries({ queryKey: queryKey(scope, "runs") });
    },
    onError: (error, variables) => {
      if (error instanceof ApiFailure && ["invalid_confirmation", "confirmation_expired", "confirmation_replayed"].includes(error.code)) {
        runKeys.current.delete(`${variables.prepared.capability_version_id}:${variables.prepared.argument_digest}`);
        setDraft((current) => ({ ...current, preflight: null, pendingArguments: null }));
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
  const connectionById = new Map((connections.data ?? []).map((item) => [item.id, item]));
  const enabledCapabilities = (capabilities.data ?? []).filter((item) => {
    const connection = connectionById.get(item.connection_id);
    return item.status === "enabled" && item.enabled_version_id && connection?.lifecycle === "active" && connection.pending_version_id === null && connection.verified_version_id !== null;
  });
  const connectionNames = new Map((connections.data ?? []).map((item) => [item.id, item.name]));
  const capabilityNames = new Map((capabilities.data ?? []).map((item) => [item.id, item.tool_identity]));
  const runIndex = new Map((history.data?.pages.flatMap((page) => page.items) ?? []).map((run) => [run.id, run]));
  if (!hasRunFilters) for (const run of runs.data ?? []) runIndex.set(run.id, run);
  const orderedRuns = [...runIndex.values()].sort((left, right) => right.created_at.localeCompare(left.created_at));
  const selectedCapability = enabledCapabilities.find((item) => item.enabled_version_id === selectedVersionId);
  const confirmationConnection = useQuery({
    queryKey: queryKey(scope, "confirmation-connection", selectedCapability?.connection_id),
    queryFn: () => api.getConnection(selectedCapability?.connection_id as string),
    enabled: Boolean(preflight && selectedCapability),
  });
  const confirmedEndpoint = confirmationConnection.data?.versions.find((version) => version.id === preflight?.connection_version_id)?.endpoint_url;
  const confirmationExpired = preflight ? Date.parse(preflight.expires_at) <= confirmationClock : false;

  function invalidatePreparedRun() {
    setDraft((current) => ({ ...current, preflight: null, pendingArguments: null })); prepare.reset(); invoke.reset();
  }

  function abandonPreparedRun() {
    if (preflight) runKeys.current.delete(`${preflight.capability_version_id}:${preflight.argument_digest}`);
    invalidatePreparedRun();
  }

  function submitPreflight(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const versionId = selectedVersionId;
    if (!versionId || !selectedCapability) {
      setArgumentsError("Select an enabled capability.");
      return;
    }
    try {
      const parsed = JSON.parse(argumentsText) as unknown;
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error();
      setArgumentsError("");
      setDraft((current) => ({ ...current, pendingArguments: parsed as Record<string, unknown> }));
      prepare.mutate({ versionId, args: parsed as Record<string, unknown> });
    } catch {
      setArgumentsError("Arguments must be a JSON object.");
    }
  }

  function submitRunFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const status = formValue(data, "run-status");
    const capabilityId = formValue(data, "run-capability");
    const actorId = formValue(data, "run-actor");
    const createdAfter = formValue(data, "run-created-after");
    const createdBefore = formValue(data, "run-created-before");
    const minDuration = formValue(data, "run-min-duration");
    const maxDuration = formValue(data, "run-max-duration");
    setRunFilters({
      ...(status ? { status } : {}),
      ...(capabilityId ? { capability_id: capabilityId } : {}),
      ...(actorId ? { actor_id: actorId } : {}),
      ...(createdAfter ? { created_after: new Date(createdAfter).toISOString() } : {}),
      ...(createdBefore ? { created_before: new Date(createdBefore).toISOString() } : {}),
      ...(minDuration ? { min_duration_seconds: Number(minDuration) } : {}),
      ...(maxDuration ? { max_duration_seconds: Number(maxDuration) } : {}),
    });
  }

  return (
    <div className="page-flow">
      <header className="page-heading"><div><p className="kicker">Durable invocation</p><h1>Runs</h1><p>Preflight exact inputs, confirm once, then follow the append-only timeline.</p></div></header>
      <section className="run-grid">
        <div className="section-block playground">
          <div className="section-heading"><div><span className="index">Playground / 01</span><h2>Prepare a run</h2></div></div>
          <form className="stacked-form" onSubmit={submitPreflight} noValidate>
            <label>Enabled capability<select name="capability" required value={selectedVersionId} disabled={prepare.isPending || Boolean(preflight)} onChange={(event) => { invalidatePreparedRun(); setDraft((current) => ({ ...current, selectedVersionId: event.target.value })); }}><option value="" disabled>Select a capability</option>{enabledCapabilities.map((item) => <option key={item.id} value={item.enabled_version_id ?? ""}>{item.tool_identity} — {connectionNames.get(item.connection_id) ?? shortId(item.connection_id)}</option>)}</select></label>
            <label>Arguments<textarea value={argumentsText} disabled={prepare.isPending || Boolean(preflight)} onChange={(event) => { invalidatePreparedRun(); setDraft((current) => ({ ...current, argumentsText: event.target.value })); }} rows={8} spellCheck={false} aria-invalid={Boolean(argumentsError)} /></label>
            <p className="incident-note">Only public, synthetic, or explicitly non-confidential data.</p>
            {argumentsError && <p className="field-error" role="alert">{argumentsError}</p>}
            {prepare.isError && <p className="field-error" role="alert">{failureMessage(prepare.error)}</p>}
            {capabilities.isError && <QueryFailure error={capabilities.error} retry={() => void capabilities.refetch()} />}
            {connections.isError && <QueryFailure error={connections.error} retry={() => void connections.refetch()} />}
            <button className="primary-action" disabled={!canOperate(role) || prepare.isPending || !selectedCapability} type="submit">{prepare.isPending ? "Checking…" : "Review invocation"}</button>
          </form>
          {!capabilities.isError && enabledCapabilities.length === 0 && <p className="field-help">Enable a capability version before opening a run.</p>}
        </div>
        <div className="section-block run-inventory">
          <div className="section-heading"><div><span className="index">Ledger / 02</span><h2>Recent runs</h2></div></div>
          <form className="audit-filters" key={runFilterFormKey} onSubmit={submitRunFilters}>
            <label>Status<select name="run-status" defaultValue={runFilters.status ?? ""}><option value="">Any</option>{["queued", "preparing", "session_fenced", "dispatch_fenced", "succeeded", "failed", "cancelled", "timed_out", "indeterminate"].map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}</select></label>
            <label>Capability<select name="run-capability" defaultValue={runFilters.capability_id ?? ""}><option value="">Any</option>{(capabilities.data ?? []).map((item) => <option key={item.id} value={item.id}>{item.tool_identity}</option>)}</select></label>
            <label>Actor ID<input name="run-actor" defaultValue={runFilters.actor_id ?? ""} placeholder="UUID" pattern="[0-9a-fA-F-]{36}" /></label>
            <label>Created after<input name="run-created-after" type="datetime-local" defaultValue={localDateTimeValue(runFilters.created_after)} /></label>
            <label>Created before<input name="run-created-before" type="datetime-local" defaultValue={localDateTimeValue(runFilters.created_before)} /></label>
            <label>Min duration (s)<input name="run-min-duration" type="number" min="0" defaultValue={runFilters.min_duration_seconds} /></label>
            <label>Max duration (s)<input name="run-max-duration" type="number" min="0" defaultValue={runFilters.max_duration_seconds} /></label>
            <div className="action-strip"><button type="button" onClick={() => { setRunFilters({}); setRunFilterFormKey((value) => value + 1); }}>Clear</button><button className="secondary-action" type="submit">Apply filters</button></div>
          </form>
          {(hasRunFilters ? history.isPending : runs.isPending) ? <LoadingState label="Loading runs" /> : (hasRunFilters ? history.isError : runs.isError) ? <QueryFailure error={hasRunFilters ? history.error : runs.error} retry={() => void (hasRunFilters ? history.refetch() : runs.refetch())} /> : orderedRuns.length === 0 ? <EmptyState title="No runs retained" copy="Completed and in-flight work will appear here." /> : (
            <ul className="select-list">{orderedRuns.map((run) => <li key={run.id}><button className={selectedId === run.id ? "selected" : ""} type="button" onClick={() => select(run.id)}><span><strong>{capabilityNames.get(run.capability_id) ?? shortId(run.capability_id)}</strong><small>{connectionNames.get(run.connection_id) ?? shortId(run.connection_id)} · actor {shortId(run.actor_user_id)} · {shortId(run.id)} · {formatTime(run.created_at)}</small></span><StatusMark value={run.status} /></button></li>)}</ul>
          )}
          {!hasRunFilters && history.isError && <QueryFailure error={history.error} retry={() => void history.refetch()} />}
          {history.hasNextPage && <button className="secondary-action" disabled={history.isFetchingNextPage} type="button" onClick={() => void history.fetchNextPage()}>{history.isFetchingNextPage ? "Loading…" : "Load older runs"}</button>}
        </div>
      </section>
      {preflight && pendingArguments && (
        <section className="confirmation-panel" aria-labelledby="confirmation-title">
          <div><span className="index">One-time confirmation</span><h2 id="confirmation-title">Confirm exact invocation</h2><p>This token expires {formatTime(preflight.expires_at)} and cannot move to another request.</p></div>
          <p className="incident-note">Only public, synthetic, or explicitly non-confidential data.</p>
          <dl><div><dt>Capability version</dt><dd><code>{shortId(preflight.capability_version_id)}</code></dd></div><div><dt>Pinned endpoint</dt><dd><code>{confirmedEndpoint ?? (confirmationConnection.isPending ? "Resolving…" : "Unavailable")}</code></dd></div><div><dt>Argument digest</dt><dd><code>{shortId(preflight.argument_digest)}</code></dd></div></dl>
          {confirmationConnection.isError && <QueryFailure error={confirmationConnection.error} retry={() => void confirmationConnection.refetch()} />}
          {!confirmationConnection.isPending && !confirmationConnection.isError && !confirmedEndpoint && <p className="field-error" role="alert">The pinned connection version is unavailable. Run preflight again.</p>}
          {confirmationExpired && <p className="field-error" role="alert">This confirmation expired. Go back and run preflight again.</p>}
          <details open><summary>Exact prepared arguments</summary><pre>{JSON.stringify(pendingArguments, null, 2)}</pre></details>
          {invoke.isError && <p className="field-error" role="alert">{failureMessage(invoke.error)}{invoke.error instanceof ApiFailure && ["invalid_confirmation", "confirmation_expired", "confirmation_replayed"].includes(invoke.error.code) ? " Run preflight again." : " Retry to safely reuse this request."}</p>}
          <div className="action-strip"><button type="button" onClick={abandonPreparedRun}>Back</button><button className="primary-action" type="button" disabled={!canOperate(role) || invoke.isPending || !confirmedEndpoint || confirmationExpired} onClick={() => invoke.mutate({ prepared: preflight, args: pendingArguments })}>{invoke.isPending ? "Submitting…" : confirmationExpired ? "Confirmation expired" : "Confirm and run"}</button></div>
        </section>
      )}
      {invoke.isError && !preflight && <p className="field-error" role="alert">{failureMessage(invoke.error)} Run preflight again.</p>}
      <section className="detail-panel run-detail">
        {selectedId === null ? <EmptyState title="Select a run" copy="Inspect lineage, safe output, and status events." /> : runDetail.isPending ? <LoadingState label="Loading run detail" /> : runDetail.isError ? <QueryFailure error={runDetail.error} retry={() => void runDetail.refetch()} /> : (
          <>
            <div className="detail-title"><div><span className="index">Run {shortId(runDetail.data.id)}</span><h2>Execution timeline</h2></div><StatusMark value={runDetail.data.status} /></div>
            <dl className="detail-facts"><div><dt>Capability</dt><dd>{capabilityNames.get(runDetail.data.capability_id) ?? shortId(runDetail.data.capability_id)} · {shortId(runDetail.data.capability_version_id)}</dd></div><div><dt>Source connection</dt><dd>{connectionNames.get(runDetail.data.connection_id) ?? shortId(runDetail.data.connection_id)}</dd></div><div><dt>Initiating actor</dt><dd>{shortId(runDetail.data.actor_user_id)}</dd></div><div><dt>Connection version</dt><dd>{shortId(runDetail.data.connection_version_id)}</dd></div><div><dt>Deadline</dt><dd>{formatTime(runDetail.data.deadline)}</dd></div><div><dt>Updated</dt><dd>{formatTime(runDetail.data.updated_at)}</dd></div></dl>
            {runDetail.data.cancellation_requested && !terminal(runDetail.data.status) && <p className="incident-note">Cancellation requested; waiting for the worker to reach a safe boundary.</p>}
            {canOperate(role) && !terminal(runDetail.data.status) && !runDetail.data.cancellation_requested && <button className="danger-action" disabled={cancel.isPending} type="button" onClick={() => { const key = cancelKeys.current.get(runDetail.data.id) ?? mutationId(); cancelKeys.current.set(runDetail.data.id, key); cancel.mutate({ id: runDetail.data.id, key }); }}>Request cancellation</button>}
            {cancel.isError && <p className="field-error" role="alert">{failureMessage(cancel.error)}</p>}
            {events.isPending ? <LoadingState label="Loading run timeline" /> : events.isError ? <QueryFailure error={events.error} retry={() => void events.refetch()} /> : <ol className="timeline">{events.data.map((event) => <li key={event.id}><span aria-hidden="true" /><div><strong>{event.event_type.replaceAll("_", " ")}</strong><small>{formatTime(event.occurred_at)} · {event.status}</small>{event.safe_error_code && <code>{event.safe_error_code}</code>}</div></li>)}</ol>}
            <details><summary>Retained arguments</summary><pre>{JSON.stringify(runDetail.data.arguments, null, 2)}</pre></details>
            {runDetail.data.result && <details open><summary>Validated result</summary><pre>{JSON.stringify(runDetail.data.result, null, 2)}</pre></details>}
            {runDetail.data.safe_error_code && <p className="incident-note">Run ended with {runDetail.data.safe_error_code}.</p>}
            {runDetail.data.status === "indeterminate" && <p className="field-error" role="alert">Do not retry this invocation. The upstream tool may have completed it; reconcile the side effect before taking further action.</p>}
            {runDetail.data.status === "failed" && ["worker_lost_before_dispatch", "preparation_failed", "session_initialization_failed"].includes(runDetail.data.safe_error_code ?? "") && <p className="incident-note">No tool call was dispatched. Correct the reported condition before starting a new run.</p>}
            {runDetail.data.status === "failed" && !["worker_lost_before_dispatch", "preparation_failed", "session_initialization_failed"].includes(runDetail.data.safe_error_code ?? "") && <p className="incident-note">Review the upstream outcome before starting another run; this failure is not classified as pre-dispatch.</p>}
          </>
        )}
      </section>
    </div>
  );
}

function Audit({ api, scope }: { api: ControlPlane; scope: QueryScope }) {
  const [draft, setDraft] = useState<AuditFilters>({});
  const [filters, setFilters] = useState<AuditFilters>({});
  const events = useInfiniteQuery({
    queryKey: queryKey(scope, "audit-events", filters),
    queryFn: ({ pageParam }) => api.listAuditEvents(filters, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.nextCursor,
  });
  const rows = events.data?.pages.flatMap((page) => page.items) ?? [];
  return <div className="page-flow">
    <header className="page-heading"><div><p className="kicker">Workspace accountability</p><h1>Audit ledger</h1><p>Payload-free mutation history, actors, outcomes, and correlation lineage.</p></div></header>
    <section className="section-block">
      <div className="section-heading"><div><span className="index">Append-only history / 01</span><h2>Recorded events</h2></div><button className="secondary-action" type="button" disabled={events.isFetching} onClick={() => void events.refetch()}>{events.isFetching ? "Refreshing…" : "Refresh ledger"}</button></div>
      <form className="audit-filters" onSubmit={(event) => { event.preventDefault(); setFilters(draft); }}>
        <label>Resource type<select value={draft.resource_type ?? ""} onChange={(event) => setDraft({ ...draft, resource_type: event.target.value as AuditFilters["resource_type"] || undefined })}><option value="">Any</option>{["workspace", "membership", "secret_binding", "server_connection", "capability", "registry_entry", "run"].map((value) => <option key={value} value={value}>{value.replaceAll("_", " ")}</option>)}</select></label>
        <label>Resource ID<input value={draft.resource_id ?? ""} onChange={(event) => setDraft({ ...draft, resource_id: event.target.value || undefined })} placeholder="UUID" /></label>
        <label>Actor ID<input value={draft.actor_id ?? ""} onChange={(event) => setDraft({ ...draft, actor_id: event.target.value || undefined })} placeholder="UUID" /></label>
        <label>Action<select value={draft.action ?? ""} onChange={(event) => setDraft({ ...draft, action: event.target.value as AuditFilters["action"] || undefined })}><option value="">Any</option>{["workspace.created", "membership.changed", "secret_binding.created", "connection.created", "connection.version_appended", "connection.verified", "connection.disabled", "connection.enabled", "capability.version_recorded", "capability.enabled", "capability.disabled", "registry_entry.imported", "run.created", "run.cancellation_requested", "run.cancelled"].map((value) => <option key={value} value={value}>{value.replaceAll(".", " ")}</option>)}</select></label>
        <label>Outcome<select value={draft.outcome ?? ""} onChange={(event) => setDraft({ ...draft, outcome: event.target.value as AuditFilters["outcome"] || undefined })}><option value="">Any</option><option value="succeeded">Succeeded</option><option value="denied">Denied</option><option value="failed">Failed</option></select></label>
        <label>From<input type="datetime-local" value={localDateTimeValue(draft.occurred_after)} onChange={(event) => setDraft({ ...draft, occurred_after: event.target.value ? new Date(event.target.value).toISOString() : undefined })} /></label>
        <label>Before<input type="datetime-local" value={localDateTimeValue(draft.occurred_before)} onChange={(event) => setDraft({ ...draft, occurred_before: event.target.value ? new Date(event.target.value).toISOString() : undefined })} /></label>
        <div className="action-strip"><button type="button" onClick={() => { setDraft({}); setFilters({}); }}>Clear</button><button className="secondary-action" type="submit">Apply filters</button></div>
      </form>
      {events.isPending ? <LoadingState label="Loading audit events" /> : events.isError ? <QueryFailure error={events.error} retry={() => void events.refetch()} /> : rows.length === 0 ? <EmptyState title="No audit events" copy="Authorized mutations will appear here." /> : <><ol className="timeline">{rows.map((event) => <li key={event.id}><span aria-hidden="true" /><div><strong>{event.action.replaceAll(".", " ")}</strong><small>{formatTime(event.occurred_at)} · {event.outcome}</small><code>{event.resource_type} / {shortId(event.resource_id)}</code><small>Actor {shortId(event.actor_user_id)} · Correlation {shortId(event.correlation_id)}</small></div></li>)}</ol>{events.hasNextPage && <button className="secondary-action" disabled={events.isFetchingNextPage} type="button" onClick={() => void events.fetchNextPage()}>{events.isFetchingNextPage ? "Loading…" : "Load older events"}</button>}</>}
    </section>
  </div>;
}

function WorkspaceApp({ session, api, onLogout }: { session: WorkspaceSession; api: ControlPlane; onLogout: () => void }) {
  const scope: QueryScope = ["workspace", session.identityId, session.workspaceId];
  const registryMutationKeys = useRef(new Map<string, string>());
  const capabilityMutationKeys = useRef(new Map<string, string>());
  const runKeys = useRef(new Map<string, string>());
  const cancelKeys = useRef(new Map<string, string>());
  const [runDraft, setRunDraft] = useState<RunDraft>({ selectedVersionId: "", argumentsText: "{\n  \"query\": \"status\"\n}", pendingArguments: null, preflight: null });
  const access = useQuery({
    queryKey: queryKey(scope, "session"),
    queryFn: () => api.currentSession(),
    refetchInterval: (query) => query.state.status === "error" ? false : 5000,
  });
  const [route, setRoute] = useState<Route>(() => readRoute());
  useEffect(() => {
    const handlePopState = () => setRoute(readRoute());
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => document.getElementById("main-content")?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [access.status, route]);
  function navigate(view: View, selectedId: string | null = null, capabilityFilter: CapabilityFilter = "all") {
    window.history.pushState({}, "", view === "capabilities" ? capabilityPath(capabilityFilter, selectedId) : routePath(view, selectedId));
    setRoute({ view, selectedId });
  }
  if (access.isPending) return <main className="auth-layout" id="main-content" tabIndex={-1}><LoadingState label="Checking workspace access" /></main>;
  if (access.isError) return <main className="auth-layout" id="main-content" tabIndex={-1}><QueryFailure error={access.error} retry={() => void access.refetch()} /><button className="text-action" type="button" onClick={onLogout}>Log out</button></main>;
  const role = access.data.role;
  const view: View = role === "viewer" && route.view === "audit" ? "overview" : route.view;
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <header className="topbar">
        <button className="brand" type="button" onClick={() => navigate("overview")} aria-label="Modall overview"><span>m</span><strong>modall</strong></button>
        <nav aria-label="Primary navigation">{navItems.filter((item) => item.id !== "audit" || canOperate(role)).map((item) => <button key={item.id} className={view === item.id ? "active" : ""} type="button" onClick={() => navigate(item.id)} aria-current={view === item.id ? "page" : undefined}><span>{item.marker}</span>{item.label}</button>)}</nav>
        <div className="workspace-menu"><div><span>{session.workspaceLabel} · {role}</span><code>{shortId(session.workspaceId)}</code></div><button className="text-action" type="button" onClick={onLogout}>Log out</button></div>
      </header>
      <main id="main-content" tabIndex={-1}>
        {view === "overview" && <Overview api={api} open={(next, filter) => navigate(next, null, filter)} scope={scope} />}
        {view === "registry" && <Registry api={api} scope={scope} role={role} selectedId={route.selectedId} select={(id) => navigate("registry", id)} mutationKeys={registryMutationKeys} />}
        {view === "capabilities" && <Capabilities api={api} scope={scope} role={role} selectedId={route.selectedId} filter={capabilityFilterFromLocation()} select={(id) => navigate("capabilities", id, capabilityFilterFromLocation())} setFilter={(filter) => navigate("capabilities", null, filter)} mutationKeys={capabilityMutationKeys} />}
        {view === "runs" && <Runs api={api} scope={scope} role={role} selectedId={route.selectedId} select={(id) => navigate("runs", id)} runKeys={runKeys} cancelKeys={cancelKeys} draft={runDraft} setDraft={setRunDraft} />}
        {view === "audit" && canOperate(role) && <Audit api={api} scope={scope} />}
      </main>
      <footer><span>Modall Registry Alpha</span><span>Exact lineage · bounded retention · fail closed</span></footer>
    </div>
  );
}

export function App({ apiFactory = createControlPlane }: { apiFactory?: ApiFactory }) {
  const queryClient = useQueryClient();
  const [session, setSession] = useState<WorkspaceSession | null>(() => loadSession());
  const api = useMemo(() => (session ? apiFactory(session) : null), [apiFactory, session]);

  async function logout() {
    if (session) {
      await queryClient.cancelQueries({ queryKey: ["workspace", session.identityId, session.workspaceId] });
    }
    queryClient.clear();
    clearSession();
    setSession(null);
  }

  return session && api ? (
    <WorkspaceApp session={session} api={api} onLogout={() => void logout()} />
  ) : (
    <SignIn onSignIn={setSession} />
  );
}

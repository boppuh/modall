/* eslint-disable @typescript-eslint/unbound-method */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { ApiFailure, type Capability, type CapabilityStatus, type Connection, type ControlPlane, type Run } from "./api/operations";
import { saveSession, type WorkspaceSession } from "./session";

const workspaceId = "11111111-1111-4111-8111-111111111111";
const connectionId = "22222222-2222-4222-8222-222222222222";
const capabilityId = "33333333-3333-4333-8333-333333333333";
const versionId = "44444444-4444-4444-8444-444444444444";
const runId = "55555555-5555-4555-8555-555555555555";
const timestamp = "2026-09-06T12:00:00Z";

const connection: Connection = {
  id: connectionId,
  name: "Internal developer tools",
  lifecycle: "active",
  pending_version_id: null,
  verified_version_id: versionId,
  control_epoch: 2,
  refresh_generation: 4,
  last_refresh_at: timestamp,
  last_refresh_error_code: null,
  created_at: timestamp,
};

const capability: Capability = {
  id: capabilityId,
  connection_id: connectionId,
  tool_identity: "tools/search",
  pending_version_id: null,
  enabled_version_id: versionId,
  status: "enabled",
  status_epoch: 3,
  created_at: timestamp,
};

const run: Run = {
  id: runId,
  actor_user_id: connectionId,
  capability_id: capabilityId,
  capability_version_id: versionId,
  connection_id: connectionId,
  connection_version_id: versionId,
  status: "running",
  arguments: { query: "status" },
  arguments_expires_at: new Date(Date.now() + 60_000).toISOString(),
  result: null,
  result_expires_at: null,
  server_observed_at: timestamp,
  safe_error_code: null,
  cancellation_requested: false,
  deadline: "2026-09-06T12:05:00Z",
  created_at: timestamp,
  updated_at: timestamp,
  terminal_at: null,
};

function fakeApi(overrides: Partial<ControlPlane> = {}): ControlPlane {
  const api: ControlPlane = {
    currentSession: vi.fn().mockResolvedValue({ workspace_id: workspaceId, actor_user_id: connectionId, role: "admin" }),
    overview: vi.fn().mockResolvedValue({ connections: [connection], capabilities: [capability], runs: [run] }),
    listConnections: vi.fn().mockResolvedValue([connection]),
    listConnectionPage: vi.fn().mockResolvedValue({ items: [connection] }),
    getConnection: vi.fn().mockResolvedValue({ ...connection, versions: [{ id: versionId, sequence: 1, endpoint_url: "https://mcp.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http", created_at: timestamp }], versions_truncated: false }),
    createConnection: vi.fn().mockResolvedValue(connection),
    appendConnectionVersion: vi.fn().mockResolvedValue({ id: versionId, sequence: 2, endpoint_url: "https://mcp.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http", created_at: timestamp }),
    connectionAction: vi.fn().mockResolvedValue(undefined),
    searchRegistry: vi.fn().mockImplementation(() => Promise.resolve({ cache_id: connectionId, fetched_at: timestamp, expires_at: new Date(Date.now() + 60_000).toISOString(), server_observed_at: new Date().toISOString(), from_cache: false, items: [{ external_id: "io.modall/search", source_version: "1.2.0", name: "Public search", description: "Search public records", advertised_urls: ["https://mcp.example/tools"], provenance_digest: "a".repeat(64) }] })),
    importRegistry: vi.fn().mockResolvedValue({ id: connectionId, source: "official", external_id: "io.modall/search", current_version_id: versionId, name: "Public search", description: "Search public records", created_at: timestamp }),
    listRegistryEntries: vi.fn().mockResolvedValue([]),
    listCapabilities: vi.fn().mockResolvedValue([capability]),
    listCapabilityPage: vi.fn().mockResolvedValue({ items: [capability] }),
    getCapability: vi.fn().mockResolvedValue({ ...capability, versions: [{ id: versionId, capability_id: capabilityId, connection_version_id: versionId, sequence: 1, display_name: "Search", description: "Search public records", input_schema: { type: "object" }, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp }], versions_truncated: false, observed_in_current_snapshot: true }),
    capabilityAction: vi.fn().mockResolvedValue(capability),
    listRuns: vi.fn().mockResolvedValue([run]),
    listRunPage: vi.fn().mockResolvedValue({ items: [run] }),
    getRun: vi.fn().mockResolvedValue(run),
    listRunEvents: vi.fn().mockResolvedValue([{ id: connectionId, sequence: 1, event_type: "admitted", status: "queued", safe_error_code: null, occurred_at: timestamp }]),
    preflight: vi.fn().mockImplementation(() => Promise.resolve({ capability_version_id: versionId, connection_version_id: versionId, argument_digest: "c".repeat(64), confirmation_token: "confirmation", expires_at: new Date(Date.now() + 60_000).toISOString(), server_observed_at: new Date().toISOString() })),
    createRun: vi.fn().mockResolvedValue(run),
    cancelRun: vi.fn().mockResolvedValue({ ...run, status: "cancelled" }),
    listAuditEvents: vi.fn().mockResolvedValue({ items: [] }),
    ...overrides,
  };
  const overriddenListCapabilities = overrides.listCapabilities;
  const overriddenListConnections = overrides.listConnections;
  if (overriddenListConnections && !overrides.listConnectionPage) {
    api.listConnectionPage = vi.fn(async () => ({ items: await overriddenListConnections() }));
  }
  if (overriddenListCapabilities && !overrides.listCapabilityPage) {
    api.listCapabilityPage = vi.fn(async (status: CapabilityStatus | undefined) => ({ items: await overriddenListCapabilities(status) }));
  }
  return api;
}

function renderApp(api: ControlPlane, authenticated = true, role: "admin" | "operator" | "viewer" = "admin") {
  if (authenticated) {
    const session: WorkspaceSession = {
      identityId: "pilot-reviewer",
      workspaceId,
      workspaceLabel: "iOS pilot",
    };
    saveSession(session);
  }
  vi.mocked(api.currentSession).mockResolvedValue({ workspace_id: workspaceId, actor_user_id: connectionId, role });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <App apiFactory={() => api} />
    </QueryClientProvider>,
  );
}

describe("App", () => {
  beforeEach(() => {
    localStorage.clear();
    window.history.replaceState({}, "", "/");
    vi.restoreAllMocks();
  });

  afterEach(() => { vi.useRealTimers(); cleanup(); });

  it("validates, saves, and clears the workspace session", async () => {
    const api = fakeApi({ overview: vi.fn().mockResolvedValue({ connections: [], capabilities: [], runs: [] }) });
    renderApp(api, false);

    expect(screen.getByRole("heading", { name: "Know what can run before it runs." })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /enter control plane/i }));
    expect(screen.getByRole("alert").textContent).toContain("workspace UUID");

    fireEvent.change(screen.getByLabelText("Workspace label"), { target: { value: "iOS pilot" } });
    fireEvent.change(screen.getByLabelText("Workspace UUID"), { target: { value: workspaceId } });
    fireEvent.change(screen.getByLabelText(/OIDC access token/), { target: { value: "token" } });
    fireEvent.change(screen.getByLabelText(/OIDC access token/), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: /enter control plane/i }));
    expect(await screen.findByRole("heading", { name: "Registry overview" })).toBeTruthy();
    expect(localStorage.length).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "Log out" }));
    expect(await screen.findByRole("heading", { name: "Open a workspace" })).toBeTruthy();
    expect(localStorage.length).toBe(0);
  });

  it("shows workspace posture and navigates through registry lifecycle controls", async () => {
    const pendingConnection = { ...connection, pending_version_id: versionId, verified_version_id: null };
    const api = fakeApi({
      getConnection: vi.fn().mockResolvedValue({ ...pendingConnection, versions: [{ id: versionId, sequence: 1, endpoint_url: "https://mcp.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http", created_at: timestamp }], versions_truncated: false }),
    });
    renderApp(api);
    expect(await screen.findByText("Internal developer tools")).toBeTruthy();
    await waitFor(() => expect(document.activeElement?.id).toBe("main-content"));
    expect(screen.getAllByText("01").length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole("button", { name: /Active connections/ }));
    expect(await screen.findByRole("heading", { name: "Server registry" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Modall overview" }));
    expect(await screen.findByRole("heading", { name: "Registry overview" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Enabled capabilities/ }));
    expect(await screen.findByRole("heading", { name: "Capabilities" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Modall overview" }));
    expect(await screen.findByRole("heading", { name: "Registry overview" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Runs in flight/ }));
    expect(await screen.findByRole("heading", { name: "Runs" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Modall overview" }));
    expect(await screen.findByRole("heading", { name: "Registry overview" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open review queue" }));
    expect(await screen.findByRole("heading", { name: "Capabilities" })).toBeTruthy();
    expect(window.location.search).toBe("?status=pending_review");
    await waitFor(() => expect(api.listCapabilityPage).toHaveBeenCalledWith("pending_review", undefined));
    fireEvent.change(screen.getByLabelText("Capability status"), { target: { value: "all" } });
    await waitFor(() => expect(window.location.search).toBe(""));
    await waitFor(() => expect(api.listCapabilityPage).toHaveBeenCalledWith(undefined, undefined));
    fireEvent.click(screen.getByRole("button", { name: "Modall overview" }));
    expect(await screen.findByRole("heading", { name: "Registry overview" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /manage registry/i }));
    expect(await screen.findByRole("heading", { name: "Server registry" })).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Registry search"), { target: { value: "search" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Public search")).toBeTruthy();
    expect(screen.getByText("io.modall/search @ 1.2.0")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(api.importRegistry).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Internal developer tools" } });
    fireEvent.change(screen.getByLabelText("HTTPS endpoint"), { target: { value: "https://mcp.example/tools" } });
    fireEvent.click(screen.getByRole("button", { name: "Add connection" }));
    await waitFor(() => expect(api.createConnection).toHaveBeenCalled());

    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    expect((await screen.findAllByText("https://mcp.example/tools")).length).toBeGreaterThan(0);
    expect(screen.getByText(/policy v1 · binding none/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Verify pending" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    fireEvent.click(screen.getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(api.connectionAction).toHaveBeenCalledTimes(3));
  });

  it("loads older capabilities only when an operator requests them", async () => {
    const olderCapability = { ...capability, id: "66666666-6666-4666-8666-666666666666", tool_identity: "tools/archive" };
    const listCapabilityPage = vi.fn<ControlPlane["listCapabilityPage"]>()
      .mockResolvedValueOnce({ items: [capability], nextCursor: "older" })
      .mockResolvedValueOnce({ items: [olderCapability] });
    renderApp(fakeApi({ listCapabilityPage }));

    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    expect(await screen.findByRole("button", { name: /tools\/search/ })).toBeTruthy();
    expect(listCapabilityPage).toHaveBeenCalledWith(undefined, undefined);
    expect(screen.queryByRole("button", { name: /tools\/archive/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Load older tools" }));
    expect(await screen.findByRole("button", { name: /tools\/archive/ })).toBeTruthy();
    expect(listCapabilityPage).toHaveBeenCalledWith(undefined, "older");
  });

  it("loads older connections only when an operator requests them", async () => {
    const olderConnection = { ...connection, id: "66666666-6666-4666-8666-666666666666", name: "Release tooling" };
    const listConnectionPage = vi.fn<ControlPlane["listConnectionPage"]>()
      .mockResolvedValueOnce({ items: [connection], nextCursor: "older" })
      .mockResolvedValueOnce({ items: [connection], nextCursor: "older" })
      .mockResolvedValueOnce({ items: [olderConnection] });
    renderApp(fakeApi({ listConnectionPage }));

    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    expect(await screen.findByRole("button", { name: /Internal developer tools/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Release tooling/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Refresh connections" }));
    await waitFor(() => expect(listConnectionPage.mock.calls.filter(([cursor]) => cursor === undefined)).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "Load older connections" }));
    expect(await screen.findByRole("button", { name: /Release tooling/ })).toBeTruthy();
    expect(listConnectionPage).toHaveBeenCalledWith("older");
  });

  it("refreshes selected connection history explicitly instead of polling it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const getConnection = vi.fn<ControlPlane["getConnection"]>().mockResolvedValue({ ...connection, versions: [{ id: versionId, sequence: 1, endpoint_url: "https://mcp.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http", created_at: timestamp }], versions_truncated: false });
    renderApp(fakeApi({ getConnection }));
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    await waitFor(() => expect(getConnection).toHaveBeenCalledOnce());
    await vi.advanceTimersByTimeAsync(2_100);
    expect(getConnection).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Refresh connections" }));
    await waitFor(() => expect(getConnection).toHaveBeenCalledTimes(2));
  });

  it("does not redirect after connection creation finishes from an abandoned Registry view", async () => {
    let resolveConnection: ((value: Connection) => void) | undefined;
    const createConnection = vi.fn<ControlPlane["createConnection"]>().mockImplementation(() => new Promise<Connection>((resolve) => { resolveConnection = resolve; }));
    const api = fakeApi({ createConnection });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Internal developer tools" } });
    fireEvent.change(screen.getByLabelText("HTTPS endpoint"), { target: { value: "https://mcp.example/tools" } });
    fireEvent.click(screen.getByRole("button", { name: "Add connection" }));
    await waitFor(() => expect(createConnection).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("button", { name: "Modall overview" }));
    expect(await screen.findByRole("heading", { name: "Registry overview" })).toBeTruthy();
    await act(async () => {
      resolveConnection?.(connection);
      await Promise.resolve();
    });
    expect(window.location.pathname).toBe("/");
  });

  it("reviews an immutable capability version and changes its approval", async () => {
    const api = fakeApi();
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    expect(await screen.findByRole("heading", { name: "Capabilities" })).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    expect(await screen.findByText("Search public records")).toBeTruthy();
    expect(screen.getByText(/"type": "object"/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Disable version" }));
    await waitFor(() => expect(api.capabilityAction).toHaveBeenCalledWith(versionId, "disable", expect.any(String)));
  });

  it("clears a failed capability action when another capability is selected", async () => {
    const otherCapability = { ...capability, id: "66666666-6666-4666-8666-666666666666", tool_identity: "tools/archive" };
    const api = fakeApi({
      listCapabilityPage: vi.fn().mockResolvedValue({ items: [capability, otherCapability] }),
      getCapability: vi.fn<ControlPlane["getCapability"]>().mockImplementation((id) => Promise.resolve({ ...(id === capabilityId ? capability : otherCapability), versions: [{ id: versionId, capability_id: id, connection_version_id: versionId, sequence: 1, display_name: "Tool", description: null, input_schema: {}, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp }], versions_truncated: false, observed_in_current_snapshot: true })),
      capabilityAction: vi.fn().mockRejectedValue(new Error("decision failed")),
    });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Disable version" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /tools\/archive/ }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("identifies capability sources and re-enables the retained version", async () => {
    const disabled = { ...capability, pending_version_id: null, status: "disabled" as const };
    const api = fakeApi({
      listCapabilities: vi.fn().mockResolvedValue([disabled]),
      getCapability: vi.fn().mockResolvedValue({ ...disabled, versions: [{ id: versionId, capability_id: capabilityId, connection_version_id: versionId, sequence: 1, display_name: "Search", description: null, input_schema: {}, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp }], versions_truncated: false }),
    });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    expect(await screen.findByText(/Source connection: Internal developer tools/)).toBeTruthy();
    const reenable = screen.getByRole("button", { name: "Re-enable version" });
    await waitFor(() => expect(reenable).toHaveProperty("disabled", false));
    fireEvent.click(reenable);
    await waitFor(() => expect(api.capabilityAction).toHaveBeenCalledWith(versionId, "enable", expect.any(String)));
  });

  it("prefers a pending version over an older retained capability version", async () => {
    const pendingVersionId = "66666666-6666-4666-8666-666666666666";
    const disabled = { ...capability, status: "pending_review" as const, pending_version_id: pendingVersionId, enabled_version_id: versionId };
    const api = fakeApi({
      listCapabilities: vi.fn().mockResolvedValue([disabled]),
      getCapability: vi.fn().mockResolvedValue({ ...disabled, versions: [
        { id: pendingVersionId, capability_id: capabilityId, connection_version_id: versionId, sequence: 2, display_name: "New search", description: null, input_schema: {}, output_schema: null, metadata_digest: "d".repeat(64), schema_supported: true, created_at: timestamp },
        { id: versionId, capability_id: capabilityId, connection_version_id: versionId, sequence: 1, display_name: "Old search", description: null, input_schema: {}, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp },
      ], versions_truncated: false }),
    });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    expect(await screen.findByRole("button", { name: "Historical version" })).toHaveProperty("disabled", true);
    expect(screen.getAllByText(versionId)).toHaveLength(2);
    const enable = screen.getByRole("button", { name: "Enable exact version" });
    await waitFor(() => expect(enable).toHaveProperty("disabled", false));
    fireEvent.click(enable);
    await waitFor(() => expect(api.capabilityAction).toHaveBeenCalledWith(pendingVersionId, "enable", expect.any(String)));
  });

  it("allows an operator to reject an unsupported pending capability", async () => {
    const pending = { ...capability, status: "pending_review" as const, pending_version_id: versionId, enabled_version_id: null };
    const api = fakeApi({
      listCapabilities: vi.fn().mockResolvedValue([pending]),
      getCapability: vi.fn().mockResolvedValue({ ...pending, versions: [{ id: versionId, capability_id: capabilityId, connection_version_id: connectionId, sequence: 1, display_name: "Unsafe search", description: null, input_schema: {}, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: false, created_at: timestamp }], versions_truncated: false }),
    });
    renderApp(api, true, "operator");
    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    expect(await screen.findByRole("button", { name: "Enable exact version" })).toHaveProperty("disabled", true);
    fireEvent.click(screen.getByRole("button", { name: "Reject version" }));
    await waitFor(() => expect(api.capabilityAction).toHaveBeenCalledWith(versionId, "disable", expect.any(String)));
  });

  it("offers only reconsideration after a pending version is rejected", async () => {
    const rejected = { ...capability, status: "disabled" as const, pending_version_id: versionId, enabled_version_id: null };
    const api = fakeApi({
      listCapabilities: vi.fn().mockResolvedValue([rejected]),
      getCapability: vi.fn().mockResolvedValue({ ...rejected, versions: [{ id: versionId, capability_id: capabilityId, connection_version_id: connectionId, sequence: 1, display_name: "Rejected search", description: null, input_schema: {}, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp }], versions_truncated: false }),
    });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    expect(await screen.findByRole("button", { name: "Reconsider version" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Reject version" })).toBeNull();
  });

  it("allows an unavailable retained capability to be disabled", async () => {
    const unavailable = { ...capability, status: "unavailable" as const };
    const api = fakeApi({
      listCapabilities: vi.fn().mockResolvedValue([unavailable]),
      getCapability: vi.fn().mockResolvedValue({ ...unavailable, versions: [{ id: versionId, capability_id: capabilityId, connection_version_id: connectionId, sequence: 1, display_name: "Unavailable search", description: null, input_schema: {}, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp }], versions_truncated: false }),
    });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Disable version" }));
    await waitFor(() => expect(api.capabilityAction).toHaveBeenCalledWith(versionId, "disable", expect.any(String)));
  });

  it("keeps unavailable pending capability versions rejectable but not enableable", async () => {
    const unavailable = { ...capability, status: "unavailable" as const, pending_version_id: versionId, enabled_version_id: null };
    renderApp(fakeApi({
      listCapabilities: vi.fn().mockResolvedValue([unavailable]),
      getCapability: vi.fn().mockResolvedValue({ ...unavailable, versions: [{ id: versionId, capability_id: capabilityId, connection_version_id: connectionId, sequence: 1, display_name: "Unavailable search", description: null, input_schema: {}, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp }], versions_truncated: false }),
    }));
    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    expect(await screen.findByRole("button", { name: "Reject version" })).toHaveProperty("disabled", false);
    expect(screen.getByRole("button", { name: "Enable exact version" })).toHaveProperty("disabled", true);
  });

  it("blocks reconsidering a disabled version absent from the current snapshot", async () => {
    const rejected = { ...capability, status: "disabled" as const, pending_version_id: versionId, enabled_version_id: null };
    renderApp(fakeApi({
      listCapabilities: vi.fn().mockResolvedValue([rejected]),
      getCapability: vi.fn().mockResolvedValue({ ...rejected, versions: [{ id: versionId, capability_id: capabilityId, connection_version_id: connectionId, sequence: 1, display_name: "Missing search", description: null, input_schema: {}, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp }], versions_truncated: false, observed_in_current_snapshot: false }),
    }));
    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    expect(await screen.findByRole("button", { name: "Reconsider version" })).toHaveProperty("disabled", true);
  });

  it("excludes enabled capabilities whose connection cannot execute", async () => {
    const listCapabilityPage = vi.fn<ControlPlane["listCapabilityPage"]>()
      .mockImplementation((_status, _cursor, executable) => Promise.resolve({ items: executable ? [] : [capability] }));
    renderApp(fakeApi({ listCapabilityPage }));
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    expect(await screen.findByRole("button", { name: "Review invocation" })).toHaveProperty("disabled", true);
    const executableSelect = screen.getByLabelText<HTMLSelectElement>("Enabled capability");
    expect([...executableSelect.options].some((option) => option.textContent?.includes("tools/search"))).toBe(false);
  });

  it("keeps historical capabilities available as run ledger filters", async () => {
    const historical = { ...capability, id: "66666666-6666-4666-8666-666666666666", tool_identity: "tools/archive", status: "disabled" as const, enabled_version_id: null };
    const listCapabilityPage = vi.fn<ControlPlane["listCapabilityPage"]>()
      .mockImplementation((_status, _cursor, executable) => Promise.resolve({ items: executable ? [capability] : [historical] }));
    renderApp(fakeApi({ listCapabilityPage }));
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    expect(await screen.findByRole("option", { name: "tools/archive" })).toBeTruthy();
    expect(screen.getByRole("option", { name: /tools\/search/ })).toBeTruthy();
  });

  it("blocks capability approval when its source connection cannot execute", async () => {
    const pending = { ...capability, status: "pending_review" as const, pending_version_id: versionId, enabled_version_id: null };
    renderApp(fakeApi({
      listCapabilities: vi.fn().mockResolvedValue([pending]),
      getCapability: vi.fn().mockResolvedValue({ ...pending, versions: [{ id: versionId, capability_id: capabilityId, connection_version_id: versionId, sequence: 1, display_name: "Search", description: null, input_schema: {}, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp }], versions_truncated: false, observed_in_current_snapshot: true }),
      getConnection: vi.fn().mockResolvedValue({ ...connection, lifecycle: "disabled" }),
    }));
    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    expect(await screen.findByRole("button", { name: "Enable exact version" })).toHaveProperty("disabled", true);
  });

  it("marks the version present in the current snapshot rather than the highest sequence", async () => {
    const currentVersionId = "66666666-6666-4666-8666-666666666666";
    renderApp(fakeApi({
      getCapability: vi.fn().mockResolvedValue({ ...capability, observed_version_id: currentVersionId, versions: [
        { id: versionId, capability_id: capabilityId, connection_version_id: versionId, sequence: 2, display_name: "Newer historical search", description: null, input_schema: {}, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp },
        { id: currentVersionId, capability_id: capabilityId, connection_version_id: versionId, sequence: 1, display_name: "Re-observed search", description: null, input_schema: {}, output_schema: null, metadata_digest: "d".repeat(64), schema_supported: true, created_at: timestamp },
      ], versions_truncated: false, observed_in_current_snapshot: true }),
    }));
    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    const marker = await screen.findByText("Current snapshot");
    expect(marker.closest("article")?.textContent).toContain("Re-observed search");
    expect(marker.closest("article")?.textContent).not.toContain("Newer historical search");
  });

  it("rotates capability action keys after the status epoch advances", async () => {
    const disabled = { ...capability, status: "disabled" as const, status_epoch: 4 };
    const enabled = { ...capability, status: "enabled" as const, status_epoch: 5 };
    const detail = (value: Capability) => ({ ...value, versions: [{ id: versionId, capability_id: capabilityId, connection_version_id: versionId, sequence: 1, display_name: "Search", description: null, input_schema: {}, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp }], versions_truncated: false, observed_in_current_snapshot: true });
    const getCapability = vi.fn<ControlPlane["getCapability"]>()
      .mockResolvedValueOnce(detail(capability))
      .mockResolvedValueOnce(detail(disabled))
      .mockResolvedValue(detail(enabled));
    const capabilityAction = vi.fn<ControlPlane["capabilityAction"]>().mockImplementation((_id, verb) => Promise.resolve(verb === "disable" ? disabled : enabled));
    renderApp(fakeApi({ getCapability, capabilityAction }));
    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Disable version" }));
    const reenable = await screen.findByRole("button", { name: "Re-enable version" });
    await waitFor(() => expect(reenable).toHaveProperty("disabled", false));
    const firstDisableKey = capabilityAction.mock.calls[0]?.[2];
    fireEvent.click(reenable);
    fireEvent.click(await screen.findByRole("button", { name: "Disable version" }));
    await waitFor(() => expect(capabilityAction).toHaveBeenCalledTimes(3));
    expect(capabilityAction.mock.calls[2]?.[2]).not.toBe(firstDisableKey);
  });

  it("preflights, confirms, follows, and cancels a run", async () => {
    const api = fakeApi();
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    expect(await screen.findByRole("heading", { name: "Runs" })).toBeTruthy();
    expect(await screen.findByRole("button", { name: /tools\/search.*Internal developer tools.*55555555/ })).toBeTruthy();

    await screen.findAllByRole("option", { name: /tools\/search/ });
    fireEvent.change(screen.getByLabelText("Enabled capability"), { target: { value: versionId } });
    fireEvent.change(screen.getByLabelText("Arguments"), { target: { value: "[]" } });
    fireEvent.click(screen.getByRole("button", { name: "Review invocation" }));
    expect(screen.getByRole("alert").textContent).toContain("JSON object");

    fireEvent.change(screen.getByLabelText("Arguments"), { target: { value: '{"query":"status"}' } });
    fireEvent.click(screen.getByRole("button", { name: "Review invocation" }));
    expect(await screen.findByRole("heading", { name: "Confirm exact invocation" })).toBeTruthy();
    const confirm = screen.getByRole("button", { name: "Confirm and run" });
    await waitFor(() => expect(confirm).toHaveProperty("disabled", false));
    fireEvent.click(confirm);
    await waitFor(() => expect(api.createRun).toHaveBeenCalled());

    expect(await screen.findByRole("heading", { name: "Execution timeline" })).toBeTruthy();
    expect(screen.getByText("admitted")).toBeTruthy();
    expect(screen.getByText("Initiating actor").parentElement?.textContent).toContain(connectionId);
    fireEvent.click(screen.getByRole("button", { name: "Request cancellation" }));
    await waitFor(() => expect(api.cancelRun).toHaveBeenCalledWith(runId, expect.any(String)));
  });

  it("does not redirect after a run finishes submitting from an abandoned Runs view", async () => {
    let resolveRun: ((value: Run) => void) | undefined;
    const createRun = vi.fn<ControlPlane["createRun"]>().mockImplementation(() => new Promise<Run>((resolve) => { resolveRun = resolve; }));
    const api = fakeApi({ createRun });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    await screen.findAllByRole("option", { name: /tools\/search/ });
    fireEvent.change(screen.getByLabelText("Enabled capability"), { target: { value: versionId } });
    fireEvent.click(screen.getByRole("button", { name: "Review invocation" }));
    const confirm = await screen.findByRole("button", { name: "Confirm and run" });
    await waitFor(() => expect(confirm).toHaveProperty("disabled", false));
    fireEvent.click(confirm);
    await waitFor(() => expect(createRun).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByRole("button", { name: "Modall overview" }));
    expect(await screen.findByRole("heading", { name: "Registry overview" })).toBeTruthy();
    await act(async () => {
      resolveRun?.(run);
      await Promise.resolve();
    });
    expect(screen.getByRole("heading", { name: "Registry overview" })).toBeTruthy();
    expect(window.location.pathname).toBe("/");
  });

  it("paginates the server-filtered executable capability projection", async () => {
    const olderCapability = { ...capability, id: "66666666-6666-4666-8666-666666666666", tool_identity: "tools/archive", enabled_version_id: "77777777-7777-4777-8777-777777777777" };
    const listCapabilityPage = vi.fn<ControlPlane["listCapabilityPage"]>()
      .mockImplementation((_status, cursor, executable) => Promise.resolve(
        executable
          ? cursor === "older" ? { items: [olderCapability] } : { items: [capability], nextCursor: "older" }
          : { items: [capability] },
      ));
    renderApp(fakeApi({ listCapabilityPage }));
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    expect(await screen.findAllByRole("option", { name: /tools\/search/ })).toHaveLength(2);
    expect(listCapabilityPage).toHaveBeenCalledWith("enabled", undefined, true);
    fireEvent.click(screen.getByRole("button", { name: "Load more executable tools" }));
    expect(await screen.findAllByRole("option", { name: /tools\/archive/ })).toHaveLength(1);
    expect(listCapabilityPage).toHaveBeenCalledWith("enabled", "older", true);
  });

  it("filters and explicitly paginates the run ledger", async () => {
    const listRunPage = vi.fn<ControlPlane["listRunPage"]>().mockResolvedValue({ items: [run], nextCursor: "older" });
    renderApp(fakeApi({ listRunPage }));
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    await waitFor(() => expect(listRunPage).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "failed" } });
    fireEvent.change(screen.getByLabelText("Actor ID"), { target: { value: connectionId } });
    fireEvent.change(screen.getByLabelText("Min duration (s)"), { target: { value: "5" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(listRunPage).toHaveBeenCalledWith(expect.objectContaining({ status: "failed", actor_id: connectionId, min_duration_seconds: 5 }), undefined));
    fireEvent.click(await screen.findByRole("button", { name: "Load older runs" }));
    await waitFor(() => expect(listRunPage).toHaveBeenCalledWith(expect.objectContaining({ status: "failed" }), "older"));
    await waitFor(() => expect(listRunPage.mock.calls.filter(([filters, cursor]) => filters?.status === "failed" && cursor === undefined).length).toBeGreaterThan(1), { timeout: 4000 });

    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.getByLabelText("Status")).toHaveProperty("value", "");
    expect(screen.getByLabelText("Actor ID")).toHaveProperty("value", "");
    expect(screen.getByLabelText("Min duration (s)")).toHaveProperty("value", "");
    await waitFor(() => expect(listRunPage).toHaveBeenCalledWith({}, undefined));
  });

  it("reports failures from the query that supplies the visible run ledger", async () => {
    const historyFailure = renderApp(fakeApi({ listRunPage: vi.fn().mockRejectedValue(new Error("history offline")) }));
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    expect(await screen.findByRole("button", { name: /55555555/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "failed" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    expect(await screen.findByRole("button", { name: "Try again" })).toBeTruthy();
    historyFailure.unmount();

    const runsFailure = renderApp(fakeApi({ listRuns: vi.fn().mockRejectedValue(new Error("reconciliation offline")) }));
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    expect(await screen.findByRole("button", { name: "Try again" })).toBeTruthy();
    runsFailure.unmount();
  });

  it("preserves a run key across ambiguous submission recovery", async () => {
    const createRun = vi.fn<ControlPlane["createRun"]>().mockRejectedValue(new Error("response lost"));
    const api = fakeApi({ createRun });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    await screen.findAllByRole("option", { name: /tools\/search/ });
    fireEvent.change(screen.getByLabelText("Enabled capability"), { target: { value: versionId } });
    fireEvent.click(screen.getByRole("button", { name: "Review invocation" }));
    let confirm = await screen.findByRole("button", { name: "Confirm and run" });
    await waitFor(() => expect(confirm).toHaveProperty("disabled", false));
    fireEvent.click(confirm);
    await waitFor(() => expect(createRun).toHaveBeenCalledTimes(1));
    const originalKey = createRun.mock.calls[0]?.[2] ?? "";

    fireEvent.click(screen.getByRole("button", { name: "Modall overview" }));
    fireEvent.click(await screen.findByRole("button", { name: /Runs in flight/ }));
    confirm = await screen.findByRole("button", { name: "Confirm and run" });
    await waitFor(() => expect(confirm).toHaveProperty("disabled", false));
    fireEvent.click(confirm);
    await waitFor(() => expect(createRun).toHaveBeenCalledTimes(2));
    expect(createRun.mock.calls[1]?.[2]).toBe(originalKey);
  });

  it("rotates a run key after an ambiguous invocation is abandoned", async () => {
    const createRun = vi.fn<ControlPlane["createRun"]>().mockRejectedValue(new Error("response lost"));
    renderApp(fakeApi({ createRun }));
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    await screen.findAllByRole("option", { name: /tools\/search/ });
    fireEvent.change(screen.getByLabelText("Enabled capability"), { target: { value: versionId } });
    fireEvent.click(screen.getByRole("button", { name: "Review invocation" }));
    let confirm = await screen.findByRole("button", { name: "Confirm and run" });
    await waitFor(() => expect(confirm).toHaveProperty("disabled", false));
    fireEvent.click(confirm);
    await waitFor(() => expect(createRun).toHaveBeenCalledTimes(1));
    const abandonedKey = createRun.mock.calls[0]?.[2];
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    fireEvent.click(screen.getByRole("button", { name: "Review invocation" }));
    confirm = await screen.findByRole("button", { name: "Confirm and run" });
    await waitFor(() => expect(confirm).toHaveProperty("disabled", false));
    fireEvent.click(confirm);
    await waitFor(() => expect(createRun).toHaveBeenCalledTimes(2));
    expect(createRun.mock.calls[1]?.[2]).not.toBe(abandonedKey);
  });

  it("warns operators not to retry an indeterminate run", async () => {
    const indeterminate = { ...run, status: "indeterminate" as const, safe_error_code: "upstream_outcome_unknown", terminal_at: timestamp };
    window.history.replaceState({}, "", `/runs/${runId}`);
    renderApp(fakeApi({ listRuns: vi.fn().mockResolvedValue([indeterminate]), getRun: vi.fn().mockResolvedValue(indeterminate) }));
    expect((await screen.findByRole("alert")).textContent).toContain("Do not retry this invocation");
  });

  it("uses server-relative confirmation lifetime and fetches a terminal run's final event", async () => {
    const terminalRun = { ...run, status: "succeeded" as const, terminal_at: timestamp };
    const listRunEvents = vi.fn().mockResolvedValue([{ id: connectionId, sequence: 1, event_type: "completed", status: "succeeded", safe_error_code: null, occurred_at: timestamp }]);
    const api = fakeApi({
      getRun: vi.fn().mockResolvedValue(terminalRun),
      listRunEvents,
      preflight: vi.fn().mockImplementation(() => Promise.resolve({ capability_version_id: versionId, connection_version_id: versionId, argument_digest: "c".repeat(64), confirmation_token: "confirmation", expires_at: "2020-01-01T00:01:00Z", server_observed_at: "2020-01-01T00:00:00Z" })),
    });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /55555555/ }));
    await waitFor(() => expect(listRunEvents.mock.calls.length).toBeGreaterThanOrEqual(2));

    fireEvent.change(screen.getByLabelText("Enabled capability"), { target: { value: versionId } });
    fireEvent.click(screen.getByRole("button", { name: "Review invocation" }));
    const confirm = await screen.findByRole("button", { name: "Confirm and run" });
    await waitFor(() => expect(confirm).toHaveProperty("disabled", false));
  });

  it("retries pinned endpoint resolution before confirmation expires", async () => {
    const recovered = { ...connection, versions: [{ id: versionId, sequence: 1, endpoint_url: "https://mcp.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http", created_at: timestamp }], versions_truncated: false };
    const getConnection = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(recovered);
    const api = fakeApi({ getConnection });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    await screen.findAllByRole("option", { name: /tools\/search/ });
    fireEvent.change(screen.getByLabelText("Enabled capability"), { target: { value: versionId } });
    fireEvent.click(screen.getByRole("button", { name: "Review invocation" }));
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    const confirm = await screen.findByRole("button", { name: "Confirm and run" });
    await waitFor(() => expect(confirm).toHaveProperty("disabled", false));
    expect(getConnection).toHaveBeenCalledTimes(2);
  });

  it("evicts selected terminal content at its retention deadline", async () => {
    const expiresAt = new Date(Date.now() + 1500).toISOString();
    const retained = { ...run, status: "succeeded" as const, result: { matches: 17 }, result_expires_at: expiresAt, arguments_expires_at: expiresAt, server_observed_at: new Date().toISOString(), terminal_at: timestamp };
    const expired = { ...retained, arguments: null, result: null, result_expires_at: null };
    const getRun = vi.fn().mockResolvedValueOnce(retained).mockResolvedValue(expired);
    window.history.replaceState({}, "", `/runs/${runId}`);
    renderApp(fakeApi({ listRuns: vi.fn().mockResolvedValue([retained]), getRun }));
    expect(await screen.findByText(/"matches": 17/)).toBeTruthy();
    await waitFor(() => expect(getRun).toHaveBeenCalledTimes(2), { timeout: 3000 });
    await waitFor(() => expect(screen.queryByText(/"matches": 17/)).toBeNull());
  });

  it("renders useful empty and failure states", async () => {
    const failed = fakeApi({ overview: vi.fn().mockRejectedValue(new Error("offline")) });
    const rendered = renderApp(failed);
    expect((await screen.findByRole("alert")).textContent).toContain("could not be reached");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(failed.overview).toHaveBeenCalledTimes(2));
    rendered.unmount();

    const denied = fakeApi({
      overview: vi.fn().mockRejectedValue(new ApiFailure("access_denied", "Workspace denied.", connectionId)),
    });
    const deniedRender = renderApp(denied);
    const deniedAlert = await screen.findByRole("alert");
    expect(deniedAlert.textContent).toContain("Code access_denied");
    expect(deniedAlert.textContent).toContain("Reference 22222222…2222");
    deniedRender.unmount();

    localStorage.clear();
    const empty = fakeApi({ overview: vi.fn().mockResolvedValue({ connections: [], capabilities: [], runs: [] }) });
    renderApp(empty);
    expect(await screen.findByText("No servers connected")).toBeTruthy();
    expect(screen.getAllByText("00")).toHaveLength(3);
  });

  it("keeps selections in browser history and restores deep links", async () => {
    window.history.replaceState({}, "", `/runs/${runId}`);
    const api = fakeApi();
    renderApp(api);
    expect(await screen.findByRole("heading", { name: "Execution timeline" })).toBeTruthy();
    expect(window.location.pathname).toBe(`/runs/${runId}`);

    window.history.pushState({}, "", `/capabilities/${capabilityId}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(await screen.findByText("Search public records")).toBeTruthy();
    await waitFor(() => expect(document.activeElement?.id).toBe("main-content"));
  });

  it("makes viewer sessions read-only", async () => {
    const api = fakeApi();
    renderApp(api, true, "viewer");
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    expect(await screen.findByRole("heading", { name: "Server registry" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Add manually" })).toBeNull();
    expect(screen.getByRole("button", { name: "Search" })).toHaveProperty("disabled", true);
    expect(screen.queryByRole("button", { name: /Audit/ })).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    expect(screen.queryByRole("button", { name: "Refresh" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Disable" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    expect(await screen.findByRole("button", { name: "Disable version" })).toHaveProperty("disabled", true);

    fireEvent.click(screen.getByRole("button", { name: /Runs$/ }));
    expect(await screen.findByRole("button", { name: "Review invocation" })).toHaveProperty("disabled", true);
    fireEvent.click(await screen.findByRole("button", { name: /55555555/ }));
    expect(await screen.findByRole("heading", { name: "Execution timeline" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Request cancellation" })).toBeNull();
  });

  it("hides refresh on disabled connections and reserves re-enable for admins", async () => {
    const disabled = { ...connection, lifecycle: "disabled" as const };
    const api = fakeApi({
      listConnections: vi.fn().mockResolvedValue([disabled]),
      getConnection: vi.fn().mockResolvedValue({ ...disabled, versions: [], versions_truncated: false }),
    });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    expect(await screen.findByRole("button", { name: "Re-enable" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Refresh" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Verify pending" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Append immutable version" })).toBeNull();
  });

  it("applies operator and immutable-version action boundaries", async () => {
    const historicalId = "66666666-6666-4666-8666-666666666666";
    const api = fakeApi({
      getCapability: vi.fn().mockResolvedValue({
        ...capability,
        versions: [
          { id: versionId, capability_id: capabilityId, connection_version_id: versionId, sequence: 2, display_name: "Search", description: null, input_schema: {}, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp },
          { id: historicalId, capability_id: capabilityId, connection_version_id: connectionId, sequence: 1, display_name: "Old search", description: null, input_schema: {}, output_schema: null, metadata_digest: "d".repeat(64), schema_supported: true, created_at: timestamp },
        ],
        versions_truncated: false,
      }),
    });
    renderApp(api, true, "operator");
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    expect(await screen.findByRole("heading", { name: "Server registry" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Add manually" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Registry search"), { target: { value: "search" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByRole("button", { name: "Import" })).toHaveProperty("disabled", false);

    fireEvent.click(screen.getByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    expect(await screen.findByRole("button", { name: "Historical version" })).toHaveProperty("disabled", true);
  });

  it("requires a fresh preflight after a definitive confirmation failure", async () => {
    const api = fakeApi({ createRun: vi.fn().mockRejectedValue(new ApiFailure("confirmation_expired", "Confirmation expired.")) });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    await screen.findAllByRole("option", { name: /tools\/search/ });
    fireEvent.change(screen.getByLabelText("Enabled capability"), { target: { value: versionId } });
    fireEvent.click(screen.getByRole("button", { name: "Review invocation" }));
    const confirm = await screen.findByRole("button", { name: "Confirm and run" });
    await waitFor(() => expect(confirm).toHaveProperty("disabled", false));
    fireEvent.click(confirm);
    expect((await screen.findByRole("alert")).textContent).toContain("Run preflight again");
    expect(screen.queryByRole("heading", { name: "Confirm exact invocation" })).toBeNull();
  });

  it("refreshes capability history explicitly instead of polling large schemas", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const getCapability = vi.fn<ControlPlane["getCapability"]>().mockResolvedValue({ ...capability, versions: [{ id: versionId, capability_id: capabilityId, connection_version_id: versionId, sequence: 1, display_name: "Search", description: null, input_schema: {}, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp }], versions_truncated: false, observed_in_current_snapshot: true });
    renderApp(fakeApi({ getCapability }));
    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    await waitFor(() => expect(getCapability).toHaveBeenCalledOnce());
    await vi.advanceTimersByTimeAsync(5_100);
    expect(getCapability).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Refresh tools" }));
    await waitFor(() => expect(getCapability).toHaveBeenCalledTimes(2));
    vi.useRealTimers();
  });

  it("retries failed searches and expires stale import controls", async () => {
    const recovered = { cache_id: connectionId, fetched_at: timestamp, expires_at: new Date(Date.now() + 60_000).toISOString(), server_observed_at: new Date().toISOString(), from_cache: false, items: [{ external_id: "entry", source_version: "1", name: "Recovered", description: null, advertised_urls: [], provenance_digest: "a".repeat(64) }] };
    const searchRegistry = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(recovered);
    const api = fakeApi({ searchRegistry });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.change(screen.getByLabelText("Registry search"), { target: { value: "search" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Recovered")).toBeTruthy();
    expect(searchRegistry).toHaveBeenCalledTimes(2);

    searchRegistry.mockResolvedValueOnce({ ...recovered, expires_at: "2020-01-01T00:01:00Z", server_observed_at: "2020-01-01T00:00:00Z" });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(searchRegistry).toHaveBeenCalledTimes(3));
    expect(screen.getByRole("button", { name: "Import" })).toHaveProperty("disabled", false);

    searchRegistry.mockResolvedValueOnce({ ...recovered, expires_at: "2020-01-01T00:00:00Z", server_observed_at: "2020-01-01T00:00:01Z" });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText(/results expired/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Import" })).toHaveProperty("disabled", true);
  });

  it("clears a failed import when a new registry search starts", async () => {
    const api = fakeApi({ importRegistry: vi.fn().mockRejectedValue(new Error("import failed")) });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.change(screen.getByLabelText("Registry search"), { target: { value: "search" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByRole("button", { name: "Import" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(api.searchRegistry).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("preserves a registry mutation key across navigation", async () => {
    const connectionAction = vi.fn<ControlPlane["connectionAction"]>().mockRejectedValue(new Error("response lost"));
    const api = fakeApi({ connectionAction });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(connectionAction).toHaveBeenCalledTimes(1));
    const originalKey = connectionAction.mock.calls[0]?.[2];
    fireEvent.click(screen.getByRole("button", { name: "Modall overview" }));
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(connectionAction).toHaveBeenCalledTimes(2));
    expect(connectionAction.mock.calls[1]?.[2]).toBe(originalKey);
  });

  it("clears a failed lifecycle action when another connection is selected", async () => {
    const otherConnection = { ...connection, id: "66666666-6666-4666-8666-666666666666", name: "Release tooling" };
    const detail = (value: Connection) => ({ ...value, versions: [{ id: versionId, sequence: 1, endpoint_url: "https://mcp.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http" as const, created_at: timestamp }], versions_truncated: false });
    const api = fakeApi({
      listConnections: vi.fn().mockResolvedValue([connection, otherConnection]),
      getConnection: vi.fn((id) => Promise.resolve(detail(id === connectionId ? connection : otherConnection))),
      connectionAction: vi.fn().mockRejectedValue(new Error("refresh failed")),
    });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Refresh" }));
    expect(await screen.findByRole("alert")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Release tooling/ }));
    expect(await screen.findByText("Release tooling")).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("rotates refresh keys when discovery generation advances", async () => {
    const connectionAction = vi.fn<ControlPlane["connectionAction"]>().mockRejectedValue(new Error("response lost"));
    const initial = { ...connection, versions: [{ id: versionId, sequence: 1, endpoint_url: "https://mcp.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http" as const, created_at: timestamp }], versions_truncated: false };
    const advanced = { ...initial, refresh_generation: initial.refresh_generation + 1 };
    const getConnection = vi.fn<ControlPlane["getConnection"]>().mockResolvedValueOnce(initial).mockResolvedValue(advanced);
    renderApp(fakeApi({ connectionAction, getConnection }));
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(connectionAction).toHaveBeenCalledTimes(1));
    const originalKey = connectionAction.mock.calls[0]?.[2];
    fireEvent.click(screen.getByRole("button", { name: "Modall overview" }));
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    await waitFor(() => expect(screen.getByText("Refresh generation").parentElement?.textContent).toContain("5"));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(connectionAction).toHaveBeenCalledTimes(2));
    expect(connectionAction.mock.calls[1]?.[2]).not.toBe(originalKey);
  });

  it("rotates connection action keys after the control epoch advances", async () => {
    const disabled = { ...connection, lifecycle: "disabled" as const, control_epoch: 3 };
    const enabled = { ...connection, lifecycle: "active" as const, control_epoch: 4 };
    const detail = (value: Connection) => ({ ...value, versions: [{ id: versionId, sequence: 1, endpoint_url: "https://mcp.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http" as const, created_at: timestamp }], versions_truncated: false });
    const getConnection = vi.fn<ControlPlane["getConnection"]>()
      .mockResolvedValueOnce(detail(connection))
      .mockResolvedValueOnce(detail(disabled))
      .mockResolvedValue(detail(enabled));
    const connectionAction = vi.fn<ControlPlane["connectionAction"]>().mockResolvedValue(undefined);
    renderApp(fakeApi({ getConnection, connectionAction }));
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Disable" }));
    const reenable = await screen.findByRole("button", { name: "Re-enable" });
    const firstDisableKey = connectionAction.mock.calls[0]?.[2];
    fireEvent.click(reenable);
    fireEvent.click(await screen.findByRole("button", { name: "Disable" }));
    await waitFor(() => expect(connectionAction).toHaveBeenCalledTimes(3));
    expect(connectionAction.mock.calls[2]?.[2]).not.toBe(firstDisableKey);
  });

  it("refreshes untouched version defaults when a newer version appears", async () => {
    const newerVersionId = "77777777-7777-4777-8777-777777777777";
    const initial = { ...connection, versions: [{ id: versionId, sequence: 1, endpoint_url: "https://old.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http" as const, created_at: timestamp }], versions_truncated: false };
    const updated = { ...connection, versions: [{ id: newerVersionId, sequence: 2, endpoint_url: "https://new.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http" as const, created_at: timestamp }], versions_truncated: false };
    const getConnection = vi.fn<ControlPlane["getConnection"]>().mockResolvedValueOnce(initial).mockResolvedValue(updated);
    renderApp(fakeApi({ getConnection }));
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    expect(await screen.findByDisplayValue("https://old.example/tools")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByDisplayValue("https://new.example/tools")).toBeTruthy();
  });

  it("rotates append keys when the base version advances", async () => {
    const newerVersionId = "77777777-7777-4777-8777-777777777777";
    const initial = { ...connection, versions: [{ id: versionId, sequence: 1, endpoint_url: "https://old.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http" as const, created_at: timestamp }], versions_truncated: false };
    const advanced = { ...connection, versions: [{ id: newerVersionId, sequence: 2, endpoint_url: "https://new.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http" as const, created_at: timestamp }], versions_truncated: false };
    const getConnection = vi.fn<ControlPlane["getConnection"]>().mockResolvedValueOnce(initial).mockResolvedValue(advanced);
    const appendConnectionVersion = vi.fn<ControlPlane["appendConnectionVersion"]>().mockRejectedValue(new Error("response lost"));
    renderApp(fakeApi({ getConnection, appendConnectionVersion }));
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Append version" }));
    await waitFor(() => expect(appendConnectionVersion).toHaveBeenCalledTimes(1));
    const originalKey = appendConnectionVersion.mock.calls[0]?.[2];
    fireEvent.click(screen.getByRole("button", { name: "Modall overview" }));
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    const endpoint = await screen.findByDisplayValue("https://new.example/tools");
    fireEvent.change(endpoint, { target: { value: "https://old.example/tools" } });
    fireEvent.click(screen.getByRole("button", { name: "Append version" }));
    await waitFor(() => expect(appendConnectionVersion).toHaveBeenCalledTimes(2));
    expect(appendConnectionVersion.mock.calls[1]?.[2]).not.toBe(originalKey);
  });

  it("clears a failed append when another connection is selected", async () => {
    const otherConnection = { ...connection, id: "66666666-6666-4666-8666-666666666666", name: "Release tooling" };
    const detail = (value: Connection) => ({ ...value, versions: [{ id: versionId, sequence: 1, endpoint_url: "https://mcp.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http" as const, created_at: timestamp }], versions_truncated: false });
    const appendConnectionVersion = vi.fn<ControlPlane["appendConnectionVersion"]>().mockRejectedValue(new Error("append failed"));
    renderApp(fakeApi({
      listConnections: vi.fn().mockResolvedValue([connection, otherConnection]),
      getConnection: vi.fn((id) => Promise.resolve(detail(id === connectionId ? connection : otherConnection))),
      appendConnectionVersion,
    }));
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Append version" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Release tooling/ }));
    expect(await screen.findByText("Release tooling")).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("preserves seconds in run timeline diagnostics", async () => {
    window.history.replaceState({}, "", `/runs/${runId}`);
    renderApp(fakeApi({ listRunEvents: vi.fn().mockResolvedValue([{ id: connectionId, sequence: 1, event_type: "admitted", status: "queued", safe_error_code: null, occurred_at: "2026-09-06T12:00:37Z" }]) }));
    expect((await screen.findByText(/:37/)).textContent).toContain("queued");
  });

  it("keeps cancellation available when the event timeline fails", async () => {
    const cancelRun = vi.fn<ControlPlane["cancelRun"]>().mockResolvedValue({ ...run, cancellation_requested: true });
    window.history.replaceState({}, "", `/runs/${runId}`);
    renderApp(fakeApi({ listRunEvents: vi.fn().mockRejectedValue(new Error("offline")), cancelRun }));
    fireEvent.click(await screen.findByRole("button", { name: "Request cancellation" }));
    await waitFor(() => expect(cancelRun).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });

  it("clears a failed cancellation when another run is selected", async () => {
    const otherRun = { ...run, id: "66666666-6666-4666-8666-666666666666" };
    const cancelRun = vi.fn<ControlPlane["cancelRun"]>().mockRejectedValue(new Error("cancel failed"));
    renderApp(fakeApi({
      listRuns: vi.fn().mockResolvedValue([run, otherRun]),
      listRunPage: vi.fn().mockResolvedValue({ items: [run, otherRun] }),
      getRun: vi.fn((id) => Promise.resolve(id === runId ? run : otherRun)),
      cancelRun,
    }));
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    const runButtons = await screen.findAllByRole("button", { name: /55555555|66666666/ });
    fireEvent.click(runButtons.find((button) => button.textContent?.includes("55555555")) as HTMLButtonElement);
    fireEvent.click(await screen.findByRole("button", { name: "Request cancellation" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /66666666/ }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("shows output contracts, cancellation progress, and paginated audit history", async () => {
    const event = { id: connectionId, actor_user_id: connectionId, action: "connection.created", resource_type: "connection", resource_id: connectionId, outcome: "succeeded", correlation_id: runId, occurred_at: timestamp };
    const api = fakeApi({
      getCapability: vi.fn().mockResolvedValue({ ...capability, versions: [{ id: versionId, capability_id: capabilityId, connection_version_id: versionId, sequence: 1, display_name: "Search", description: null, input_schema: {}, output_schema: { type: "object" }, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp }], versions_truncated: false }),
      getRun: vi.fn().mockResolvedValue({ ...run, cancellation_requested: true }),
      listAuditEvents: vi.fn().mockResolvedValueOnce({ items: [event], nextCursor: "older" }).mockResolvedValueOnce({ items: [event], nextCursor: "older" }).mockResolvedValueOnce({ items: [{ ...event, id: versionId }]}),
    });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Capabilities/ }));
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    expect(await screen.findByText("Output schema")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /Runs$/ }));
    fireEvent.click(await screen.findByRole("button", { name: /55555555/ }));
    expect(await screen.findByText(/Cancellation requested/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Request cancellation" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Audit/ }));
    expect(await screen.findByText("connection created")).toBeTruthy();
    expect(await screen.findByText(new RegExp(`Actor ${connectionId}`))).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Resource type"), { target: { value: "server_connection" } });
    fireEvent.change(screen.getByLabelText("Resource ID"), { target: { value: connectionId } });
    fireEvent.change(screen.getByLabelText("Actor ID"), { target: { value: connectionId } });
    fireEvent.change(screen.getByLabelText("Action"), { target: { value: "connection.created" } });
    fireEvent.change(screen.getByLabelText("Outcome"), { target: { value: "succeeded" } });
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-09-06T08:00" } });
    fireEvent.change(screen.getByLabelText("Before"), { target: { value: "2026-09-07T08:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply filters" }));
    await waitFor(() => expect(api.listAuditEvents).toHaveBeenCalledWith({ resource_type: "server_connection", resource_id: connectionId, actor_id: connectionId, action: "connection.created", outcome: "succeeded", occurred_after: new Date("2026-09-06T08:00").toISOString(), occurred_before: new Date("2026-09-07T08:00").toISOString() }, undefined));
    fireEvent.click(await screen.findByRole("button", { name: "Load older events" }));
    await waitFor(() => expect(api.listAuditEvents).toHaveBeenCalledTimes(3));
    fireEvent.click(screen.getByRole("button", { name: "Refresh ledger" }));
    await waitFor(() => expect(vi.mocked(api.listAuditEvents).mock.calls.length).toBeGreaterThan(3));
  });
});

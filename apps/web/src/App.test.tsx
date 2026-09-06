/* eslint-disable @typescript-eslint/unbound-method */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { ApiFailure, type Capability, type Connection, type ControlPlane, type Run } from "./api/operations";
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
  pending_version_id: versionId,
  verified_version_id: null,
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
  pending_version_id: versionId,
  enabled_version_id: versionId,
  status: "enabled",
  status_epoch: 3,
  created_at: timestamp,
};

const run: Run = {
  id: runId,
  capability_id: capabilityId,
  capability_version_id: versionId,
  connection_id: connectionId,
  connection_version_id: versionId,
  status: "running",
  arguments: { query: "status" },
  result: null,
  safe_error_code: null,
  cancellation_requested: false,
  deadline: "2026-09-06T12:05:00Z",
  created_at: timestamp,
  updated_at: timestamp,
  terminal_at: null,
};

function fakeApi(overrides: Partial<ControlPlane> = {}): ControlPlane {
  return {
    currentSession: vi.fn().mockResolvedValue({ workspace_id: workspaceId, actor_user_id: connectionId, role: "admin" }),
    overview: vi.fn().mockResolvedValue({ connections: [connection], capabilities: [capability], runs: [run] }),
    listConnections: vi.fn().mockResolvedValue([connection]),
    getConnection: vi.fn().mockResolvedValue({ ...connection, versions: [{ id: versionId, sequence: 1, endpoint_url: "https://mcp.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http", created_at: timestamp }], versions_truncated: false }),
    createConnection: vi.fn().mockResolvedValue(connection),
    appendConnectionVersion: vi.fn().mockResolvedValue({ id: versionId, sequence: 2, endpoint_url: "https://mcp.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http", created_at: timestamp }),
    connectionAction: vi.fn().mockResolvedValue(undefined),
    searchRegistry: vi.fn().mockResolvedValue({ cache_id: connectionId, fetched_at: timestamp, expires_at: "2026-09-06T23:00:00Z", from_cache: false, items: [{ external_id: "io.modall/search", source_version: "1.2.0", name: "Public search", description: "Search public records", advertised_urls: ["https://mcp.example/tools"], provenance_digest: "a".repeat(64) }] }),
    importRegistry: vi.fn().mockResolvedValue({ id: connectionId, source: "official", external_id: "io.modall/search", current_version_id: versionId, name: "Public search", description: "Search public records", created_at: timestamp }),
    listRegistryEntries: vi.fn().mockResolvedValue([]),
    listCapabilities: vi.fn().mockResolvedValue([capability]),
    getCapability: vi.fn().mockResolvedValue({ ...capability, versions: [{ id: versionId, capability_id: capabilityId, sequence: 1, display_name: "Search", description: "Search public records", input_schema: { type: "object" }, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp }], versions_truncated: false }),
    capabilityAction: vi.fn().mockResolvedValue(capability),
    listRuns: vi.fn().mockResolvedValue([run]),
    getRun: vi.fn().mockResolvedValue(run),
    listRunEvents: vi.fn().mockResolvedValue([{ id: connectionId, sequence: 1, event_type: "admitted", status: "queued", safe_error_code: null, occurred_at: timestamp }]),
    preflight: vi.fn().mockResolvedValue({ capability_version_id: versionId, connection_version_id: versionId, argument_digest: "c".repeat(64), confirmation_token: "confirmation", expires_at: "2026-09-06T12:03:00Z" }),
    createRun: vi.fn().mockResolvedValue(run),
    cancelRun: vi.fn().mockResolvedValue({ ...run, status: "cancelled" }),
    listAuditEvents: vi.fn().mockResolvedValue({ items: [] }),
    ...overrides,
  };
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

  afterEach(() => cleanup());

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
    expect(screen.getByRole("heading", { name: "Open a workspace" })).toBeTruthy();
    expect(localStorage.length).toBe(0);
  });

  it("shows workspace posture and navigates through registry lifecycle controls", async () => {
    const api = fakeApi();
    renderApp(api);
    expect(await screen.findByText("Internal developer tools")).toBeTruthy();
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
    fireEvent.click(screen.getByRole("button", { name: "Modall overview" }));
    expect(await screen.findByRole("heading", { name: "Registry overview" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /manage registry/i }));
    expect(await screen.findByRole("heading", { name: "Server registry" })).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Registry search"), { target: { value: "search" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Public search")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() => expect(api.importRegistry).toHaveBeenCalled());

    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Internal developer tools" } });
    fireEvent.change(screen.getByLabelText("HTTPS endpoint"), { target: { value: "https://mcp.example/tools" } });
    fireEvent.click(screen.getByRole("button", { name: "Add connection" }));
    await waitFor(() => expect(api.createConnection).toHaveBeenCalled());

    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    expect((await screen.findAllByText("https://mcp.example/tools")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Verify pending" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    fireEvent.click(screen.getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(api.connectionAction).toHaveBeenCalledTimes(3));
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

  it("preflights, confirms, follows, and cancels a run", async () => {
    const api = fakeApi();
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Runs$/ }));
    expect(await screen.findByRole("heading", { name: "Runs" })).toBeTruthy();

    await screen.findByRole("option", { name: /tools\/search/ });
    fireEvent.change(screen.getByLabelText("Enabled capability"), { target: { value: versionId } });
    fireEvent.change(screen.getByLabelText("Arguments"), { target: { value: "[]" } });
    fireEvent.click(screen.getByRole("button", { name: "Review invocation" }));
    expect(screen.getByRole("alert").textContent).toContain("JSON object");

    fireEvent.change(screen.getByLabelText("Arguments"), { target: { value: '{"query":"status"}' } });
    fireEvent.click(screen.getByRole("button", { name: "Review invocation" }));
    expect(await screen.findByRole("heading", { name: "Confirm exact invocation" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm and run" }));
    await waitFor(() => expect(api.createRun).toHaveBeenCalled());

    expect(await screen.findByRole("heading", { name: "Execution timeline" })).toBeTruthy();
    expect(screen.getByText("admitted")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Request cancellation" }));
    await waitFor(() => expect(api.cancelRun).toHaveBeenCalledWith(runId, expect.any(String)));
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
    expect((await screen.findByRole("alert")).textContent).toContain("Reference 22222222…2222");
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
          { id: versionId, capability_id: capabilityId, sequence: 2, display_name: "Search", description: null, input_schema: {}, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp },
          { id: historicalId, capability_id: capabilityId, sequence: 1, display_name: "Old search", description: null, input_schema: {}, output_schema: null, metadata_digest: "d".repeat(64), schema_supported: true, created_at: timestamp },
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
    await screen.findByRole("option", { name: /tools\/search/ });
    fireEvent.change(screen.getByLabelText("Enabled capability"), { target: { value: versionId } });
    fireEvent.click(screen.getByRole("button", { name: "Review invocation" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm and run" }));
    expect((await screen.findByRole("alert")).textContent).toContain("Run preflight again");
    expect(screen.queryByRole("heading", { name: "Confirm exact invocation" })).toBeNull();
  });

  it("retries failed searches and expires stale import controls", async () => {
    const recovered = { cache_id: connectionId, fetched_at: timestamp, expires_at: "2026-09-06T23:00:00Z", from_cache: false, items: [{ external_id: "entry", source_version: "1", name: "Recovered", description: null, advertised_urls: [], provenance_digest: "a".repeat(64) }] };
    const searchRegistry = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(recovered);
    const api = fakeApi({ searchRegistry });
    renderApp(api);
    fireEvent.click(await screen.findByRole("button", { name: /Registry/ }));
    fireEvent.change(screen.getByLabelText("Registry search"), { target: { value: "search" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    fireEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Recovered")).toBeTruthy();
    expect(searchRegistry).toHaveBeenCalledTimes(2);

    searchRegistry.mockResolvedValueOnce({ ...recovered, expires_at: "2020-01-01T00:00:00Z" });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText(/results expired/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Import" })).toHaveProperty("disabled", true);
  });

  it("shows output contracts, cancellation progress, and paginated audit history", async () => {
    const event = { id: connectionId, actor_user_id: connectionId, action: "connection.created", resource_type: "connection", resource_id: connectionId, outcome: "succeeded", correlation_id: runId, occurred_at: timestamp };
    const api = fakeApi({
      getCapability: vi.fn().mockResolvedValue({ ...capability, versions: [{ id: versionId, capability_id: capabilityId, sequence: 1, display_name: "Search", description: null, input_schema: {}, output_schema: { type: "object" }, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp }], versions_truncated: false }),
      getRun: vi.fn().mockResolvedValue({ ...run, cancellation_requested: true }),
      listAuditEvents: vi.fn().mockResolvedValueOnce({ items: [event], nextCursor: "older" }).mockResolvedValueOnce({ items: [{ ...event, id: versionId }]}),
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
    fireEvent.click(screen.getByRole("button", { name: "Load older events" }));
    await waitFor(() => expect(api.listAuditEvents).toHaveBeenCalledTimes(2));
  });
});

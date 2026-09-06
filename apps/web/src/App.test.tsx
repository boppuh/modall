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
    overview: vi.fn().mockResolvedValue({ connections: [connection], capabilities: [capability], runs: [run] }),
    listConnections: vi.fn().mockResolvedValue([connection]),
    getConnection: vi.fn().mockResolvedValue({ ...connection, versions: [{ id: versionId, sequence: 1, endpoint_url: "https://mcp.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http", created_at: timestamp }], versions_truncated: false }),
    createConnection: vi.fn().mockResolvedValue(connection),
    connectionAction: vi.fn().mockResolvedValue(undefined),
    searchRegistry: vi.fn().mockResolvedValue({ cache_id: connectionId, fetched_at: timestamp, expires_at: timestamp, from_cache: false, items: [{ external_id: "io.modall/search", source_version: "1.2.0", name: "Public search", description: "Search public records", advertised_urls: ["https://mcp.example/tools"], provenance_digest: "a".repeat(64) }] }),
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
    ...overrides,
  };
}

function renderApp(api: ControlPlane, authenticated = true) {
  if (authenticated) {
    const session: WorkspaceSession = {
      identityId: "pilot-reviewer",
      workspaceId,
      workspaceLabel: "iOS pilot",
    };
    saveSession(session);
  }
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
    fireEvent.click(screen.getByRole("button", { name: "Add and verify" }));
    await waitFor(() => expect(api.createConnection).toHaveBeenCalled());

    fireEvent.click(await screen.findByRole("button", { name: /Internal developer tools/ }));
    expect(await screen.findByText("https://mcp.example/tools")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Verify pending" }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    fireEvent.click(screen.getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(api.connectionAction).toHaveBeenCalledTimes(3));
  });

  it("reviews an immutable capability version and changes its approval", async () => {
    const api = fakeApi();
    renderApp(api);
    fireEvent.click(screen.getByRole("button", { name: /Capabilities/ }));
    expect(await screen.findByRole("heading", { name: "Capabilities" })).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: /tools\/search/ }));
    expect(await screen.findByText("Search public records")).toBeTruthy();
    expect(screen.getByText(/"type": "object"/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Disable version" }));
    await waitFor(() => expect(api.capabilityAction).toHaveBeenCalledWith(versionId, "disable"));
  });

  it("preflights, confirms, follows, and cancels a run", async () => {
    const api = fakeApi();
    renderApp(api);
    fireEvent.click(screen.getByRole("button", { name: /Runs$/ }));
    expect(await screen.findByRole("heading", { name: "Runs" })).toBeTruthy();

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
    await waitFor(() => expect(api.cancelRun).toHaveBeenCalledWith(runId));
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
});

import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiFailure, createControlPlane } from "./operations";

const id = "11111111-1111-4111-8111-111111111111";
const otherId = "22222222-2222-4222-8222-222222222222";
const timestamp = "2026-09-06T12:00:00Z";

const connection = {
  id,
  name: "Tools",
  lifecycle: "active",
  pending_version_id: otherId,
  verified_version_id: id,
  control_epoch: 1,
  refresh_generation: 2,
  last_refresh_at: timestamp,
  last_refresh_error_code: null,
  created_at: timestamp,
};
const capability = {
  id,
  connection_id: id,
  tool_identity: "tools/search",
  pending_version_id: otherId,
  enabled_version_id: otherId,
  status: "enabled",
  status_epoch: 2,
  created_at: timestamp,
};
const run = {
  id,
  actor_user_id: otherId,
  capability_id: id,
  capability_version_id: otherId,
  connection_id: id,
  connection_version_id: otherId,
  status: "succeeded",
  arguments: {},
  arguments_expires_at: timestamp,
  result: {},
  result_expires_at: timestamp,
  server_observed_at: timestamp,
  safe_error_code: null,
  cancellation_requested: false,
  deadline: timestamp,
  created_at: timestamp,
  updated_at: timestamp,
  terminal_at: timestamp,
};

describe("control-plane operations", () => {
  let requests: Request[];
  let failNext: boolean;

  beforeEach(() => {
    requests = [];
    failNext = false;
    vi.stubGlobal("crypto", { randomUUID: () => "99999999-9999-4999-8999-999999999999" });
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>((input) => {
        const request = input instanceof Request ? input : new Request(input);
        requests.push(request);
        if (failNext) {
          failNext = false;
          return Promise.resolve(
            new Response(
              JSON.stringify({
                error: { code: "upstream_unavailable", message: "Registry unavailable." },
                correlation_id: otherId,
              }),
              { status: 503, headers: { "Content-Type": "application/json" } },
            ),
          );
        }
        const path = new URL(request.url).pathname;
        let payload: unknown = {};
        if (path === "/v1/session") {
          payload = { workspace_id: id, actor_user_id: otherId, role: "admin" };
        } else if (path === "/v1/server-connections" && request.method === "GET") {
          payload = { items: [connection], page: { next_cursor: null } };
        } else if (path === "/v1/server-connections" && request.method === "POST") {
          payload = connection;
        } else if (path === `/v1/server-connections/${id}`) {
          payload = { ...connection, versions: [], versions_truncated: false };
        } else if (path === `/v1/server-connections/${id}/versions`) {
          payload = { id: otherId, sequence: 2, endpoint_url: "https://mcp.example/v2", secret_binding_id: null, policy_version: "v1", transport: "streamable_http", created_at: timestamp };
        } else if (path.endsWith("/refresh")) {
          payload = { connection_id: id, connection_version_id: otherId, generation: 3, job_id: id, status: "queued" };
        } else if (path.startsWith("/v1/server-connections/")) {
          payload = connection;
        } else if (path === "/v1/registry/searches") {
          payload = { cache_id: id, items: [], fetched_at: timestamp, expires_at: timestamp, server_observed_at: timestamp, from_cache: false };
        } else if (path === "/v1/registry/imports") {
          payload = { id, source: "official", external_id: "entry", current_version_id: otherId, name: "Entry", description: null, created_at: timestamp };
        } else if (path === "/v1/registry/entries") {
          payload = { items: [], page: { next_cursor: null } };
        } else if (path === "/v1/capabilities") {
          payload = { items: [capability], page: { next_cursor: null } };
        } else if (path === `/v1/capabilities/${id}`) {
          payload = { ...capability, versions: [], versions_truncated: false };
        } else if (path.startsWith("/v1/capability-versions/")) {
          payload = capability;
        } else if (path === "/v1/runs" && request.method === "GET") {
          payload = { items: [run], page: { next_cursor: null } };
        } else if (path === "/v1/runs" && request.method === "POST") {
          payload = run;
        } else if (path === `/v1/runs/${id}/events`) {
          payload = { items: [], page: { next_cursor: null } };
        } else if (path.startsWith("/v1/runs/")) {
          payload = run;
        } else if (path === "/v1/run-preflights") {
          payload = { capability_version_id: otherId, connection_version_id: otherId, argument_digest: "a".repeat(64), confirmation_token: "token", expires_at: timestamp, server_observed_at: timestamp };
        } else if (path === "/v1/audit-events") {
          payload = { items: [], page: { next_cursor: null } };
        }
        return Promise.resolve(
          new Response(JSON.stringify(payload), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );
  });

  it("executes the complete generated-client operator surface", async () => {
    const api = createControlPlane({ identityId: "reviewer", workspaceId: id });
    expect((await api.currentSession()).role).toBe("admin");
    expect((await api.overview()).connections).toHaveLength(1);
    expect(await api.listConnections()).toHaveLength(1);
    expect((await api.listConnectionPage()).items).toHaveLength(1);
    expect((await api.getConnection(id)).id).toBe(id);
    expect((await api.createConnection({ name: "Tools", endpointUrl: "https://mcp.example", secretBindingId: otherId }, "create-key")).id).toBe(id);
    await api.appendConnectionVersion(id, { endpointUrl: "https://mcp.example/v2" }, "append-key");
    await api.connectionAction(id, "verify", "verify-key");
    await api.connectionAction(id, "refresh", "refresh-key");
    await api.connectionAction(id, "enable", "enable-key");
    await api.connectionAction(id, "disable", "disable-key");
    expect((await api.searchRegistry("search")).cache_id).toBe(id);
    expect((await api.importRegistry(id, "a".repeat(64), "import-key")).source).toBe("official");
    expect(await api.listRegistryEntries()).toEqual([]);
    expect(await api.listCapabilities("pending_review")).toHaveLength(1);
    expect((await api.listCapabilityPage("pending_review", "older")).items).toHaveLength(1);
    expect(requests.some((request) => new URL(request.url).searchParams.get("status") === "pending_review")).toBe(true);
    expect(requests.some((request) => new URL(request.url).searchParams.get("cursor") === "older")).toBe(true);
    await api.listCapabilityPage("enabled", undefined, true);
    expect(requests.some((request) => new URL(request.url).searchParams.get("executable") === "true")).toBe(true);
    expect((await api.getCapability(id)).tool_identity).toBe("tools/search");
    await api.capabilityAction(otherId, "enable", "cap-enable-key");
    await api.capabilityAction(otherId, "disable", "cap-disable-key");
    expect(await api.listRuns()).toHaveLength(1);
    const runRequests = requests.filter((request) => new URL(request.url).pathname === "/v1/runs" && request.method === "GET");
    expect(runRequests.some((request) => new URL(request.url).searchParams.get("active") === "true")).toBe(true);
    expect((await api.listRunPage({ status: "failed", actor_id: otherId }, "older")).items).toHaveLength(1);
    const filteredRunUrl = new URL(requests.at(-1)?.url ?? "http://localhost");
    expect(filteredRunUrl.searchParams.get("status")).toBe("failed");
    expect(filteredRunUrl.searchParams.get("actor_id")).toBe(otherId);
    expect(filteredRunUrl.searchParams.get("cursor")).toBe("older");
    expect((await api.getRun(id)).status).toBe("succeeded");
    expect(await api.listRunEvents(id)).toEqual([]);
    const preflight = await api.preflight(otherId, { query: "status" });
    expect((await api.createRun(preflight, { query: "status" }, "run-key")).id).toBe(id);
    expect((await api.cancelRun(id, "cancel-key")).id).toBe(id);
    expect(await api.listAuditEvents({ resource_type: "run", resource_id: id, actor_id: otherId, action: "run.created", outcome: "succeeded", occurred_after: timestamp, occurred_before: "2026-09-07T12:00:00Z" })).toEqual({ items: [] });
    const auditQuery = new URL(requests.at(-1)?.url ?? "http://localhost").searchParams;
    expect(auditQuery.get("resource_type")).toBe("run");
    expect(auditQuery.get("resource_id")).toBe(id);
    expect(auditQuery.get("actor_id")).toBe(otherId);
    expect(auditQuery.get("action")).toBe("run.created");
    expect(auditQuery.get("outcome")).toBe("succeeded");
    expect(auditQuery.get("occurred_after")).toBe(timestamp);

    expect(requests.every((request) => request.headers.get("X-Workspace-ID") === id)).toBe(true);
    expect(
      requests
        .filter((request) => request.method === "POST" && request.url !== "http://localhost:8000/v1/registry/searches" && request.url !== "http://localhost:8000/v1/run-preflights")
        .every((request) => request.headers.has("Idempotency-Key")),
    ).toBe(true);
  });

  it("turns bounded API envelopes and network failures into useful errors", async () => {
    const api = createControlPlane({ identityId: "reviewer", workspaceId: id });
    failNext = true;
    await expect(api.listConnections()).rejects.toEqual(
      new ApiFailure("upstream_unavailable", "Registry unavailable.", otherId),
    );

    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(() =>
        Promise.resolve(new Response(null, { status: 502, statusText: "Bad Gateway" })),
      ),
    );
    const fallback = createControlPlane({ identityId: "reviewer", workspaceId: id });
    await expect(fallback.listConnections()).rejects.toEqual(
      new ApiFailure("http_502", "The control plane did not complete the request."),
    );
  });

  it("bounds connection pages and rejects a repeated collected cursor", async () => {
    let page = 0;
    vi.stubGlobal("fetch", vi.fn<typeof fetch>(() => {
      page += 1;
      return Promise.resolve(new Response(JSON.stringify({
        items: [{ ...connection, id: page === 1 ? id : otherId }],
        page: { next_cursor: page === 1 ? "next-page" : null },
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    }));
    const api = createControlPlane({ identityId: "reviewer", workspaceId: id });
    expect(await api.listConnections()).toHaveLength(1);
    expect((await api.listConnectionPage("next-page")).items).toHaveLength(1);

    page = 0;
    const runRequestUrls: string[] = [];
    vi.stubGlobal("fetch", vi.fn<typeof fetch>((input) => {
      page += 1;
      const request = input instanceof Request ? input : new Request(input);
      runRequestUrls.push(request.url);
      if (new URL(request.url).searchParams.get("active") === "true") {
        return Promise.resolve(new Response(JSON.stringify({ items: [], page: { next_cursor: null } }), { status: 200, headers: { "Content-Type": "application/json" } }));
      }
      return Promise.resolve(new Response(JSON.stringify({
        items: [run],
        page: { next_cursor: "older-runs" },
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    }));
    const runApi = createControlPlane({ identityId: "reviewer", workspaceId: id });
    const listedRuns = await runApi.listRuns();
    expect(runRequestUrls.filter((url) => new URL(url).searchParams.get("active") === "true")).toHaveLength(1);
    expect(listedRuns.map((item) => item.id)).toEqual([id]);
    expect(page).toBe(2);

    vi.stubGlobal("fetch", vi.fn<typeof fetch>(() => Promise.resolve(new Response(JSON.stringify({
      items: [connection], page: { next_cursor: "same" },
    }), { status: 200, headers: { "Content-Type": "application/json" } }))));
    const loopingApi = createControlPlane({ identityId: "reviewer", workspaceId: id });
    await expect(loopingApi.listRunEvents(id)).rejects.toThrow("repeated page cursor");
  });
});

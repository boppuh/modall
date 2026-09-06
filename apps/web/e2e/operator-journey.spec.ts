import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Route } from "@playwright/test";

const workspaceId = "11111111-1111-4111-8111-111111111111";
const connectionId = "22222222-2222-4222-8222-222222222222";
const capabilityId = "33333333-3333-4333-8333-333333333333";
const versionId = "44444444-4444-4444-8444-444444444444";
const runId = "55555555-5555-4555-8555-555555555555";
const timestamp = "2026-09-06T12:00:00Z";

const connection = {
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
const capability = {
  id: capabilityId,
  connection_id: connectionId,
  tool_identity: "tools/search",
  pending_version_id: null,
  enabled_version_id: versionId,
  status: "enabled",
  status_epoch: 3,
  created_at: timestamp,
};
const run = {
  id: runId,
  capability_id: capabilityId,
  capability_version_id: versionId,
  connection_id: connectionId,
  connection_version_id: versionId,
  status: "succeeded",
  arguments: { query: "status" },
  result: { matches: 17 },
  safe_error_code: null,
  cancellation_requested: false,
  deadline: "2026-09-06T12:05:00Z",
  created_at: timestamp,
  updated_at: timestamp,
  terminal_at: timestamp,
};

async function mockControlPlane(route: Route) {
  const request = route.request();
  const origin = request.headers().origin ?? "http://127.0.0.1:5173";
  const corsHeaders = {
    "access-control-allow-origin": origin,
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "content-type, idempotency-key, x-actor-user-id, x-workspace-id",
  };
  if (request.method() === "OPTIONS") {
    await route.fulfill({ status: 204, headers: corsHeaders });
    return;
  }
  const path = new URL(request.url()).pathname;
  const key = `${request.method()} ${path}`;
  let body: unknown;
  if (key === "GET /v1/session") {
    body = { workspace_id: workspaceId, actor_user_id: connectionId, role: "admin" };
  } else if (key === "GET /v1/server-connections") {
    body = { items: [connection], page: { next_cursor: null } };
  } else if (key === "POST /v1/server-connections") {
    body = connection;
  } else if (key === `GET /v1/server-connections/${connectionId}`) {
    body = { ...connection, versions: [{ id: versionId, sequence: 1, endpoint_url: "https://mcp.example/tools", secret_binding_id: null, policy_version: "v1", transport: "streamable_http", created_at: timestamp }], versions_truncated: false };
  } else if (key === "GET /v1/registry/entries") {
    body = { items: [], page: { next_cursor: null } };
  } else if (key === "GET /v1/capabilities") {
    body = { items: [capability], page: { next_cursor: null } };
  } else if (key === `GET /v1/capabilities/${capabilityId}`) {
    body = { ...capability, versions: [{ id: versionId, capability_id: capabilityId, sequence: 1, display_name: "Search", description: "Search public records", input_schema: { type: "object", properties: { query: { type: "string" } } }, output_schema: null, metadata_digest: "b".repeat(64), schema_supported: true, created_at: timestamp }], versions_truncated: false };
  } else if (key === "GET /v1/runs") {
    body = { items: [run], page: { next_cursor: null } };
  } else if (key === "POST /v1/runs") {
    body = run;
  } else if (key === `GET /v1/runs/${runId}`) {
    body = run;
  } else if (key === `GET /v1/runs/${runId}/events`) {
    body = { items: [{ id: connectionId, sequence: 1, event_type: "completed", status: "succeeded", safe_error_code: null, occurred_at: timestamp }], page: { next_cursor: null } };
  } else if (key === "POST /v1/run-preflights") {
    body = { capability_version_id: versionId, connection_version_id: versionId, argument_digest: "c".repeat(64), confirmation_token: "token", expires_at: new Date(Date.now() + 60_000).toISOString() };
  } else if (key === "GET /v1/audit-events") {
    body = { items: [], page: { next_cursor: null } };
  } else {
    throw new Error(`Unexpected control-plane request: ${key}`);
  }
  await route.fulfill({ status: 200, contentType: "application/json", headers: corsHeaders, body: JSON.stringify(body) });
}

test("operator can traverse the exact-version journey without accessibility violations", async ({ page }) => {
  await page.route("http://localhost:8000/v1/**", mockControlPlane);
  await page.goto("/");
  await page.getByLabel("Workspace UUID").fill(workspaceId);
  await page.getByRole("button", { name: /Enter control plane/ }).click();
  await expect(page.getByRole("heading", { name: "Registry overview" })).toBeVisible();

  await page.getByRole("button", { name: /Registry$/ }).click();
  await expect(page.getByRole("heading", { name: "Server registry" })).toBeVisible();
  await page.getByRole("button", { name: /Internal developer tools/ }).click();
  await expect(page.getByText("https://mcp.example/tools")).toBeVisible();

  await page.getByRole("button", { name: /Capabilities$/ }).click();
  await page.getByRole("button", { name: /tools\/search/ }).click();
  await expect(page.getByText("Search public records")).toBeVisible();

  await page.getByRole("button", { name: /Runs$/ }).click();
  await page.getByRole("button", { name: /55555555/ }).click();
  await expect(page.getByRole("heading", { name: "Execution timeline" })).toBeVisible();
  await expect(page.getByText("17")).toBeVisible();

  await page.keyboard.press("Tab");
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
});

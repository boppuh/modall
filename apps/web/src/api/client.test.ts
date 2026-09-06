import { describe, expect, it, vi } from "vitest";

import { createModallClient, workspaceQueryKey } from "./client";

describe("generated API client", () => {
  it("binds every request and query key to identity and workspace", async () => {
    const fetchImplementation = vi.fn<typeof fetch>((request) => {
      const headers = new Headers(request instanceof Request ? request.headers : undefined);
      expect(headers.get("X-Workspace-ID")).toBe("workspace-1");
      expect(headers.get("Authorization")).toBe("Bearer token-1");
      return Promise.resolve(
        new Response(JSON.stringify({ items: [], page: { next_cursor: null } }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    const context = {
      identityId: "identity-1",
      workspaceId: "workspace-1",
      accessToken: "token-1",
    };
    const client = createModallClient("https://modall.example", () => context, fetchImplementation);

    const response = await client.GET("/v1/server-connections");

    expect(response.error).toBeUndefined();
    expect(fetchImplementation).toHaveBeenCalledOnce();
    expect(workspaceQueryKey(context, "connections")).toEqual([
      "workspace",
      "identity-1",
      "workspace-1",
      "connections",
    ]);
  });

  it("supports local identity without manufacturing a bearer token", async () => {
    const fetchImplementation = vi.fn<typeof fetch>((request) => {
      const headers = new Headers(request instanceof Request ? request.headers : undefined);
      expect(headers.get("X-Workspace-ID")).toBe("local-workspace");
      expect(headers.has("Authorization")).toBe(false);
      return Promise.resolve(
        new Response(JSON.stringify({ items: [], page: { next_cursor: null } }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    const context = { identityId: "local", workspaceId: "local-workspace" };
    const client = createModallClient("http://localhost:8000", () => context, fetchImplementation);

    await client.GET("/v1/capabilities");

    expect(fetchImplementation).toHaveBeenCalledOnce();
    expect(createModallClient("http://localhost:8000", () => context)).toBeDefined();
  });
});

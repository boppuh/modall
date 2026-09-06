import { describe, expect, it } from "vitest";

import { clearSession, isWorkspaceId, loadSession, saveSession } from "./session";

const workspaceId = "11111111-1111-4111-8111-111111111111";

describe("workspace session", () => {
  it("persists valid local and OIDC sessions without widening identity scope", () => {
    const storage = new StorageFixture();
    const local = { identityId: "local", workspaceId, workspaceLabel: "Pilot" };
    saveSession(local, storage);
    expect(loadSession(storage)).toEqual(local);

    const oidc = { ...local, accessToken: "token" };
    saveSession(oidc, storage);
    expect(loadSession(storage)).toEqual(local);
    expect(storage.getItem("modall.workspace-session.v1")).not.toContain("token");
    clearSession(storage);
    expect(loadSession(storage)).toBeNull();
  });

  it("rejects malformed identifiers and stored values", () => {
    const storage = new StorageFixture();
    expect(isWorkspaceId(workspaceId)).toBe(true);
    expect(isWorkspaceId("not-a-workspace")).toBe(false);
    storage.setItem("modall.workspace-session.v1", "not-json");
    expect(loadSession(storage)).toBeNull();
    storage.setItem("modall.workspace-session.v1", JSON.stringify({ workspaceId: "bad" }));
    expect(loadSession(storage)).toBeNull();
    storage.setItem(
      "modall.workspace-session.v1",
      JSON.stringify({ identityId: 4, workspaceId, workspaceLabel: "Pilot", accessToken: "old" }),
    );
    expect(loadSession(storage)).toBeNull();
  });
});

class StorageFixture implements Storage {
  private readonly values = new Map<string, string>();
  get length() { return this.values.size; }
  clear() { this.values.clear(); }
  getItem(key: string) { return this.values.get(key) ?? null; }
  key(index: number) { return [...this.values.keys()][index] ?? null; }
  removeItem(key: string) { this.values.delete(key); }
  setItem(key: string, value: string) { this.values.set(key, value); }
}

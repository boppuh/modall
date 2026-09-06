import type { IdentityContext } from "./api/client";

export interface WorkspaceSession extends IdentityContext {
  workspaceLabel: string;
}

const storageKey = "modall.workspace-session.v1";
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function isWorkspaceId(value: string): boolean {
  return uuidPattern.test(value);
}

export function loadSession(storage: Storage = localStorage): WorkspaceSession | null {
  const raw = storage.getItem(storageKey);
  if (raw === null) return null;
  try {
    const parsed = JSON.parse(raw) as Partial<WorkspaceSession>;
    if (
      typeof parsed.identityId !== "string" ||
      typeof parsed.workspaceId !== "string" ||
      typeof parsed.workspaceLabel !== "string" ||
      !isWorkspaceId(parsed.workspaceId)
    ) {
      return null;
    }
    return {
      identityId: parsed.identityId,
      workspaceId: parsed.workspaceId,
      workspaceLabel: parsed.workspaceLabel,
    };
  } catch {
    return null;
  }
}

export function saveSession(session: WorkspaceSession, storage: Storage = localStorage): void {
  storage.setItem(
    storageKey,
    JSON.stringify({
      identityId: session.identityId,
      workspaceId: session.workspaceId,
      workspaceLabel: session.workspaceLabel,
    }),
  );
}

export function clearSession(storage: Storage = localStorage): void {
  storage.removeItem(storageKey);
}

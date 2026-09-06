import createClient from "openapi-fetch";

import type { paths } from "./schema";

export interface IdentityContext {
  identityId: string;
  workspaceId: string;
  accessToken?: string;
}

export type IdentityContextProvider = () => IdentityContext;

export function createModallClient(
  baseUrl: string,
  contextProvider: IdentityContextProvider,
  fetchImplementation: typeof fetch = fetch,
) {
  const client = createClient<paths>({ baseUrl, fetch: fetchImplementation });
  client.use({
    onRequest({ request }) {
      const context = contextProvider();
      request.headers.set("X-Workspace-ID", context.workspaceId);
      if (context.accessToken) {
        request.headers.set("Authorization", `Bearer ${context.accessToken}`);
      }
      return request;
    },
  });
  return client;
}

export function workspaceQueryKey(
  context: IdentityContext,
  ...resource: readonly unknown[]
): readonly unknown[] {
  return ["workspace", context.identityId, context.workspaceId, ...resource];
}

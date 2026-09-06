import type { Client } from "openapi-fetch";

import { createModallClient, type IdentityContext } from "./client";
import type { components, paths } from "./schema";

type Schemas = components["schemas"];
export type Connection = Schemas["ConnectionResponse"];
export type ConnectionDetail = Schemas["ConnectionDetailResponse"];
export type ConnectionVersion = Schemas["ConnectionVersionResponse"];
export type Capability = Schemas["CapabilityResponse"];
export type CapabilityStatus = "pending_review" | "enabled" | "disabled" | "unavailable";
export type CapabilityDetail = Schemas["CapabilityDetailResponse"];
export type CapabilityVersion = Schemas["CapabilityVersionResponse"];
export type RegistryEntry = Schemas["RegistryEntryResponse"];
export type RegistrySearch = Schemas["RegistrySearchResponse"];
export type Run = Schemas["RunResponse"];
export type RunEvent = Schemas["RunEventResponse"];
export type RunPreflight = Schemas["RunPreflightResponse"];
export type AuditEvent = Schemas["AuditEventResponse"];
export type AuditFilters = Omit<NonNullable<paths["/v1/audit-events"]["get"]["parameters"]["query"]>, "cursor" | "limit">;
export type EffectiveSession = Schemas["SessionResponse"];

export class ApiFailure extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly correlationId?: string,
  ) {
    super(message);
  }
}

type ApiResult<T> = {
  data?: T;
  error?: Schemas["ErrorResponse"];
  response: Response;
};

async function unwrap<T>(pending: Promise<ApiResult<T>>): Promise<T> {
  const result = await pending;
  if (result.data !== undefined) return result.data;
  const error = result.error as
    | { error?: { code?: string; message?: string }; correlation_id?: string }
    | undefined;
  throw new ApiFailure(
    error?.error?.code ?? `http_${result.response.status}`,
    error?.error?.message ?? "The control plane did not complete the request.",
    error?.correlation_id,
  );
}

type Page<T> = { items: T[]; page: { next_cursor?: string | null } };

async function collectPages<T>(fetchPage: (cursor?: string) => Promise<Page<T>>): Promise<T[]> {
  const items: T[] = [];
  const seen = new Set<string>();
  let cursor: string | undefined;
  do {
    const page = await fetchPage(cursor);
    items.push(...page.items);
    const next = page.page.next_cursor ?? undefined;
    if (next && seen.has(next)) throw new Error("The control plane returned a repeated page cursor.");
    if (next) seen.add(next);
    cursor = next;
  } while (cursor);
  return items;
}

export interface OverviewData {
  connections: Connection[];
  capabilities: Capability[];
  runs: Run[];
}

export interface ControlPlane {
  currentSession(): Promise<EffectiveSession>;
  overview(): Promise<OverviewData>;
  listConnections(): Promise<Connection[]>;
  getConnection(id: string): Promise<ConnectionDetail>;
  createConnection(input: { name: string; endpointUrl: string; secretBindingId?: string }, idempotencyKey: string): Promise<Connection>;
  appendConnectionVersion(id: string, input: { endpointUrl: string; secretBindingId?: string }, idempotencyKey: string): Promise<ConnectionVersion>;
  connectionAction(id: string, action: "verify" | "refresh" | "enable" | "disable", idempotencyKey: string): Promise<void>;
  searchRegistry(query: string): Promise<RegistrySearch>;
  importRegistry(cacheId: string, provenanceDigest: string, idempotencyKey: string): Promise<RegistryEntry>;
  listRegistryEntries(): Promise<RegistryEntry[]>;
  listCapabilities(status?: CapabilityStatus): Promise<Capability[]>;
  getCapability(id: string): Promise<CapabilityDetail>;
  capabilityAction(versionId: string, action: "enable" | "disable", idempotencyKey: string): Promise<Capability>;
  listRuns(): Promise<Run[]>;
  getRun(id: string): Promise<Run>;
  listRunEvents(id: string): Promise<RunEvent[]>;
  preflight(versionId: string, argumentsValue: Record<string, unknown>): Promise<RunPreflight>;
  createRun(preflight: RunPreflight, argumentsValue: Record<string, unknown>, idempotencyKey: string): Promise<Run>;
  cancelRun(id: string, idempotencyKey: string): Promise<Run>;
  listAuditEvents(filters?: AuditFilters, cursor?: string): Promise<{ items: AuditEvent[]; nextCursor?: string }>;
}

class GeneratedControlPlane implements ControlPlane {
  constructor(private readonly client: Client<paths>) {}

  currentSession(): Promise<EffectiveSession> {
    return unwrap(this.client.GET("/v1/session"));
  }

  async overview(): Promise<OverviewData> {
    const [connections, capabilities, runs] = await Promise.all([
      this.listConnections(),
      this.listCapabilities(),
      this.listRuns(),
    ]);
    return { connections, capabilities, runs };
  }

  async listConnections(): Promise<Connection[]> {
    return collectPages((cursor) => unwrap(this.client.GET("/v1/server-connections", { params: { query: { limit: 100, cursor } } })));
  }

  getConnection(id: string): Promise<ConnectionDetail> {
    return unwrap(
      this.client.GET("/v1/server-connections/{connection_id}", {
        params: { path: { connection_id: id } },
      }),
    );
  }

  createConnection(input: { name: string; endpointUrl: string; secretBindingId?: string }, idempotencyKey: string): Promise<Connection> {
    return unwrap(
      this.client.POST("/v1/server-connections", {
        params: { header: { "Idempotency-Key": idempotencyKey } },
        body: { name: input.name, endpoint_url: input.endpointUrl, secret_binding_id: input.secretBindingId, policy_version: "v1" },
      }),
    );
  }

  appendConnectionVersion(id: string, input: { endpointUrl: string; secretBindingId?: string }, idempotencyKey: string): Promise<ConnectionVersion> {
    return unwrap(this.client.POST("/v1/server-connections/{connection_id}/versions", {
      params: { path: { connection_id: id }, header: { "Idempotency-Key": idempotencyKey } },
      body: { endpoint_url: input.endpointUrl, secret_binding_id: input.secretBindingId, policy_version: "v1" },
    }));
  }

  async connectionAction(
    id: string,
    action: "verify" | "refresh" | "enable" | "disable", idempotencyKey: string,
  ): Promise<void> {
    const options = {
      params: {
        path: { connection_id: id },
        header: { "Idempotency-Key": idempotencyKey },
      },
    } as const;
    if (action === "verify") {
      await unwrap(this.client.POST("/v1/server-connections/{connection_id}/verify", options));
    } else if (action === "refresh") {
      await unwrap(this.client.POST("/v1/server-connections/{connection_id}/refresh", options));
    } else if (action === "enable") {
      await unwrap(this.client.POST("/v1/server-connections/{connection_id}/enable", options));
    } else {
      await unwrap(this.client.POST("/v1/server-connections/{connection_id}/disable", options));
    }
  }

  searchRegistry(query: string): Promise<RegistrySearch> {
    return unwrap(this.client.POST("/v1/registry/searches", { body: { query } }));
  }

  importRegistry(cacheId: string, provenanceDigest: string, idempotencyKey: string): Promise<RegistryEntry> {
    return unwrap(
      this.client.POST("/v1/registry/imports", {
        params: { header: { "Idempotency-Key": idempotencyKey } },
        body: { cache_id: cacheId, provenance_digest: provenanceDigest },
      }),
    );
  }

  async listRegistryEntries(): Promise<RegistryEntry[]> {
    return collectPages((cursor) => unwrap(this.client.GET("/v1/registry/entries", { params: { query: { limit: 100, cursor } } })));
  }

  async listCapabilities(status?: CapabilityStatus): Promise<Capability[]> {
    return collectPages((cursor) => unwrap(this.client.GET("/v1/capabilities", { params: { query: { limit: 100, cursor, status } } })));
  }

  getCapability(id: string): Promise<CapabilityDetail> {
    return unwrap(
      this.client.GET("/v1/capabilities/{capability_id}", {
        params: { path: { capability_id: id } },
      }),
    );
  }

  capabilityAction(versionId: string, action: "enable" | "disable", idempotencyKey: string): Promise<Capability> {
    const options = {
      params: {
        path: { capability_version_id: versionId },
        header: { "Idempotency-Key": idempotencyKey },
      },
    } as const;
    return action === "enable"
      ? unwrap(this.client.POST("/v1/capability-versions/{capability_version_id}/enable", options))
      : unwrap(this.client.POST("/v1/capability-versions/{capability_version_id}/disable", options));
  }

  async listRuns(): Promise<Run[]> {
    const [recent, ...activePages] = await Promise.all([
      unwrap(this.client.GET("/v1/runs", { params: { query: { limit: 100 } } })),
      ...["queued", "preparing", "session_fenced", "dispatch_fenced"].map((status) =>
        collectPages((cursor) => unwrap(this.client.GET("/v1/runs", { params: { query: { limit: 100, cursor, status } } }))),
      ),
    ]);
    const byId = new Map(recent.items.map((run) => [run.id, run]));
    for (const run of activePages.flat()) byId.set(run.id, run);
    return [...byId.values()].sort((left, right) => right.created_at.localeCompare(left.created_at));
  }

  getRun(id: string): Promise<Run> {
    return unwrap(this.client.GET("/v1/runs/{run_id}", { params: { path: { run_id: id } } }));
  }

  async listRunEvents(id: string): Promise<RunEvent[]> {
    return collectPages((cursor) => unwrap(this.client.GET("/v1/runs/{run_id}/events", { params: { path: { run_id: id }, query: { limit: 100, cursor } } })));
  }

  preflight(
    versionId: string,
    argumentsValue: Record<string, unknown>,
  ): Promise<RunPreflight> {
    return unwrap(
      this.client.POST("/v1/run-preflights", {
        body: { capability_version_id: versionId, arguments: argumentsValue },
      }),
    );
  }

  createRun(preflight: RunPreflight, argumentsValue: Record<string, unknown>, idempotencyKey: string): Promise<Run> {
    return unwrap(
      this.client.POST("/v1/runs", {
        params: { header: { "Idempotency-Key": idempotencyKey } },
        body: {
          capability_version_id: preflight.capability_version_id,
          arguments: argumentsValue,
          confirmation_token: preflight.confirmation_token,
        },
      }),
    );
  }

  cancelRun(id: string, idempotencyKey: string): Promise<Run> {
    return unwrap(
      this.client.POST("/v1/runs/{run_id}/cancel", {
        params: {
          path: { run_id: id },
          header: { "Idempotency-Key": idempotencyKey },
        },
      }),
    );
  }

  async listAuditEvents(filters: AuditFilters = {}, cursor?: string): Promise<{ items: AuditEvent[]; nextCursor?: string }> {
    const page = await unwrap(this.client.GET("/v1/audit-events", {
      params: { query: { limit: 100, cursor, ...filters } },
    }));
    return { items: page.items, ...(page.page.next_cursor ? { nextCursor: page.page.next_cursor } : {}) };
  }
}

export function createControlPlane(context: IdentityContext): ControlPlane {
  const configuredBaseUrl = (import.meta.env as { VITE_API_BASE_URL?: string }).VITE_API_BASE_URL;
  const baseUrl = configuredBaseUrl ?? "http://localhost:8000";
  return new GeneratedControlPlane(createModallClient(baseUrl, () => context));
}

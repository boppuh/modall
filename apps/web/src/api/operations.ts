import type { Client } from "openapi-fetch";

import { createModallClient, type IdentityContext } from "./client";
import type { components, paths } from "./schema";

type Schemas = components["schemas"];
export type Connection = Schemas["ConnectionResponse"];
export type ConnectionDetail = Schemas["ConnectionDetailResponse"];
export type Capability = Schemas["CapabilityResponse"];
export type CapabilityDetail = Schemas["CapabilityDetailResponse"];
export type CapabilityVersion = Schemas["CapabilityVersionResponse"];
export type RegistryEntry = Schemas["RegistryEntryResponse"];
export type RegistrySearch = Schemas["RegistrySearchResponse"];
export type Run = Schemas["RunResponse"];
export type RunEvent = Schemas["RunEventResponse"];
export type RunPreflight = Schemas["RunPreflightResponse"];

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

function mutationKey(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}

export interface OverviewData {
  connections: Connection[];
  capabilities: Capability[];
  runs: Run[];
}

export interface ControlPlane {
  overview(): Promise<OverviewData>;
  listConnections(): Promise<Connection[]>;
  getConnection(id: string): Promise<ConnectionDetail>;
  createConnection(input: { name: string; endpointUrl: string }): Promise<Connection>;
  connectionAction(id: string, action: "verify" | "refresh" | "enable" | "disable"): Promise<void>;
  searchRegistry(query: string): Promise<RegistrySearch>;
  importRegistry(cacheId: string, provenanceDigest: string): Promise<RegistryEntry>;
  listRegistryEntries(): Promise<RegistryEntry[]>;
  listCapabilities(): Promise<Capability[]>;
  getCapability(id: string): Promise<CapabilityDetail>;
  capabilityAction(versionId: string, action: "enable" | "disable"): Promise<Capability>;
  listRuns(): Promise<Run[]>;
  getRun(id: string): Promise<Run>;
  listRunEvents(id: string): Promise<RunEvent[]>;
  preflight(versionId: string, argumentsValue: Record<string, unknown>): Promise<RunPreflight>;
  createRun(preflight: RunPreflight, argumentsValue: Record<string, unknown>): Promise<Run>;
  cancelRun(id: string): Promise<Run>;
}

class GeneratedControlPlane implements ControlPlane {
  constructor(private readonly client: Client<paths>) {}

  async overview(): Promise<OverviewData> {
    const [connections, capabilities, runs] = await Promise.all([
      this.listConnections(),
      this.listCapabilities(),
      this.listRuns(),
    ]);
    return { connections, capabilities, runs };
  }

  async listConnections(): Promise<Connection[]> {
    return (await unwrap(this.client.GET("/v1/server-connections"))).items;
  }

  getConnection(id: string): Promise<ConnectionDetail> {
    return unwrap(
      this.client.GET("/v1/server-connections/{connection_id}", {
        params: { path: { connection_id: id } },
      }),
    );
  }

  createConnection(input: { name: string; endpointUrl: string }): Promise<Connection> {
    return unwrap(
      this.client.POST("/v1/server-connections", {
        params: { header: { "Idempotency-Key": mutationKey("connection") } },
        body: { name: input.name, endpoint_url: input.endpointUrl, policy_version: "v1" },
      }),
    );
  }

  async connectionAction(
    id: string,
    action: "verify" | "refresh" | "enable" | "disable",
  ): Promise<void> {
    const options = {
      params: {
        path: { connection_id: id },
        header: { "Idempotency-Key": mutationKey(action) },
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

  importRegistry(cacheId: string, provenanceDigest: string): Promise<RegistryEntry> {
    return unwrap(
      this.client.POST("/v1/registry/imports", {
        params: { header: { "Idempotency-Key": mutationKey("import") } },
        body: { cache_id: cacheId, provenance_digest: provenanceDigest },
      }),
    );
  }

  async listRegistryEntries(): Promise<RegistryEntry[]> {
    return (await unwrap(this.client.GET("/v1/registry/entries"))).items;
  }

  async listCapabilities(): Promise<Capability[]> {
    return (await unwrap(this.client.GET("/v1/capabilities"))).items;
  }

  getCapability(id: string): Promise<CapabilityDetail> {
    return unwrap(
      this.client.GET("/v1/capabilities/{capability_id}", {
        params: { path: { capability_id: id } },
      }),
    );
  }

  capabilityAction(versionId: string, action: "enable" | "disable"): Promise<Capability> {
    const options = {
      params: {
        path: { capability_version_id: versionId },
        header: { "Idempotency-Key": mutationKey(`capability-${action}`) },
      },
    } as const;
    return action === "enable"
      ? unwrap(this.client.POST("/v1/capability-versions/{capability_version_id}/enable", options))
      : unwrap(this.client.POST("/v1/capability-versions/{capability_version_id}/disable", options));
  }

  async listRuns(): Promise<Run[]> {
    return (await unwrap(this.client.GET("/v1/runs"))).items;
  }

  getRun(id: string): Promise<Run> {
    return unwrap(this.client.GET("/v1/runs/{run_id}", { params: { path: { run_id: id } } }));
  }

  async listRunEvents(id: string): Promise<RunEvent[]> {
    return (
      await unwrap(
        this.client.GET("/v1/runs/{run_id}/events", { params: { path: { run_id: id } } }),
      )
    ).items;
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

  createRun(preflight: RunPreflight, argumentsValue: Record<string, unknown>): Promise<Run> {
    return unwrap(
      this.client.POST("/v1/runs", {
        params: { header: { "Idempotency-Key": mutationKey("run") } },
        body: {
          capability_version_id: preflight.capability_version_id,
          arguments: argumentsValue,
          confirmation_token: preflight.confirmation_token,
        },
      }),
    );
  }

  cancelRun(id: string): Promise<Run> {
    return unwrap(
      this.client.POST("/v1/runs/{run_id}/cancel", {
        params: {
          path: { run_id: id },
          header: { "Idempotency-Key": mutationKey("cancel") },
        },
      }),
    );
  }
}

export function createControlPlane(context: IdentityContext): ControlPlane {
  const configuredBaseUrl = (import.meta.env as { VITE_API_BASE_URL?: string }).VITE_API_BASE_URL;
  const baseUrl = configuredBaseUrl ?? "http://localhost:8000";
  return new GeneratedControlPlane(createModallClient(baseUrl, () => context));
}

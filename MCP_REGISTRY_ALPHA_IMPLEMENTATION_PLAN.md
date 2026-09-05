# Modall Milestone 1 — MCP Registry Alpha Implementation Plan

**Status:** Proposed for execution  
**Release:** `v0.1.0` closed operator alpha  
**Parent roadmap:** `IMPLEMENTATION_PLAN.md`  
**Target:** 22–28 person-weeks; approximately 8–10 elapsed weeks with three focused engineers

---

## 1. Release decision

Build a closed MCP registry and basic operator UI as the first usable slice of Modall.

The milestone is successful when an internal operator can:

1. search the official MCP Registry or enter a remote endpoint;
2. create and verify a connection using an optional secret-manager binding;
3. discover the server's tools;
4. inspect immutable capability and schema versions;
5. enable a reviewed tool;
6. invoke it with public, synthetic, or otherwise non-confidential input; and
7. inspect the exact version, timing, result, error, and audit history.

Reference journey:

```text
find or enter server
  -> configure connection
  -> verify and discover
  -> inspect immutable tool versions
  -> enable a tool
  -> validate and confirm an invocation
  -> inspect the durable run timeline
```

This is a foundation for comparison and routing. Catalog breadth, marketplace activity, and model training are not release-success measures.

## 2. Scope and threat model

### 2.1 Release assumptions

- Closed alpha for pre-provisioned internal users.
- One production workspace initially; the data model remains workspace-scoped.
- Single-region deployment.
- Curated remote MCP servers over Streamable HTTP.
- Public, synthetic, or explicitly non-confidential invocation inputs only.
- Text and structured JSON results only.
- Remote endpoints and metadata are untrusted.
- MCP credentials and application identity tokens are confidential.
- No regulated-deletion, enterprise revocation-latency, or availability commitment.

These assumptions are enforced in product copy, API validation, operator training, and release tests. Changing one requires threat-model review and re-estimation.

### 2.2 In scope

#### Registry and discovery

- Manual registration of an HTTPS remote MCP endpoint.
- Optional static credential binding through the deployment secret provider.
- Read-only search and metadata import from the official MCP Registry.
- Explicit verification and discovery of a configured endpoint.
- One pinned MCP SDK and protocol contract selected by ADR.
- Paginated tool discovery with strict page, item, byte, and time limits.
- Immutable discovery snapshots, capability versions, and MCP tool bindings.
- Connection states: `draft`, `verifying`, `active`, `degraded`, and `disabled`.
- Capability states: `pending_review`, `enabled`, `disabled`, and `unavailable`.
- Explicit refresh plus a conservative scheduled refresh for active connections.

#### Invocation

- Server-side JSON Schema validation without fetching external references.
- Bounded obvious-secret detection before arguments are persisted or sent.
- Explicit operator confirmation against an exact capability version.
- PostgreSQL-backed asynchronous jobs.
- Durable run, attempt, event, and correlation history.
- One dispatch fence and no automatic invocation retry after that fence.
- Deadline, cancellation request, input limit, and result limit.
- Text and structured JSON result capture; every other content kind fails as unsupported without durable payload retention.
- Stable local error codes with bounded sanitized upstream detail.

#### Operator UI

- Authenticated application shell and role-aware navigation.
- Overview with connection, capability, and run status.
- Registry search/import.
- Connection list, create, detail, verify, refresh, enable, and disable.
- Capability list and immutable version/schema detail.
- JSON invocation playground with validation and confirmation.
- Run list and diagnostic timeline.
- Loading, empty, stale, degraded, authorization, validation, timeout, cancellation, indeterminate, and failure states.
- Keyboard and screen-reader support for the reference journey.

#### Operations

- OIDC in deployed environments and explicit local-development authentication.
- Admin, Operator, and Viewer roles.
- Structured audit events, logs, metrics, and distributed traces.
- Local Docker Compose environment.
- Reproducible deployment manifests.
- Forward database migrations and backup/restore qualification.
- Release, rollback, endpoint-disable, credential-rotation, and upstream-outage runbooks.

### 2.3 Explicitly out of scope

- Task-aware routing, comparisons, grading, outcomes, or leaderboards.
- Private source-code processing or confidential invocation payloads.
- Large, binary, image, audio, archive, or multimodal artifacts.
- Artifact uploads, download grants, chunk streaming, OCR, or MIME-specific inspection.
- Per-record cryptographic erasure or deletion evidence across backups.
- Continuous identity-provider authorization refresh during a request or stream.
- Interactive OAuth, user-consent flows, SCIM, or workspace administration UI.
- Hosted `stdio`, package installation, containers, or arbitrary commands.
- MCP prompts, resources, sampling, elicitation, roots, apps, or task extensions.
- Multiple protocol revisions unless a second revision is approved without moving the release range.
- Public publishing, seller onboarding, billing, settlement, or monetary quotas.
- Zero-downtime mixed-version database compatibility.

## 3. Users and permissions

### 3.1 Roles

| Action | Admin | Operator | Viewer |
|---|---:|---:|---:|
| View registry, connections, capabilities, and runs | Yes | Yes | Yes |
| View workspace audit history | Yes | Yes | No |
| Search/import registry metadata | Yes | Yes | No |
| Create or change endpoint/secret binding | Yes | No | No |
| Verify or refresh a connection | Yes | Yes | No |
| Enable or disable a capability | Yes | Yes | No |
| Invoke or cancel a run | Yes | Yes | No |
| Disable a connection during an incident | Yes | Yes | No |

Membership is maintained in Modall for this alpha. Every API request validates the signed login/session and current local membership. Run dispatch rechecks the current local role and workspace membership of the originating actor. Enterprise identity-source propagation guarantees are deferred until that promotion trigger is accepted.

### 3.2 Required scenarios

- **Manual connection:** Admin configures a curated endpoint; Operator verifies, reviews, and enables a discovered tool.
- **Registry import:** Operator imports public metadata, then Admin independently configures an executable endpoint and optional credential.
- **Schema drift:** Refresh creates a new pending capability version without mutating the enabled historical version.
- **Safe invocation:** Operator validates and confirms non-confidential JSON, receives one durable run, and inspects the result.
- **Failure diagnosis:** Operator distinguishes validation, connection, timeout, upstream, cancellation, and indeterminate outcomes from the UI.
- **Incident disable:** Operator disables a connection; queued unfenced runs stop and later invocations are rejected.

## 4. Release gates

### 4.1 Functional

- All required scenarios pass against the reference fixture server.
- Manual and imported metadata remain distinct from executable connection configuration.
- Refresh never mutates a prior discovery snapshot or capability version.
- A run always identifies the exact capability, connection, protocol, and schema version used.
- Unsupported result content is rejected without storing its payload.

### 4.2 Security

- Cross-workspace repository and API tests fail closed.
- The worker cannot reach loopback, link-local, private-network, cloud-metadata, or other denied destinations in deployed mode.
- Invocation redirects are never followed.
- Secrets appear only in the configured secret provider and transient transport memory.
- Arguments, results, remote metadata, and upstream errors are absent from logs, traces, and audit records.
- Untrusted text/JSON renders as inert content and cannot execute active markup.
- Obvious credential-shaped arguments are rejected before persistence and send.

### 4.3 Reliability

- A worker crash before the dispatch fence safely retries the job.
- A crash or ambiguous transport failure after the fence produces `indeterminate` and never automatically invokes again.
- Disabling a connection prevents every queued unfenced call.
- Concurrent refreshes elect one current snapshot without overwriting history.
- Backup restoration reproduces registry identity, enabled-state projection, run history, and audit history.

### 4.4 Usability

- A new internal operator completes the reference journey without database or command-line access.
- Confirmation clearly identifies endpoint, capability version, argument summary, and non-confidential-data restriction.
- Timeline language explains whether a retry is safe.
- The reference journey passes keyboard and screen-reader review.

### 4.5 Release decision

The engineering owner, product owner, and security reviewer sign the functional, security, reliability, and usability evidence. Deferred enterprise controls are not silently converted into blockers unless the release assumptions changed.

## 5. Architecture

### 5.1 Deployment shape

```text
Browser
  |
Web application
  |
Control-plane API -------- PostgreSQL
  |                            |
  |                        durable jobs
  |                            |
  +------------------------- Worker
                                 |
                         outbound network policy
                                 |
                         curated MCP servers
```

Supporting dependencies:

- deployment OIDC provider;
- deployment secret provider;
- OpenTelemetry collector; and
- the official Registry through one bounded adapter.

Do not add Redis, Kafka, Temporal, a service mesh, or independent domain services without a measured requirement.

### 5.2 Suggested stack

- API and worker: Python 3.13, FastAPI, SQLAlchemy, Alembic, Pydantic.
- Database/jobs: PostgreSQL with `FOR UPDATE SKIP LOCKED` leasing.
- Web: TypeScript, React, Vite, TanStack Query, generated OpenAPI client.
- MCP: official Tier 1 SDK behind a local `McpClientAdapter`.
- Tests: pytest, Testcontainers, Playwright, and fixture MCP servers.

An ADR may change a choice before implementation. Public and domain contracts must not expose framework or SDK types.

### 5.3 Modules

- `identity`: principals, workspaces, memberships, roles.
- `registry`: entries, connections, discovery snapshots, capabilities, versions.
- `execution`: jobs, runs, attempts, events, idempotency.
- `mcp_adapter`: transport, protocol initialization, discovery, invocation.
- `audit`: typed security and operator events.
- `api`: HTTP contracts and authorization.
- `web`: operator workflows.
- `ops`: telemetry, retention jobs, health, and runbooks.

## 6. Domain and persistence

### 6.1 Core tables

| Table | Purpose |
|---|---|
| `users`, `workspaces`, `workspace_memberships` | Internal identity and current role |
| `secret_bindings` | Opaque provider/reference/version metadata; never secret material |
| `registry_entries`, `registry_entry_versions` | Imported or manual catalog metadata and provenance |
| `server_connections`, `server_connection_versions` | Stable connection identity, current verified version, and monotonic refresh generation plus immutable endpoint/credential configuration |
| `discovery_snapshots` | Immutable bounded normalized discovery payload, canonical digest, and publishing refresh generation |
| `capabilities`, `capability_versions` | Stable logical tool identity and immutable schema/metadata version |
| `mcp_tool_bindings` | Exact capability-version to connection-version/tool/protocol binding |
| `capability_status_events` | Append-only enable/disable/unavailable decisions |
| `jobs` | Durable leased work with a monotonically increasing `lease_epoch` on every claim/reclaim |
| `runs`, `run_attempts`, `run_events` | Public execution identity, exact lineage, state, timing, and bounded content |
| `idempotency_records` | Workspace/actor/route-scoped HMAC of caller key, versioned server-HMAC of the canonical request, and original resource reference |
| `audit_events` | Typed actor/action/resource/outcome/correlation evidence without payload content |

### 6.2 Invariants

- Every tenant-owned row has `workspace_id`; repositories require workspace context.
- IDs are server-generated UUIDs and do not encode tenant or content.
- A connection version is immutable and pins endpoint, secret-binding version, transport, and policy.
- A capability version is immutable and pins schemas plus exactly one MCP tool binding.
- An MCP tool binding is executable only while its connection version is the connection's exact current verified version and its observed tool identity/schema remains present in the current published discovery snapshot. Connection-version replacement or schema/tool drift transitions every superseded binding out of executable state before new admission or fencing.
- Refresh appends a discovery snapshot and any changed capability versions.
- Every verification/refresh job receives a monotonically increasing connection-scoped generation; only the latest-started generation may publish a snapshot or health projection.
- A capability status projection changes through append-only events.
- A run pins one capability version before enqueue and never changes target.
- Each run has at most one active attempt and one dispatch fence.
- Every job claim increments `lease_epoch`; only the current unexpired lease owner presenting that exact epoch may create the dispatch fence, so a reclaimed stale worker cannot send.
- Terminal run states are immutable except for separately appended reconciliation detail.
- Mutating API idempotency is unique by workspace, actor, method, route, and HMAC-derived key. A separate versioned server-HMAC covers the canonical capability/connection/request body; matching reuse replays the original resource and mismatching reuse returns `idempotency_conflict`, including after argument content expires.
- Audit rows contain typed identifiers/enums, not arguments, results, schemas, secrets, or raw upstream errors.

### 6.3 Retention

- Retained arguments and any retained result/error content for every terminal run state—including succeeded, failed, timed out, cancelled, and indeterminate—expire after 14 days by default.
- Run status, timing, exact version lineage, and safe error code: 90 days.
- Discovery snapshots and capability versions: retained while referenced by an enabled capability or retained run, then eligible for deletion.
- Audit events: 180 days for the alpha.
- Registry search cache: at most one hour.

Deletion is ordinary database/object deletion under managed encryption-at-rest guarantees. The alpha makes no cryptographic-erasure or backup-purge promise. That distinction appears in operator documentation.

## 7. Remote endpoint and content safety

### 7.1 Endpoint policy

- Deployed connections require HTTPS and an explicit host.
- Userinfo, fragments, non-default schemes, malformed hosts, and overlong URLs are rejected.
- DNS resolution and connection traffic pass through deployment network controls that deny loopback, link-local, private, multicast, reserved, and cloud-metadata destinations.
- Redirects are disabled for verification, discovery, and invocation in `v0.1.0`.
- The worker has no ambient access to the database admin interface, secret-provider control plane, or internal service network beyond required destinations.
- Local-development HTTP is limited to explicit loopback fixture configuration and cannot be enabled in deployed mode.

### 7.2 Untrusted metadata and schemas

- Bound decoded and raw bytes, nesting, string length, property count, tool count, page count, and total discovery time.
- Store the exact normalized JSON needed for history only after the complete discovery snapshot passes bounds, schema handling, and secret screening; do not log payload bodies.
- Preserve local JSON Schema references but never fetch an external schema reference.
- Compile `pattern`, `patternProperties`, and other schema regexes only through the approved linear-time/RE2-compatible subset; unsupported constructs keep the version visible but non-invocable. Run schema compilation and validation under an independent wall-clock deadline and resource limit, with timeout failing closed.
- Apply obvious-secret screening to remote descriptive fields before any snapshot payload write. A match, timeout, or scanner failure discards raw and normalized payload bytes and persists only safe failure code/timing/connection/generation metadata; no secret-bearing quarantine payload or content digest is retained.
- Render every remote string as text and every schema/result through inert viewers.

### 7.3 Arguments and results

- Arguments must validate against the pinned input schema and configured byte/depth limits.
- Confirmation repeats schema identity and the non-confidential-data restriction.
- Obvious-secret screening runs before durable argument storage and enqueue. A match, timeout, or scanner failure creates no run, stores no argument payload or content digest, and sends nothing upstream.
- Text and structured JSON results remain only in bounded worker memory until byte/depth checks and obvious-secret screening finish. When the pinned capability version declares an output schema, structured JSON must also validate against that exact schema before any durable payload write.
- Only a fully accepted result may be written to `runs`, `run_attempts`, `run_events`, or result storage. Scanner failure, a detected secret, limit failure, or schema mismatch persists only safe quarantine/error metadata and no raw result payload in durable storage or APIs; schema mismatch uses `invalid_upstream_output`.
- Unsupported content blocks, embedded resources, and binary bytes are discarded and reported with `unsupported_result_content`.

The scanner is a guardrail against accidental credentials, not a claim of comprehensive PII or confidential-data classification.

## 8. MCP behavior

### 8.1 Protocol qualification

ADR-002 selects one exact protocol revision and official SDK version after fixture qualification. The adapter records the negotiated revision on discovery and invocation. A different revision is unsupported until separately qualified.

Fixture coverage includes:

- initialization success and protocol mismatch;
- paginated `tools/list`;
- schema and metadata drift;
- timeout, malformed JSON-RPC, oversized payload, and disconnect;
- authenticated and unauthenticated connections; and
- text, structured JSON, unsupported content, and error results.

### 8.2 Discovery

1. Claim one verification/refresh job carrying the connection's allocated monotonic refresh generation.
2. Load the immutable connection version and secret reference.
3. Recheck current connection state and endpoint policy.
4. Initialize a short-lived MCP session through the constrained worker network.
5. Read all tool pages within the configured bounds.
6. Normalize and validate the complete snapshot.
7. Complete under one of two locked transactions. A successful discovery requires this is still the latest-started generation, its version is current, and lifecycle remains valid (`verifying` for initial verification or `active|degraded` for refresh), explicitly rejecting `disabled`; it appends the snapshot/capability versions, marks superseded bindings non-executable, and updates current snapshot and healthy lifecycle projections. A timeout, disconnect, invalid, incomplete, or rejected payload from the latest otherwise-eligible generation preserves the prior snapshot but records typed failure health and moves `verifying -> degraded` or `active -> degraded`. Obsolete, disabled, or superseded-generation jobs update neither snapshot nor health.

An unsuccessful discovery never publishes a snapshot. Only the latest otherwise-eligible failure may update health/degraded lifecycle; obsolete, disabled, or superseded-generation work changes nothing. Discovery may retry because it sends no tool invocation.

### 8.3 Invocation and dispatch fence

1. API validates authorization, capability state, input schema, limits, secret scan, confirmation token, and idempotency.
2. One transaction creates the run and queued job pinned to exact immutable versions.
3. Worker claims the job, atomically increments and records its `lease_epoch`, and rechecks current actor membership, connection/capability status, deadline, cancellation, exact current verified connection version, and the binding's presence with identical tool/schema identity in the current discovery snapshot.
4. Worker initializes a fresh short-lived MCP session using the pinned connection version.
5. Immediately before `tools/call`, one transaction repeats every mutable and exact-binding/current-snapshot check, requires the job lease is still owned/unexpired and its epoch exactly matches the worker claim, and changes the attempt from `preparing` to `dispatch_fenced` exactly once. A stale binding or worker fails this transaction and sends nothing.
6. Worker sends one call. No code path automatically invokes again after the fence.
7. A definitive response records success or failure. Timeout/disconnect/crash after fencing records `indeterminate` unless transport evidence proves no request bytes were accepted.
8. Worker closes the session and releases transient credentials and buffers.

Disabling a connection or capability wins against an unfenced attempt through the same row locks. A control change after the fence does not rewrite history or imply the remote call was stopped.

### 8.4 Cancellation

- Queued or preparing work cancels without an upstream call.
- After dispatch, cancellation is best effort and never represented as proof of non-execution.
- If the adapter cannot prove the remote disposition, the terminal state is `indeterminate` with `cancellation_requested=true`.

## 9. HTTP API

All responses use stable IDs, ISO-8601 timestamps, correlation IDs, and machine-readable error codes. Mutating endpoints require `Idempotency-Key`; only a scoped HMAC and key version are retained. Any response containing arguments or result payload sets `Cache-Control: no-store`; clients purge run-content state on logout or identity/workspace switch.

### 9.1 Registry and connections

- `POST /v1/registry/searches` — bounded read-only upstream search; query is body-only and redacted from telemetry.
- `POST /v1/registry/imports` — import one exact upstream metadata version; never creates executable trust.
- `GET /v1/registry/entries`
- `POST /v1/server-connections`
- `GET /v1/server-connections`
- `GET /v1/server-connections/{id}`
- `POST /v1/server-connections/{id}/versions` — Admin-only append of a new immutable endpoint or secret-binding configuration; atomically makes prior bindings non-executable, moves the stable connection back to `verifying`, and blocks invocation until that exact version verifies and publishes a current discovery snapshot.
- `POST /v1/server-connections/{id}/verify`
- `POST /v1/server-connections/{id}/refresh`
- `POST /v1/server-connections/{id}/disable`
- `POST /v1/server-connections/{id}/enable`

### 9.2 Capabilities

- `GET /v1/capabilities`
- `GET /v1/capabilities/{id}`
- `GET /v1/capability-versions/{id}`
- `POST /v1/capability-versions/{id}/enable`
- `POST /v1/capability-versions/{id}/disable`

### 9.3 Runs

- `POST /v1/run-preflights` — authorize and validate one exact request without dispatch; return a short-lived signed confirmation token bound to actor, workspace, route, exact capability version, exact connection version, canonical argument digest, expiry, and a server-generated one-time nonce.
- `POST /v1/runs` — validate every confirmation-token binding against the authenticated caller and submitted canonical request, then atomically consume the nonce with run/idempotency creation. A transferred, expired, altered, or already-consumed token creates no run; an existing matching idempotency record replays only its original run.
- `GET /v1/runs`
- `GET /v1/runs/{id}`
- `GET /v1/runs/{id}/events`
- `POST /v1/runs/{id}/cancel`

Run content is returned only while retained and authorized and always uses `Cache-Control: no-store`. After content expiry, the API returns safe status and version/timing lineage without arguments or result payload.

### 9.4 Audit

- `GET /v1/audit-events` — Admin/Operator-only, cursor-paginated, workspace-scoped typed audit history. Supports bounded filters for resource type/ID, actor ID, action enum, outcome enum, and time range; it never returns request, schema, remote-metadata, result, secret, or raw upstream-error payloads.

## 10. Operator UI

| Route | Primary content |
|---|---|
| `/overview` | Connection, capability, run, and queue health |
| `/discover` | Registry search, import, and manual connection entry |
| `/servers` | Connection list and lifecycle status |
| `/servers/:id` | Immutable configuration history, verification, refresh, tools, health, audit |
| `/capabilities` | Filterable capability and enabled-state list |
| `/capabilities/:id` | Version history, inert schema viewers, exact binding, enable/disable |
| `/playground/:versionId` | JSON editor, validation, data warning, confirmation, run creation |
| `/runs` | Status, capability, actor, duration, and time filters |
| `/runs/:id` | Timeline, exact lineage, bounded input/result, safe errors, cancellation state |

The UI never receives secret material. Remote URLs are rendered as text unless separately validated for navigation. Raw schemas/results are not inserted as HTML.

## 11. Epics and tasks

Effort ranges include implementation, tests, review, and documentation. Work within an epic may overlap; the total range does not assume perfect parallelism.

### E0 — Decisions and repository foundation — 2–3 person-weeks

| ID | Task | Acceptance |
|---|---|---|
| E0-T1 | Approve scope/threat model and ADRs 001–007 | Owners sign assumptions, non-goals, protocol, dispatch, retention, immutable identity, and drift decisions |
| E0-T2 | Scaffold API, worker, web, shared schemas, lint, typecheck, and CI | Clean checkout runs all quality gates |
| E0-T3 | Add PostgreSQL migrations, local Compose, fixture secret provider, and test harness | One command starts a healthy local stack |
| E0-T4 | Build reference MCP fixture server and recorded Registry fixtures | Deterministic discovery/invocation contracts run offline |

### E1 — Identity, persistence, and audit — 3–4 person-weeks

| ID | Task | Acceptance |
|---|---|---|
| E1-T1 | Implement users, workspaces, memberships, and roles | Repository and API authorization matrices pass |
| E1-T2 | Integrate OIDC and explicit local-development principal | Deployed/local modes cannot be confused |
| E1-T3 | Implement workspace-scoped repository boundary and database constraints | Cross-workspace ID probes fail closed |
| E1-T4 | Implement secret-binding abstraction and deployment provider | Secret values never persist or appear in API/telemetry fixtures |
| E1-T5 | Implement typed audit events and correlation propagation | Required mutations emit payload-free audit evidence |

### E2 — Registry and MCP discovery — 4–5 person-weeks

| ID | Task | Acceptance |
|---|---|---|
| E2-T1 | Implement registry entries, connections, immutable connection versions, and lifecycle | Invalid transitions and mutable-version writes fail |
| E2-T2 | Implement constrained MCP transport and endpoint policy | SSRF, DNS, scheme, TLS, redirect, timeout, and size fixtures pass |
| E2-T3 | Wrap the pinned SDK and qualify initialization, safe-regex schema handling, bounded validation, and paginated discovery | Recorded protocol and adversarial schema-timing suites pass without SDK leakage into domain types |
| E2-T4 | Implement bounded normalized snapshots and canonical identity | Equivalent snapshots deduplicate; changed snapshots append |
| E2-T5 | Implement capabilities, immutable versions, bindings, and status events | Drift creates pending versions and preserves enabled history |
| E2-T6 | Implement explicit/scheduled refresh, monotonic generations, current-snapshot health, and overlapping-job protection | Only the latest-started eligible generation publishes; its failure preserves snapshot but degrades health, while obsolete/disabled/superseded work changes neither |

### E3 — Durable invocation — 4–5 person-weeks

| ID | Task | Acceptance |
|---|---|---|
| E3-T1 | Implement PostgreSQL job leasing, monotonically increasing lease epochs, heartbeat, and recovery | Worker-loss tests reclaim only safe jobs and stale epochs cannot fence |
| E3-T2 | Implement runs, attempts, events, state machine, and exact lineage | Projection replay reproduces every status |
| E3-T3 | Implement schema validation, limits, fail-closed secret guardrail, and caller/request/version-bound one-time confirmation token | Invalid/sensitive/scanner-failed/stale/transferred/replayed requests retain no arguments and cannot enqueue |
| E3-T4 | Implement scoped key HMAC, versioned canonical-request HMAC, conflict detection, and concurrent creation handling | Same key/request creates one run; changed capability/arguments conflict after content expiry; raw key never persists |
| E3-T5 | Implement exact-current-binding and lease-epoch-validated fresh-session dispatch fence, send-once policy, deadline, and cancellation | Crash/reclaim/control/configuration/schema/cancel races never send through stale lineage or duplicate a call |
| E3-T6 | Implement pre-storage bounded result/error normalization and retention cleanup | Only clean, output-schema-valid text/JSON reaches durable storage; unsupported/sensitive/invalid payload is absent, and every terminal state's retained content expires |

### E4 — Control-plane API — 2–3 person-weeks

| ID | Task | Acceptance |
|---|---|---|
| E4-T1 | Establish errors, pagination, optimistic concurrency, workspace-scoped audit reads, and OpenAPI conventions | Contract tests cover stable codes, audit role/filter/content boundaries, and unknown future events |
| E4-T2 | Implement Registry search/import and connection endpoints, including append-only configuration versions | Role, outage, unsafe endpoint, version-history, reverification, and idempotency paths pass |
| E4-T3 | Implement capability/version/status endpoints | Immutable/history/authorization paths pass |
| E4-T4 | Implement bound one-time preflight/run/event/cancel endpoints, `no-store` content responses, and generated client | Token transfer/replay, identity-switch cache isolation, API E2E, and generated-client CI pass |

### E5 — Operator UI — 4–5 person-weeks

| ID | Task | Acceptance |
|---|---|---|
| E5-T1 | Build shell, auth boundary, navigation, query/error primitives | Role and logout tests pass |
| E5-T2 | Build overview and shared status/empty/loading components | Visual and component-state tests pass |
| E5-T3 | Build discovery/import and connection lifecycle flows | Manual/import/incident scenarios pass in Playwright |
| E5-T4 | Build capability list, immutable version, schema, and enablement views | Drift scenario passes in Playwright |
| E5-T5 | Build playground, confirmation, polling, run list, and timeline | Safe invocation/failure scenarios pass in Playwright |
| E5-T6 | Complete responsive, keyboard, focus, label, contrast, and screen-reader pass | Automated checks and manual reference-journey review pass |

### E6 — Operations and release — 3 person-weeks

| ID | Task | Acceptance |
|---|---|---|
| E6-T1 | Add logs, traces, metrics, dashboards, and alerts | One correlation spans API, job, MCP, and terminal run without payload leakage |
| E6-T2 | Add limits, rate/concurrency controls, retention jobs, and operational health | Abuse and cleanup fixtures pass |
| E6-T3 | Exercise threat model and independent security review | Current-scope blockers close or explicitly block release |
| E6-T4 | Exercise deployment, backup/restore, rollback, disable, rotation, and outage runbooks | A second engineer completes each runbook |
| E6-T5 | Run load, failure, accessibility, and clean-install qualification | Stored evidence satisfies every release gate |

Total: **36 tasks, 22–28 person-weeks**.

## 12. Planned pull requests

PRs should be vertically reviewable. Generated files and fixtures are excluded from the suggested implementation-line budget.

| PR | Title | Scope | Depends on | Merge proof |
|---:|---|---|---|---|
| 01 | `docs: lock registry alpha decisions` | ADRs, state machines, threat model, API conventions | — | Human architecture/product/security approval |
| 02 | `build: scaffold api worker web and CI` | Workspaces, lockfiles, local stack, quality gates | 01 | Green clean-checkout CI |
| 03 | `feat(identity): add workspace auth secrets and audit` | E1 models/services and migration | 02 | Isolation, auth, secret, and audit tests |
| 04 | `feat(registry): add connections capabilities and immutable versions` | Registry domain, persistence, lifecycle | 03 | Versioning and transition tests |
| 05 | `test(mcp): add protocol and registry fixtures` | MCP server and recorded upstream fixtures | 02 | Offline contract suite |
| 06 | `feat(mcp): add constrained discovery and refresh` | Transport, SDK adapter, snapshots, drift, health | 04, 05 | SSRF and discovery fault suites |
| 07 | `feat(registry): add official catalog search and import` | Bounded adapter, cache, provenance | 04, 05 | Recorded contract and outage tests |
| 08 | `feat(execution): add jobs runs and idempotency` | Job/run ledger, preflight, limits, state | 03, 04 | Crash and concurrency tests |
| 09 | `feat(invocation): add fenced MCP tool execution` | Session, fence, call, result/error, cancellation | 06, 08 | No-duplicate and content-safety suites |
| 10 | `feat(api): expose registry capability and run contracts` | HTTP endpoints, OpenAPI, generated client | 07–09 | API E2E and compatibility tests |
| 11 | `feat(web): ship registry operator journey` | E5 UI and accessibility | 10 | Playwright reference journey |
| 12 | `release: harden and qualify registry alpha` | Telemetry, limits, retention, runbooks, evidence | 11 | Release gates signed |

Parallel lanes after PR-02:

- identity/domain work: PR-03 → PR-04;
- protocol fixtures: PR-05;
- UI shell against mock contracts; and
- operational scaffolding.

Critical path:

```text
01 -> 02 -> 03 -> 04
02 -> 05
04 + 05 -> 06
04 + 05 -> 07
03 + 04 -> 08
06 + 08 -> 09
07 + 08 + 09 -> 10
10 -> 11 -> 12
```

## 13. Test plan

### Unit

- state transitions and immutable-write guards;
- workspace authorization decisions;
- endpoint/IP policy and URL normalization;
- snapshot canonicalization;
- JSON Schema/content bounds, safe regex subset, and independent validation timeout;
- run projection, idempotency, and safe error mapping.

### Contract

- pinned MCP initialization, discovery, invocation, and error behavior;
- recorded official Registry request/response shapes;
- generated OpenAPI client;
- secret-provider adapter;
- unknown event/error forward compatibility.

### Integration

- migration from empty and previous PR schema;
- job leasing, worker death, and dispatch-fence recovery;
- stale-worker fencing where an old worker pauses, a new worker reclaims and increments the epoch, and the old epoch cannot fence or send;
- disable/refresh/invocation races;
- configuration-version append/reverification and disable-during-discovery races;
- overlapping refresh generation races where an older completion cannot replace the latest snapshot or health;
- latest eligible discovery timeout/invalid/secret result preserves the snapshot but records degraded health; obsolete or disabled failures cannot mutate health;
- secret retrieval and telemetry redaction;
- concurrent idempotency and refresh;
- same-key changed-request conflict after retained arguments expire;
- pre-storage result rejection, output-schema mismatch suppression, all-terminal-state retention cleanup, and backup restoration.

### End to end

- all scenarios in Section 3.2;
- Admin/Operator can page/filter workspace audit history while Viewer and cross-workspace callers cannot read it;
- confirmation-token alteration, transfer, expiry, and replay create no run, while matching idempotent replay returns only the original run;
- content-bearing run responses are `no-store`, and logout or identity/workspace switching exposes no prior cached arguments or results;
- Viewer cannot mutate and Operator cannot change secret bindings;
- hostile endpoint cannot access a denied destination;
- disabled connection blocks queued unfenced work;
- schema drift preserves history and requires review;
- connection rotation or schema drift makes superseded bindings non-executable before admission and every fence;
- adversarial schema regexes cannot monopolize API or worker validation;
- unsupported or secret-shaped results expose no payload;
- argument or discovery scanner failure persists no submitted/remote payload and sends no invocation;
- upstream Registry outage does not impair existing capabilities or invocation.

### Manual qualification

- fresh install;
- credential rotation;
- backup restore;
- incident disable;
- failure diagnosis from UI/telemetry;
- keyboard and screen-reader journey;
- rollback using the documented maintenance/drain procedure.

## 14. Delivery and staffing

### 14.1 Estimate

| Epic | Person-weeks |
|---|---:|
| E0 — Decisions/foundation | 2–3 |
| E1 — Identity/persistence/audit | 3–4 |
| E2 — Registry/discovery | 4–5 |
| E3 — Invocation | 4–5 |
| E4 — API | 2–3 |
| E5 — UI | 4–5 |
| E6 — Operations/release | 3 |
| **Total** | **22–28** |

With three focused engineers, plan **8–10 elapsed weeks** including integration and stabilization. With two, plan 12–16 weeks. Re-estimate if a release assumption or promotion trigger changes.

### 14.2 Ownership

- Platform/MCP engineer: E2, E3, worker/network safety.
- Backend/control-plane engineer: E1, E4, migrations, audit.
- Product/full-stack engineer: E5, generated client, E2/E3 operational views.
- Part-time security reviewer: threat model, endpoint isolation, credential and content handling.
- Product owner: scope, operator workflow, and release acceptance.

### 14.3 Target sequence

| Weeks | Target |
|---|---|
| 1–2 | Decisions, stack, identity, persistence, fixtures, UI shell |
| 3–4 | Registry domain, constrained transport, discovery, immutable versions |
| 5–6 | Jobs, run ledger, fenced invocation, upstream Registry adapter |
| 7–8 | APIs, complete operator journey, telemetry, fault coverage |
| 9–10 | Stabilization, accessibility, security review, restore/runbooks, release evidence |

## 15. Rollout and rollback

### Stage 0 — developer qualification

- local fixture-only environment;
- all automated suites green;
- no real credentials or remote side effects.

### Stage 1 — internal staging

- curated read-only or reversible tools;
- synthetic/public payloads;
- daily review of failures, indeterminate runs, and denied endpoint attempts.

### Stage 2 — closed alpha

- pre-provisioned internal operators;
- explicit allowlist of server connections and capabilities;
- conservative concurrency and result limits;
- weekly product/security/operations review.

### Rollback

1. Disable new connection creation and run admission.
2. Stop claiming queued jobs.
3. Let fenced attempts finish or become indeterminate; cancel unfenced attempts.
4. Snapshot database and audit state.
5. Roll back only to a binary compatible with the current schema; otherwise use the documented maintenance migration.
6. Re-enable traffic after smoke, history, and reference-journey checks.

Mixed-version zero-downtime rollback is not promised in this alpha.

## 16. Principal risks

| Risk | Mitigation |
|---|---|
| Registry work obscures routing differentiation | Time-box M1 and require M2 kickoff artifacts before release closeout |
| Hostile endpoint reaches internal services | Isolated worker egress, deny network policy, HTTPS, no redirects, adversarial fixtures |
| Tool call executes twice | Durable fence, one attempt, no automatic post-fence retry, indeterminate truth |
| Credential leaks through application surfaces | Opaque bindings, just-in-time retrieval, payload-free telemetry/audit, fixture scanning |
| Remote schema/result attacks UI | Strict bounds, local-only references, inert rendering, CSP |
| Schema drift changes behavior silently | Immutable snapshots/versions and explicit enablement |
| Alpha receives confidential data | Product/API warning, obvious-secret guardrail, operator policy, incident procedure |
| Deferred control becomes necessary mid-build | Promotion trigger requires explicit re-scope, owner, acceptance tests, and estimate |
| Plan regrows through review | Findings classified as blocker, seam, promotion control, or backlog before editing scope |

## 17. Required ADRs

1. ADR-001 — milestone scope, threat model, and promotion triggers.
2. ADR-002 — exact MCP SDK, protocol revision, and unsupported features.
3. ADR-003 — workspace authorization and OIDC/local-development boundary.
4. ADR-004 — endpoint isolation, DNS/IP policy, TLS, and redirects.
5. ADR-005 — durable job, dispatch fence, idempotency, cancellation, and indeterminate semantics.
6. ADR-006 — non-confidential content storage, secret guardrail, retention, and explicit deletion limitations.
7. ADR-007 — immutable registry/version identity and schema-drift behavior.

## 18. Definition of done

Milestone 1 is done when:

- all release assumptions and promotion triggers are documented in product and operator surfaces;
- the reference journey passes against a fresh deployed environment;
- 36 scoped tasks and 12 planned PR outcomes are complete or explicitly removed by scope decision;
- workspace, endpoint, credential, immutable-lineage, and no-duplicate-call boundaries pass adversarial tests;
- unsupported/private/secret-shaped payloads fail without content leakage;
- telemetry diagnoses every required failure state without payload capture;
- backup/restore and rollback preserve truthful run/version/audit history;
- accessibility and security reviewers sign their gates;
- effort and elapsed-time reporting match actual delivery; and
- M2 can reuse capability and run identities without changing M1 history.

## 19. Kickoff decisions

Before PR-02 begins, confirm:

- engineering and security owners;
- hosting environment, PostgreSQL, OIDC, secret provider, and egress-control mechanism;
- exact MCP SDK/protocol revision;
- official Registry contract fixture and acceptable outage behavior;
- reference MCP servers and which tools are read-only or safely reversible;
- input/result/time/concurrency defaults;
- run/audit retention defaults;
- whether eight to ten elapsed weeks with three focused engineers is acceptable; and
- that private repositories and promotion-triggered enterprise controls remain outside `v0.1.0`.

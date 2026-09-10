# Registry Alpha Threat Model

Status: engineering baseline; independent security review pending.

## Scope and trust boundaries

The closed alpha accepts only curated MCP endpoints and public, synthetic, or explicitly
non-confidential invocation data. Operators authenticate through OIDC outside local development.
The API trusts the configured identity provider, PostgreSQL, and the mounted secret provider. In
Cloudflare staging, Access and Tunnel are the public authentication and transport boundary, while
the web gateway is the only trusted assertion-forwarding peer. MCP servers and the public Registry
are untrusted networks and untrusted content sources.

The API never receives or persists MCP credential values; its authentication middleware necessarily
handles OIDC bearer tokens in memory. It stores opaque secret bindings, immutable MCP metadata,
bounded invocation arguments/results, and payload-free audit/telemetry records. Uvicorn access logs
are disabled, and unstructured log messages are discarded rather than serialized. Workers retrieve
an MCP credential just in time, validate the endpoint again, establish one fenced session, perform
at most one dispatch, and release transient content.

## Threats and enforced controls

| Threat | Control | Verification |
|---|---|---|
| Cross-workspace access | Workspace-scoped foreign keys and authorization on every `/v1` request | `tests/test_auth.py`, `tests/test_permissions.py` |
| SSRF, DNS rebinding, redirects | HTTPS-only policy, public-address resolution, peer verification, no redirects | `tests/test_mcp_adapter.py` |
| Credential disclosure | Opaque bindings, mounted-file provider, bounded JSON logs, upstream log suppression | `tests/test_secrets.py`, `tests/test_mcp_adapter.py` |
| Duplicate side effects | Durable nonce/idempotency records, session and dispatch fences, indeterminate terminal state | `tests/test_execution.py`, `tests/test_postgres_concurrency.py` |
| Schema/result resource exhaustion | Byte, depth, regex, process memory, and timeout bounds | `tests/test_mcp_adapter.py`, `tests/test_execution.py` |
| Silent capability drift | Immutable snapshots/versions and exact-version approval | `tests/test_registry.py`, `tests/test_discovery_publication.py` |
| Restored work dispatches twice | Installation epoch, startup quarantine, bounded reconciliation | quarantine state-machine tests plus the pending actual backup/restore manual gate |
| API abuse | Configured per-peer rate and concurrency admission plus bounded query pagination | `tests/test_ops.py`, API contract tests |
| Forged edge identity | Browser authorization is stripped; Access assertions are accepted only from the fixed gateway peer and still receive full JWT validation | `tests/test_auth.py`, `tests/test_api_contracts.py`, `scripts/verify_cloudflare_deployment.py` |
| Accidental public service exposure | No Compose host ports; internal application and monitoring networks; Tunnel reaches only web and optionally Grafana | `scripts/verify_cloudflare_deployment.py`, staging qualification smoke check |
| Retained content outlives policy | Absolute database-clock expiry and bounded worker cleanup | `tests/test_execution.py` |

## Explicit residual risks

- This alpha is not approved for confidential, regulated, private-repository, or production customer
  payloads.
- API rate limiting is process-local. Multiple API replicas require a shared gateway limiter before
  horizontal scale. Behind ingress, the API accepts `X-Real-IP` only from explicitly configured
  proxy transport addresses; Uvicorn forwarded-header rewriting is disabled, and ingress must
  discard caller-supplied `X-Real-IP` and `X-Forwarded-For` values before setting a validated IP.
- Metrics endpoints are unauthenticated. The Cloudflare staging API accepts metrics requests only
  from the pinned Prometheus peer, and the worker listener binds only to its monitoring interface;
  other deployment targets must provide equivalent network and application-layer restrictions.
- The mounted-file provider assumes the deployment platform protects its filesystem and process
  namespace.
- Cloudflare Tunnel is an inbound boundary, not an application egress allowlist. The staging host or
  provider firewall must constrain API, worker, and alerting destinations independently.
- MCP calls are not generally idempotent. A connection loss after the dispatch fence is reported as
  indeterminate and requires human reconciliation.
- Mixed-version zero-downtime rollback is unsupported.

## Promotion triggers

Confidential data, external tenants, paid settlement, arbitrary publishers, multiple API replicas,
or unrestricted write-capable tools require a revised threat model and explicit controls before
scope changes. The independent reviewer records findings in the release qualification document;
open blockers prevent promotion.

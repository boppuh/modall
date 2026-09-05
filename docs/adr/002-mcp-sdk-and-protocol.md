# ADR-002: MCP SDK and qualified protocol revision

- Status: accepted
- Date: 2026-09-05

## Decision

Modall Registry Alpha pins the official Python MCP SDK at `mcp==1.27.2` and qualifies
protocol revision `2025-06-18` over Streamable HTTP.

The local adapter introduced in planned PR06 must expose Modall-owned domain types, request
this exact revision during initialization, reject a different negotiated revision, disable
redirects, and support only `tools/list` and `tools/call`. Prompts, resources, sampling,
elicitation, roots, tasks, SSE, and stdio are out of scope.

## Rationale

The newest maintained 1.x SDK explicitly supports `2025-06-18`, retains the initialization
and session behavior required by the alpha's two-stage invocation fence, and continues to
receive critical security fixes. Pinning the patch version makes qualification reproducible.
Adopting the newer stateless protocol is a separate architecture and threat-model decision,
not an incidental dependency upgrade.

## Qualification evidence

`tests/test_mcp_fixtures.py` verifies the SDK pin and supported-revision list against a
deterministic Streamable HTTP fixture. The suite covers initialization, initialized
notification, opaque-cursor pagination, schema and metadata drift, authentication, protocol
mismatch, malformed and oversized responses, timeout, disconnect, text and structured
results, unsupported content, and tool errors. Official Registry v0.1 response fixtures are
stored with source commit and SHA-256 provenance and require no network access in CI.

## Consequences

- Dependabot-style SDK updates do not merge without rerunning this contract suite and security
  review.
- A server that negotiates any other revision may be shown as discovered metadata but cannot
  be enabled or invoked.
- Protocol `2026-07-28` and SDK 2.x require a new ADR because they change handshake, session,
  and dispatch-fence assumptions.

# Modall

Modall is a capability registry and execution platform. The current milestone is a closed
operator alpha for connecting, discovering, versioning, invoking, and diagnosing curated MCP
tools with exact lineage.

The governing scope is [MCP_REGISTRY_ALPHA_IMPLEMENTATION_PLAN.md](MCP_REGISTRY_ALPHA_IMPLEMENTATION_PLAN.md).

## Prerequisites

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
- Node.js 22.13 or newer (before 23) and npm 10
- Docker with Compose

## Develop

Install the locked dependencies and run all quality gates:

```sh
make bootstrap
make check
```

Run the complete local process topology:

```sh
cp .env.example .env
make compose-up
```

The API readiness endpoint is at `http://localhost:8000/health/ready`; the web shell is at
`http://localhost:5173`. Stop the stack with `make compose-down`.

The operator UI asks for the bootstrapped workspace UUID. Local mode uses the configured local
identity; OIDC environments also accept an access token for the current browser session. Tokens
remain in memory and are never written to browser storage; OIDC sessions therefore require a new
token after a reload. The UI covers Registry search/import,
manual connection and lifecycle controls, immutable capability review, one-time run confirmation,
polling, cancellation, and the durable event timeline.

Run the desktop and mobile reference journey, including the automated WCAG scan, with:

```sh
npx playwright install --with-deps chromium
npm run e2e --workspace @modall/web
```

Compose applies Alembic migrations before starting the API. For a separately managed database,
run `make migrate` with `MODALL_DATABASE_URL` configured.

Local/test processes use the explicit `local` authentication mode and fixture secret provider.
Local fixture credentials are shared by the API and worker through
`MODALL_FIXTURE_SECRET_ROOT`. Store each credential beneath that directory using the same
base64url filename mapping described below; `.modall/` is ignored by Git and mounted read-only by
Compose. Create `.modall/fixture-secrets` before starting Compose and never commit its contents.
Staging and production settings fail validation unless OIDC (`MODALL_OIDC_ISSUER`,
`MODALL_OIDC_AUDIENCE`, and `MODALL_OIDC_JWKS_URL`) and the `mounted_file` secret provider are
configured. Mounted secrets are read only from `MODALL_SECRET_MOUNT_ROOT`. The immutable filename
is the unpadded base64url encoding of the external reference, a `.`, and the unpadded base64url
encoding of the version (for example, `api-token`/`v2` maps to `YXBpLXRva2Vu.djI`). The database
stores only the opaque reference and version. Bindings whose encoded filename exceeds the portable
255-byte component limit are rejected before persistence.

The API and worker load confirmation and idempotency HMAC key versions from the same provider
using the fixed references `system-confirmation-hmac` and `system-idempotency-hmac`. Configure
active-first version lists with `MODALL_CONFIRMATION_HMAC_KEY_VERSIONS` and
`MODALL_IDEMPOTENCY_HMAC_KEY_VERSIONS`; deployed mounted-file environments must project the
corresponding encoded files before either process starts.

## Operate and qualify

API and worker logs are payload-free JSON. A caller-supplied correlation UUID is persisted on each
new run and follows its worker lifecycle; the full identifier is available in the run detail and API
response. Scrape API `/metrics` and `/metrics` on the configured worker metrics port (9101 by
default). The maintained alert rules and Grafana dashboard live under `ops/`. Deployment ingress
must restrict both metrics surfaces to the monitoring network.

Admission, content, timeout, retention, reconciliation, rate, and concurrency bounds are explicit
`MODALL_` settings documented in `.env.example`; API and worker build one shared execution policy.
The API limiter is process-local for the closed alpha, so multiple replicas require a shared ingress
limiter before promotion. Behind ingress, configure `MODALL_TRUSTED_PROXY_ADDRESSES` and require
the ingress to discard inbound `X-Real-IP` and `X-Forwarded-For`, then set one validated client
address in `X-Real-IP`; Uvicorn's independent forwarded-header rewriting is disabled.

Use `modall-ops status` for payload-free execution posture. Restore operations require explicit
confirmation arguments and must follow
[the operations runbook](docs/operations/registry-alpha-runbook.md). The
[threat model](docs/security/registry-alpha-threat-model.md) and
[qualification record](docs/release/registry-alpha-qualification.md) distinguish automated evidence
from the still-required human reviews. Validate committed operational artifacts with
`make release-artifacts`.

## API contracts

The authenticated control-plane API is published under `/v1`. Supply the selected workspace in
`X-Workspace-ID`; deployed clients also send their OIDC bearer token. Mutations require a bounded
`Idempotency-Key`. Workspace responses are non-cacheable and every response carries an
`X-Correlation-ID`; callers may supply a UUID correlation ID to continue an existing trace.

OpenAPI is available at `/openapi.json`. Run `npm run api:generate` after changing a contract to
refresh the committed schema and TypeScript declarations used by the web application. CI rejects
generated-client drift.

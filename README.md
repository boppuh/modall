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

Compose applies Alembic migrations before starting the API. For a separately managed database,
run `make migrate` with `MODALL_DATABASE_URL` configured.

Local/test processes use the explicit `local` authentication mode and fixture secret provider.
Staging and production settings fail validation unless OIDC (`MODALL_OIDC_ISSUER`,
`MODALL_OIDC_AUDIENCE`, and `MODALL_OIDC_JWKS_URL`) and the `mounted_file` secret provider are
configured. Mounted secrets are read only from `MODALL_SECRET_MOUNT_ROOT`. The immutable filename
is the unpadded base64url encoding of the external reference, a `.`, and the unpadded base64url
encoding of the version (for example, `api-token`/`v2` maps to `YXBpLXRva2Vu.djI`). The database
stores only the opaque reference and version. Bindings whose encoded filename exceeds the portable
255-byte component limit are rejected before persistence.

The API and worker are intentionally thin in this foundation PR. Persistence, identity,
registry, discovery, execution, and operator workflows land in the independently reviewed
slices listed in the implementation plan.

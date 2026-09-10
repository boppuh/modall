# Cloudflare Staging Deployment

This is the supported Milestone 1 staging shape. Cloudflare Access and Tunnel protect a private
single-host container deployment; a managed PostgreSQL service remains the durable system of
record. The deployment is intentionally single-region and single-replica for the closed alpha.

Never commit tunnel tokens, database URLs, Access cookies, alert destinations, HMAC keys, MCP
credentials, or Grafana passwords. Record immutable deployment and backup identifiers in the
external release system of record.

## Cloudflare control plane

1. Put the staging hostname in a Cloudflare-managed DNS zone.
2. Create one self-hosted Access application covering the entire hostname. Use an identity policy
   limited to named reviewers, require MFA, and keep the application deny-by-default.
3. Record the exact team issuer (`https://TEAM.cloudflareaccess.com`), application AUD tag, and
   certs URL (`https://TEAM.cloudflareaccess.com/cdn-cgi/access/certs`). Do not use the generic
   organization token as the application audience.
4. Create a remotely managed Tunnel. Configure its application hostname to route to
   `http://web:8080`. Optionally protect a separate Grafana hostname with its own Access policy and
   route it to `http://grafana:3000`. Do not publish Prometheus, Alertmanager, API, or worker ports.
5. Store the remotely managed tunnel token in the host secret directory. The connector reads it
   through `--token-file`; never place it in the environment file or Compose command line.

Cloudflare sends the application JWT in `Cf-Access-Jwt-Assertion`. The web gateway strips any
browser-supplied `Authorization`, cookies, `X-Forwarded-For`, and `X-Real-IP`, sets `X-Real-IP` only
from Cloudflare's `CF-Connecting-IP`, and forwards only the assertion. The API accepts that
assertion only when its direct peer is the gateway's fixed `172.30.0.10` application-network
address. It then validates signature, issuer, AUD, lifetime, and subject against Cloudflare's certs
endpoint. Health routes receive neither the Access assertion nor its cookie.

## Host and secret preparation

Use a dedicated Linux host with Docker Engine and the Compose plugin. Block all inbound host
traffic. Permit outbound Tunnel traffic and the minimum DNS, HTTPS, managed-PostgreSQL, Registry,
MCP-server, identity-key, alert-webhook, and image-registry destinations required for qualification.
Cloudflare Tunnel does not control API or worker egress.

Create an external secret root such as `/opt/modall/secrets`. Supply these files with no trailing
newline. Keep them root-owned and do not make them group- or world-readable:

- `database-url`: a TLS-required managed PostgreSQL DSN for a dedicated least-privilege role;
- `cloudflare-tunnel-token`: the token for this staging Tunnel only;
- `grafana-admin-password`: a unique staging password;
- `alertmanager.yml`: a copy of `deploy/cloudflare/alertmanager.example.yml` whose receiver points
  to the staging owner's real, secret alert destination; and
- `provider/`: mounted-provider files for both system HMAC key versions and any approved MCP
  credential bindings.

Mounted-provider filenames are the unpadded base64url encoding of the external reference, a dot,
and the unpadded base64url version. Generate a filename without exposing a value:

```sh
uv run python -c 'from modall.secrets.provider import MountedFileSecretProvider as P; import sys; print(P.filename_for(sys.argv[1], sys.argv[2]))' system-confirmation-hmac v1
```

The system confirmation and idempotency keys must contain independently generated high-entropy
bytes. Do not reuse either key as an MCP credential or database password.

Local Compose projects `file:` secrets as bind mounts, so their host permissions remain effective
inside the container. The topology pins API, worker, and Alertmanager to UID 65534, `cloudflared` to
UID 65532, and Grafana to UID 472. On the supported rootful Linux host, grant only those container
principals the required access with POSIX ACLs (install the host's `acl` package first):

```sh
sudo chown -R root:root /opt/modall/secrets
sudo chmod 0700 /opt/modall/secrets /opt/modall/secrets/provider
sudo chmod 0600 /opt/modall/secrets/database-url /opt/modall/secrets/cloudflare-tunnel-token /opt/modall/secrets/grafana-admin-password /opt/modall/secrets/alertmanager.yml
sudo setfacl -m u:65534:--x,u:65532:--x,u:472:--x /opt/modall/secrets
sudo setfacl -m u:65534:r /opt/modall/secrets/database-url /opt/modall/secrets/alertmanager.yml
sudo setfacl -m u:65532:r /opt/modall/secrets/cloudflare-tunnel-token
sudo setfacl -m u:472:r /opt/modall/secrets/grafana-admin-password
sudo setfacl -R -m u:65534:rX /opt/modall/secrets/provider
```

If the host uses user-namespace remapping or rootless Docker, translate the container UIDs to their
host subordinate UIDs before applying ACLs; do not grant `o+r` as a shortcut. After building, inspect
the rendered `user` values and require every service to start successfully with `up -d --wait`.
An unreadable secret must be treated as a deployment failure, not repaired by weakening host modes.

Copy `deploy/cloudflare/staging.env.example` to `deploy/cloudflare/staging.env` and replace every
placeholder and host path. This file contains coordinates and paths, not secret values. Run:

```sh
make staging-config-check
make release-artifacts
```

`staging-config-check` reads `deploy/cloudflare/staging.env` by default so it validates the file
that deployment will actually use. CI overrides `STAGING_ENV_FILE` with the committed example.

## Deploy and bootstrap

Create and verify a restorable managed-PostgreSQL backup before first admission. From the checked
out immutable release revision, build and start the private topology:

```sh
docker compose --env-file deploy/cloudflare/staging.env -f deploy/cloudflare/compose.yaml pull
docker compose --env-file deploy/cloudflare/staging.env -f deploy/cloudflare/compose.yaml build
docker compose --env-file deploy/cloudflare/staging.env -f deploy/cloudflare/compose.yaml up -d --wait
```

Use Cloudflare's authenticated identity endpoint to obtain the primary reviewer's exact Access
`sub` without copying the JWT into a ticket or shell history. Bootstrap the workspace from the API
container; the operation is idempotent for the same issuer, subject, and workspace name:

```sh
docker compose --env-file deploy/cloudflare/staging.env -f deploy/cloudflare/compose.yaml exec api modall-ops bootstrap-workspace --name 'Registry Alpha' --issuer 'https://TEAM.cloudflareaccess.com' --subject 'ACCESS-SUBJECT' --display-name 'Primary Reviewer' --confirm BOOTSTRAP
```

Record the returned workspace UUID outside Git. An initial request made before bootstrap may create
the user identity but cannot grant itself membership.

## Qualify and collect evidence

Save the exact bare HTTPS staging origin in an owner-readable file maintained separately from the
command invocation. Save a short-lived `CF_Authorization` application cookie in another
owner-readable temporary file. Run the payload-free edge, health, and identity smoke check, then
securely delete the cookie file:

```sh
uv run python scripts/qualify_cloudflare_staging.py --base-url https://STAGING-HOST --trusted-origin-file /opt/modall/staging-origin --workspace-id WORKSPACE-UUID --access-cookie-file /secure/temp/access-cookie
```

The command refuses to send the cookie unless `--base-url` exactly matches an origin in the trusted
file. Before loading the cookie, it requires the unauthenticated UI root to redirect to a Cloudflare
Access login endpoint and proves unauthenticated API and metrics requests are blocked. It then checks
that both API health contracts survive the edge path and the Access identity has current workspace
membership. It never prints or persists the cookie. This smoke check does not replace the manual
reference journey.

Next execute every procedure in `docs/operations/registry-alpha-runbook.md`: reference journey,
counted-side-effect backup/restore, compatible rollback, endpoint disable and re-enable, credential
and HMAC rotation, Registry and MCP outage, keyboard navigation, and screen-reader review. Confirm
the provisioned Grafana dashboard receives both scrape targets and route a synthetic alert to the
staging owner. Store command output, revision, backup ID, dashboard link, and reviewer approvals in
the external release record without payloads or credentials.

## Network boundaries

The Compose topology publishes no host ports. Its application, dashboard, and monitoring networks
are internal. Only `cloudflared` and the web gateway share the edge network; `cloudflared` reaches
Grafana over the separate dashboard network and cannot resolve or connect to Prometheus or
Alertmanager. Prometheus scrapes fixed monitoring interfaces. The API returns 404 from `/metrics`
unless its direct peer is the pinned Prometheus address, and the worker metrics server binds only to
its monitoring address; the worker does not join the application network. API and worker also join a
separate egress network because they must reach PostgreSQL, Cloudflare signing keys, the official
Registry, and curated MCP endpoints. Alertmanager joins egress solely to deliver notifications.

Docker networks are segmentation, not a destination allowlist. Enforce the release allowlist and
deny private, link-local, metadata, and unapproved destinations in the host or provider firewall.
The application endpoint policy remains an independent fail-closed layer. Ensure `/metrics` and
worker port 9101 are reachable only from the monitoring network.

## Rotation and rollback

Rotate the Tunnel token by refreshing it in Cloudflare, replacing only the mounted token file, and
recreating `cloudflared`. A single-host alpha has a short connector interruption; use multiple
connectors before accepting an availability commitment. Rotate Access policies or AUD tags only in
a maintenance window because the API pins the exact issuer and audience.

For application rollback, follow the main runbook: stop admission and workers, snapshot PostgreSQL,
deploy only a revision compatible with the current schema, then repeat readiness, identity, audit,
and synthetic journey checks. Never roll back the database destructively or retry an indeterminate
side effect without upstream reconciliation.

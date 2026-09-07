# Registry Alpha Operations Runbook

All examples assume `MODALL_DATABASE_URL` and secret-provider settings point at the intended
environment. Capture command output and the deployment revision in the release evidence record.
Never place tokens, arguments, results, endpoint credentials, or secret values in tickets or logs.

## Deploy and verify

1. Confirm a restorable database backup and record its identifier outside this repository.
2. Project the active and retained HMAC key versions and MCP credential files.
3. Run `alembic upgrade head`; `alembic check` must report no drift.
4. Start the API, then worker, then web application.
5. Configure `MODALL_TRUSTED_PROXY_ADDRESSES` with the ingress transport addresses. The ingress must
   discard inbound `X-Real-IP` and `X-Forwarded-For`, then set exactly one validated client IP in
   `X-Real-IP` before proxying. Uvicorn forwarded-header rewriting remains disabled.
6. Restrict API `/metrics` and the configured worker metrics port (9101 by default) to the monitoring
   network; neither is an internet-facing endpoint.
7. Require successful API `/health/ready`, API `/metrics`, and worker `/health/live` on the configured
   metrics port from the monitoring network, and verify `/metrics` is unreachable publicly.
8. Run the clean-install reference journey with synthetic data.
9. Confirm request error rate, p95 latency, worker failures, and maintenance failures are below the
   alert thresholds in `ops/prometheus/alerts.yml`.

## Backup and restore quarantine

1. Stop workers. Keep the API unavailable to operators.
2. Restore the backup while workers remain stopped.
3. Run `modall-ops restore-enter --confirm RESTORE` against the restored database before any worker
   starts. This advances an epoch that cannot exist in the backup lineage and enables dispatch
   quarantine.
4. Repeatedly run `modall-ops restore-reconcile --batch-size 100` until it reports zero. Queued,
   preparing, and session-fenced runs become cancelled; dispatch-fenced runs become indeterminate.
5. Inspect `modall-ops status` and the run/audit projections. `active_runs` must be zero.
6. Run `modall-ops restore-clear --confirm CLEAR`, start workers, and execute only the synthetic
   smoke journey. Never retry an indeterminate side effect without upstream reconciliation.

## Roll back

1. Disable new admission at the ingress and stop workers.
2. Incident-disable affected connections. Let already dispatch-fenced work settle or become
   indeterminate; cancel unfenced work.
3. Snapshot the database and record the current migration revision.
4. Deploy only a binary compatible with that schema. This alpha does not support mixed-version
   rollback or destructive migration rollback against retained production data.
5. Re-run readiness, history, audit, and synthetic reference-journey checks before reopening.

## Incident-disable an endpoint

Use the Registry UI as an Operator or Admin and choose **Disable**. Confirm queued unfenced runs no
longer dispatch, capture the correlation UUID, and investigate without copying payloads. Only an
Admin can re-enable; re-enable creates fresh verification work and never revives pre-disable runs.

## Rotate credentials and HMAC keys

For an MCP credential, create a new immutable secret version, append a connection version referencing
it, verify discovery, review drift, then disable the old connection version through normal lifecycle
controls. For system HMAC keys, project the new file to API and worker, prepend its version in both
configured version lists, deploy both processes, retain the prior versions for their full replay
window, and remove old versions only after the history-incomplete safety tests and window permit.

## Public Registry or MCP outage

Registry outage: stop search/import retries, continue using previously verified capabilities, and
watch `registry_upstream_*` safe errors. MCP outage: do not retry indeterminate calls; disable the
connection if failures are persistent. In both cases use the correlation UUID to join API and worker
events, never an argument or result fragment.

## Retention maintenance

Workers run bounded cleanup automatically. To exercise it while workers are stopped, run
`modall-ops maintenance`. Alert on any `modall_worker_maintenance_total{outcome="failed"}` increase.

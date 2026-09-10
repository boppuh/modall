# Registry Alpha Qualification Record

Release candidate: pending. This file separates reproducible evidence from human approval.

## Automated gates

| Gate | Command/evidence | Required result |
|---|---|---|
| Python quality and adversarial suite | `make python-check` | 100% pass, coverage at least 90% |
| Web quality and production build | `make web-check` | lint, type, unit, and build pass |
| Desktop/mobile accessibility journey | `npm run e2e --workspace @modall/web` | both projects pass with no axe violations |
| Migration/clean install | CI Python migration cycle | upgrade, drift check, downgrade, fresh upgrade pass |
| Local topology | CI Compose build and `docker compose config --quiet` | pass |
| Cloudflare staging topology | `make staging-config-check` plus CI production-image build | no public origin ports, pinned edge connector, trusted assertion boundary, mounted secrets, and private monitoring |
| Release artifacts | `make release-artifacts` | manifest, dashboard, alerts, runbook, threat model valid |
| Restore-quarantine state machine | `uv run pytest --no-cov tests/test_execution.py -k test_restore_quarantine_fences_old_jobs_and_retention_erases_content` | pass |
| Admission/load bounds | active-run admission, API concurrency/rate, and bounded pagination tests | pass |

## Manual gates

These are intentionally not marked complete by automation.

- [ ] Primary reviewer: reference journey, failure diagnosis, and release scope accepted.
- [ ] Qualified iOS engineer: independently executes deploy, an actual database backup/restore with
  a counted upstream side effect, rollback, disable, rotation, outage, keyboard, and screen-reader
  procedures.
- [ ] Independent security reviewer: reviews the threat model and records no open release blockers.
- [ ] Staging owner: records backup identifier, deployed revision, dashboards, and alert routing.

Any unchecked manual gate blocks promotion beyond developer qualification. A reviewer records name,
date, environment, immutable revision, and evidence location in the release system of record; do not
commit personal attestations or secrets to this repository.

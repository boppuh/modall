"""Fail CI when release evidence or operator artifacts drift out of scope."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    manifest = json.loads((ROOT / "docs/release/evidence-manifest.json").read_text())
    dashboard = json.loads((ROOT / "ops/grafana/registry-alpha-dashboard.json").read_text())
    assert manifest["milestone"] == "registry-alpha"
    assert set(manifest["human_gates"].values()) == {"pending"}
    assert len(manifest["automated_gates"]) >= 8
    assert dashboard["uid"] == "modall-registry-alpha"
    assert len(dashboard["panels"]) >= 5
    alerts = (ROOT / "ops/prometheus/alerts.yml").read_text()
    for required in (
        "ModallApiErrorRateHigh",
        "ModallApiLatencyHigh",
        "ModallWorkerInvocationFailures",
        "ModallMaintenanceFailure",
    ):
        assert required in alerts
    for required_path in (
        "docs/security/registry-alpha-threat-model.md",
        "docs/operations/registry-alpha-runbook.md",
        "docs/release/registry-alpha-qualification.md",
    ):
        assert (ROOT / required_path).stat().st_size > 1000


if __name__ == "__main__":
    main()

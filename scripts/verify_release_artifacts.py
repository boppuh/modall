"""Fail CI when release evidence or operator artifacts drift out of scope."""

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_AUTOMATED_GATES = {
    "python-quality",
    "web-quality",
    "accessibility-reference-journey",
    "migration-clean-install",
    "compose-build",
    "restore-quarantine-state-machine",
    "bounded-load",
    "release-artifacts",
}
REQUIRED_HUMAN_GATES = {
    "primary_reviewer",
    "ios_engineer",
    "security_reviewer",
    "staging_owner",
}
REQUIRED_PANELS = {
    "API request rate": ("modall_http_responses_total", 'scope="v1"', "rate("),
    "API p95 latency": (
        "modall_http_request_duration_seconds_bucket",
        'scope="v1"',
        "histogram_quantile(",
    ),
    "Requests in flight": ("modall_http_in_flight", "sum("),
    "Worker outcomes": (
        "modall_worker_invocations_total",
        'event="invocation_terminal"',
        "rate(",
    ),
    "Maintenance failures": (
        "modall_worker_maintenance_total",
        'outcome="failed"',
        "increase(",
    ),
}
REQUIRED_ALERTS = {
    "ModallApiErrorRateHigh": (
        "modall_http_responses_total",
        'scope="v1"',
        'status_class="5xx"',
        "> 0.02",
    ),
    "ModallApiLatencyHigh": (
        "modall_http_request_duration_seconds_bucket",
        'scope="v1"',
        "histogram_quantile(",
        "> 1",
    ),
    "ModallApiTargetDown": ("up", 'job="modall-api"', "== 0", "absent("),
    "ModallWorkerInvocationFailures": (
        "modall_worker_invocations_total",
        'event="invocation_terminal"',
        'outcome=~"failed|indeterminate"',
        "> 0",
    ),
    "ModallWorkerPollFailures": ("modall_worker_polls_total", 'outcome="failed"', "> 0"),
    "ModallWorkerStalled": (
        "modall_worker_last_progress_unixtime_seconds",
        "modall_worker_liveness_timeout_seconds",
        "time()",
        ">",
        'up{job="modall-worker"}',
        "== 0",
        "absent(",
    ),
    "ModallMaintenanceFailure": (
        "modall_worker_maintenance_total",
        'outcome="failed"',
        "> 0",
    ),
}
REQUIRED_DOCUMENT_SECTIONS = {
    "docs/security/registry-alpha-threat-model.md": {
        "## Scope and trust boundaries",
        "## Threats and enforced controls",
        "## Explicit residual risks",
        "## Promotion triggers",
    },
    "docs/operations/registry-alpha-runbook.md": {
        "## Deploy and verify",
        "## Backup and restore quarantine",
        "## Roll back",
        "## Incident-disable an endpoint",
        "## Rotate credentials and HMAC keys",
        "## Public Registry or MCP outage",
        "## Retention maintenance",
    },
    "docs/release/registry-alpha-qualification.md": {
        "## Automated gates",
        "## Manual gates",
    },
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def require_mapping(value: object, label: str) -> Mapping[str, object]:
    require(isinstance(value, Mapping), f"{label} must be an object")
    return cast(Mapping[str, object], value)


def contains_metric(expression: str, metric: str) -> bool:
    return (
        re.search(rf"(?<![A-Za-z0-9_:]){re.escape(metric)}(?![A-Za-z0-9_:])", expression)
        is not None
    )


def require_expression_terms(expression: str, terms: tuple[str, ...], label: str) -> None:
    metric, *required_text = terms
    require(contains_metric(expression, metric), f"{label} queries the wrong metric")
    require(all(term in expression for term in required_text), f"{label} has incorrect semantics")


def main() -> None:
    manifest = require_mapping(
        json.loads((ROOT / "docs/release/evidence-manifest.json").read_text()), "manifest"
    )
    require(manifest.get("milestone") == "registry-alpha", "unexpected milestone")
    automated_gates = manifest.get("automated_gates")
    require(isinstance(automated_gates, list), "automated release gates must be a list")
    require(
        set(cast(list[str], automated_gates)) == REQUIRED_AUTOMATED_GATES,
        "automated release gates drifted",
    )
    human_gates = require_mapping(manifest.get("human_gates"), "human gates")
    require(set(human_gates) == REQUIRED_HUMAN_GATES, "human release gates drifted")
    require(set(human_gates.values()) == {"pending"}, "human gates must remain pending in git")

    dashboard = require_mapping(
        json.loads((ROOT / "ops/grafana/registry-alpha-dashboard.json").read_text()), "dashboard"
    )
    require(dashboard.get("uid") == "modall-registry-alpha", "unexpected dashboard UID")
    panels = dashboard.get("panels")
    require(isinstance(panels, list), "dashboard panels must be a list")
    panel_queries: dict[str, str] = {}
    for raw_panel in cast(list[object], panels):
        panel = require_mapping(raw_panel, "dashboard panel")
        title = panel.get("title")
        targets = panel.get("targets")
        require(isinstance(title, str), "dashboard panel title is missing")
        require(isinstance(targets, list) and len(targets) == 1, f"invalid targets for {title}")
        title = cast(str, title)
        target = require_mapping(cast(list[object], targets)[0], f"dashboard target for {title}")
        expression = target.get("expr")
        require(
            isinstance(expression, str) and bool(expression.strip()), f"missing query for {title}"
        )
        panel_queries[title] = cast(str, expression)
    require(set(panel_queries) == set(REQUIRED_PANELS), "dashboard panel set drifted")
    for title, terms in REQUIRED_PANELS.items():
        require_expression_terms(panel_queries[title], terms, f"dashboard panel {title}")

    alert_document = require_mapping(
        yaml.safe_load((ROOT / "ops/prometheus/alerts.yml").read_text()), "alert document"
    )
    groups = alert_document.get("groups")
    require(isinstance(groups, list) and bool(groups), "alert groups are missing")
    rules: list[Mapping[str, object]] = []
    for raw_group in cast(list[object], groups):
        group = require_mapping(raw_group, "alert group")
        raw_rules = group.get("rules")
        require(isinstance(raw_rules, list), "alert rules must be a list")
        rules.extend(require_mapping(rule, "alert rule") for rule in cast(list[object], raw_rules))
    alert_names = {rule.get("alert") for rule in rules}
    require(alert_names == set(REQUIRED_ALERTS), "Prometheus alert set drifted")
    for rule in rules:
        name = rule["alert"]
        expression = rule.get("expr")
        require(
            isinstance(expression, str) and bool(expression.strip()),
            f"alert {name} has no expression",
        )
        require(isinstance(name, str), "alert name must be a string")
        name = cast(str, name)
        expression = cast(str, expression)
        require_expression_terms(expression, REQUIRED_ALERTS[name], f"alert {name}")
        require("for" in rule, f"alert {name} has no duration")
        require(isinstance(rule.get("labels"), Mapping), f"alert {name} has no labels")
        require(isinstance(rule.get("annotations"), Mapping), f"alert {name} has no annotations")

    for required_path, required_sections in REQUIRED_DOCUMENT_SECTIONS.items():
        document = (ROOT / required_path).read_text()
        headings = {line for line in document.splitlines() if line.startswith("## ")}
        require(required_sections <= headings, f"{required_path} is missing required sections")


if __name__ == "__main__":
    main()

import pytest

from scripts.verify_release_artifacts import contains_metric, require_expression_terms


def test_release_verifier_matches_exact_metric_identifiers() -> None:
    assert contains_metric(
        "sum(rate(modall_http_responses_total[5m]))", "modall_http_responses_total"
    )
    assert not contains_metric(
        "sum(rate(modall_http_responses_total_invalid[5m]))", "modall_http_responses_total"
    )


def test_release_verifier_rejects_inert_or_incomplete_expressions() -> None:
    required = ("modall_worker_polls_total", 'outcome="failed"', "> 0")

    with pytest.raises(ValueError, match="wrong metric"):
        require_expression_terms("vector(0)", required, "poll alert")
    with pytest.raises(ValueError, match="incorrect semantics"):
        require_expression_terms("modall_worker_polls_total", required, "poll alert")


def test_release_verifier_requires_counter_reset_detection() -> None:
    required = (
        "modall_worker_polls_total",
        'outcome="failed"',
        "> 0",
        "unless",
        "offset 10m",
    )
    incomplete = 'increase(modall_worker_polls_total{outcome="failed"}[10m]) > 0'

    with pytest.raises(ValueError, match="incorrect semantics"):
        require_expression_terms(incomplete, required, "poll alert")

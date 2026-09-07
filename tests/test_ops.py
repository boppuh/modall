import asyncio
import json
import logging
from typing import cast
from urllib.error import HTTPError
from urllib.request import urlopen
from uuid import uuid4

import httpx
import pytest

from modall.api.main import create_app
from modall.config import Settings
from modall.ops import cli
from modall.ops.cli import _parser
from modall.ops.limits import FixedWindowRateLimiter
from modall.ops.telemetry import JsonFormatter, MetricsRegistry, start_metrics_server
from modall.persistence.database import create_engine as create_database_engine
from modall.persistence.models import Base


def test_rate_limiter_resets_and_bounds_peers() -> None:
    now = [0.0]
    limiter = FixedWindowRateLimiter(2, max_peers=2, now=lambda: now[0])

    assert limiter.allow("first")
    assert limiter.allow("first")
    assert not limiter.allow("first")
    assert limiter.allow("second")
    assert limiter.allow("third")
    assert limiter.allow("first")  # The least-recently-used peer was evicted.
    now[0] = 60.0
    assert limiter.allow("first")


@pytest.mark.parametrize("limit,max_peers", [(0, 1), (1, 0)])
def test_rate_limiter_rejects_invalid_bounds(limit: int, max_peers: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        FixedWindowRateLimiter(limit, max_peers=max_peers)


def test_json_formatter_emits_only_explicit_safe_scalars() -> None:
    record = logging.LogRecord(
        "modall.test", logging.INFO, __file__, 1, "message-must-not-leak", (), None
    )
    correlation_id = uuid4()
    record.event = "request_completed"
    record.telemetry = {
        "correlation_id": correlation_id,
        "duration": float("inf"),
        "arguments": {"secret": "must-not-leak"},
        "long": "x" * 600,
    }
    record.untrusted_extra = "also-must-not-leak"

    rendered = JsonFormatter().format(record)
    payload = json.loads(rendered)

    assert payload["event"] == "request_completed"
    assert payload["correlation_id"] == str(correlation_id)
    assert payload["duration"] == "inf"
    assert payload["arguments"] == "<dict>"
    assert len(payload["long"]) == 512
    assert "must-not-leak" not in rendered

    unstructured = JsonFormatter().format(
        logging.LogRecord(
            "third.party", logging.ERROR, __file__, 1, "credential=%s", ("secret",), None
        )
    )
    assert json.loads(unstructured)["event"] == "unstructured_log"
    assert "credential" not in unstructured
    assert "secret" not in unstructured


def test_metrics_render_openmetrics_histogram_and_escape_labels() -> None:
    metrics = MetricsRegistry()
    metrics.increment("modall_requests_total", route='a\\"b')
    metrics.gauge("modall_in_flight", 2)
    metrics.observe_http(0.02, method="GET", route="/health")

    rendered = metrics.render()

    assert rendered.count("# TYPE modall_requests_total counter") == 1
    assert 'route="a\\\\\\"b"' in rendered
    assert "modall_in_flight 2" in rendered
    assert 'modall_http_request_duration_seconds_bucket{le="0.025"' in rendered
    assert "modall_http_request_duration_seconds_count" in rendered
    assert rendered.endswith("# EOF\n")


def test_worker_metrics_server_exposes_only_known_paths() -> None:
    metrics = MetricsRegistry()
    server = start_metrics_server(metrics, host="127.0.0.1", port=0)
    host, port = cast(tuple[str, int], server.server_address)
    try:
        with urlopen(f"http://{host}:{port}/health/live", timeout=2) as response:
            assert json.load(response) == {"status": "ok", "service": "worker"}
        with urlopen(f"http://{host}:{port}/metrics", timeout=2) as response:
            assert b"modall_build_info" in response.read()
        with pytest.raises(HTTPError) as missing:
            urlopen(f"http://{host}:{port}/unknown", timeout=2)
        assert missing.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_api_metrics_and_rate_limit_are_payload_free() -> None:
    async def scenario() -> None:
        async def ready() -> bool:
            return True

        settings = Settings(_env_file=None, environment="test", api_rate_limit_per_minute=1)
        metrics = MetricsRegistry()
        app = create_app(settings, readiness_probe=ready, metrics=metrics)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.get("/v1/not-a-real-resource")
            second = await client.get("/v1/not-a-real-resource")
            exposed = await client.get("/metrics")

        assert first.status_code == 404
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "rate_limited"
        assert second.headers["Retry-After"] == "60"
        assert 'route="unmatched"' in exposed.text
        assert "not-a-real-resource" not in exposed.text

    asyncio.run(scenario())


def test_api_concurrency_bound_does_not_block_health() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def ready() -> bool:
            return True

        app = create_app(
            Settings(
                _env_file=None,
                environment="test",
                api_max_concurrency=1,
                api_queue_timeout_seconds=0.01,
            ),
            readiness_probe=ready,
        )

        @app.get("/v1/slow")
        async def slow() -> dict[str, str]:
            entered.set()
            await release.wait()
            return {"status": "done"}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(client.get("/v1/slow"))
            await entered.wait()
            overloaded = await client.get("/v1/slow")
            live = await client.get("/health/live")
            release.set()
            completed = await first

        assert overloaded.status_code == 503
        assert overloaded.json()["error"]["code"] == "capacity_exceeded"
        assert live.status_code == 200
        assert completed.status_code == 200

    asyncio.run(scenario())


def test_ops_parser_requires_destructive_confirmations() -> None:
    parser = _parser()

    assert parser.parse_args(["restore-enter", "--confirm", "RESTORE"]).command == "restore-enter"
    assert parser.parse_args(["restore-clear", "--confirm", "CLEAR"]).command == "restore-clear"
    with pytest.raises(SystemExit):
        parser.parse_args(["restore-enter"])


def test_ops_status_does_not_require_secret_keyrings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        engine = create_database_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        monkeypatch.setattr(cli, "create_engine", lambda _: engine)
        monkeypatch.setattr(
            cli,
            "build_execution_keyrings",
            lambda _: (_ for _ in ()).throw(AssertionError("status loaded secrets")),
        )

        result = await cli.execute(
            Settings(_env_file=None, environment="test"),
            _parser().parse_args(["status"]),
        )

        assert result == {
            "dispatch_quarantined": False,
            "execution_epoch": None,
            "active_runs": 0,
        }

    asyncio.run(scenario())

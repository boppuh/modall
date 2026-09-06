import asyncio
import logging

import pytest

from modall.config import Settings
from modall.persistence.database import create_engine
from modall.worker import main
from modall.worker.main import configure_logging, run_once


def test_worker_poll_emits_no_payload(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(environment="test", log_level="DEBUG")
    configure_logging(settings)

    with caplog.at_level(logging.DEBUG):
        run_once(settings)

    assert "worker_poll environment=test" in caplog.text


def test_worker_run_polls_with_configured_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(environment="test", worker_poll_interval_seconds=0.25)
    runs: list[Settings] = []

    monkeypatch.setattr(main, "get_settings", lambda: settings)

    async def fake_worker(resolved: Settings) -> None:
        runs.append(resolved)

    monkeypatch.setattr(main, "run_worker", fake_worker)

    main.run()

    assert runs == [settings]


def test_worker_runs_global_registry_cache_cleanup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario(*, cleanup_outcome: str) -> None:
        settings = Settings(
            environment="test",
            worker_poll_interval_seconds=0.25,
            worker_maintenance_timeout_seconds=0.01,
        )
        engine = create_engine("sqlite+aiosqlite:///:memory:")
        cleanups = 0
        sleeps: list[float] = []

        async def cleanup(session: object) -> None:
            nonlocal cleanups
            del session
            cleanups += 1
            if cleanup_outcome == "fails":
                raise RuntimeError("database detail")
            if cleanup_outcome == "hangs":
                await asyncio.Event().wait()

        async def stop(seconds: float) -> None:
            sleeps.append(seconds)
            raise asyncio.CancelledError

        monkeypatch.setattr(main, "create_engine", lambda database_url: engine)
        monkeypatch.setattr(main, "purge_expired_registry_cache", cleanup)
        monkeypatch.setattr(asyncio, "sleep", stop)
        with pytest.raises(asyncio.CancelledError):
            await main.run_worker(settings)
        assert cleanups == 1
        assert sleeps == [0.25]

    asyncio.run(scenario(cleanup_outcome="succeeds"))
    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario(cleanup_outcome="fails"))
        asyncio.run(scenario(cleanup_outcome="hangs"))
    assert "registry_cache_cleanup_failed" in caplog.text
    assert "database detail" not in caplog.text

import asyncio
import logging

import pytest

from modall.config import Settings
from modall.execution.runner import InvocationRunner
from modall.execution.service import ExecutionService
from modall.persistence.database import create_engine, create_session_factory
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
        result_cleanups = 0
        argument_cleanups = 0
        metadata_cleanups = 0
        invocation_polls = 0
        sleeps: list[float] = []

        class FakeRunner:
            async def claim_and_run(self, **kwargs: object) -> bool:
                nonlocal invocation_polls
                del kwargs
                invocation_polls += 1
                return False

        class FakeExecutionService:
            async def expire_retained_results(self) -> int:
                nonlocal result_cleanups
                result_cleanups += 1
                return 0

            async def expire_retained_content(self) -> int:
                nonlocal argument_cleanups
                argument_cleanups += 1
                return 0

            async def delete_expired_run_metadata(self) -> int:
                nonlocal metadata_cleanups
                metadata_cleanups += 1
                return 0

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
        monkeypatch.setattr(
            main,
            "build_execution_runtime",
            lambda settings, session_factory: (
                FakeRunner(),
                lambda session: FakeExecutionService(),
            ),
        )
        monkeypatch.setattr(main, "purge_expired_registry_cache", cleanup)
        monkeypatch.setattr(asyncio, "sleep", stop)
        with pytest.raises(asyncio.CancelledError):
            await main.run_worker(settings)
        assert cleanups == 1
        assert invocation_polls == 1
        assert result_cleanups == 1
        assert argument_cleanups == 1
        assert metadata_cleanups == 1
        assert sleeps == [0.25]

    asyncio.run(scenario(cleanup_outcome="succeeds"))
    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario(cleanup_outcome="fails"))
    assert "registry_cache_cleanup_failed" in caplog.text
    assert "database detail" not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario(cleanup_outcome="hangs"))
    assert "registry_cache_cleanup_failed" in caplog.text
    assert "database detail" not in caplog.text


def test_worker_builds_secret_backed_invocation_runtime() -> None:
    async def scenario() -> None:
        engine = create_engine("sqlite+aiosqlite:///:memory:")
        try:
            session_factory = create_session_factory(engine)
            runner, service_factory = main.build_execution_runtime(
                Settings(environment="test", _env_file=None), session_factory
            )
            assert isinstance(runner, InvocationRunner)
            async with session_factory() as session:
                assert isinstance(service_factory(session), ExecutionService)
        finally:
            await engine.dispose()

    asyncio.run(scenario())

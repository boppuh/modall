import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modall.persistence import database
from modall.persistence.database import (
    DatabaseProbe,
    alembic_database_url,
    async_database_url,
    create_engine,
    create_session_factory,
    register_after_rollback,
    transaction,
)
from modall.persistence.migration_config import load_migration_database_url


def test_async_database_url_selects_asyncpg() -> None:
    assert (
        async_database_url("postgresql://user:pass@db/database")
        == "postgresql+asyncpg://user:pass@db/database"
    )
    assert (
        async_database_url("postgresql+psycopg://user:pass@db/database")
        == "postgresql+asyncpg://user:pass@db/database"
    )
    assert (
        async_database_url("postgres://user:pass@db/database")
        == "postgresql+asyncpg://user:pass@db/database"
    )
    assert (
        async_database_url("postgresql+asyncpg://user:pass@db/database")
        == "postgresql+asyncpg://user:pass@db/database"
    )
    assert async_database_url("sqlite+aiosqlite:///:memory:") == "sqlite+aiosqlite:///:memory:"


def test_alembic_database_url_escapes_configparser_interpolation() -> None:
    assert (
        alembic_database_url("postgresql://user:p%40ss@db/database")
        == "postgresql+asyncpg://user:p%%40ss@db/database"
    )


def test_alembic_uses_database_only_settings_in_deployed_mode() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "MODALL_ENVIRONMENT": "production",
            "MODALL_DATABASE_URL": "postgresql://user:p%40ss@localhost/database",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert (
        "ALTER TABLE capability_versions ADD COLUMN schema_supported "
        "BOOLEAN DEFAULT false NOT NULL;"
    ) in completed.stdout


def test_migration_database_url_loads_repository_env_without_runtime_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODALL_DATABASE_URL", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MODALL_ENVIRONMENT=production\n"
        "MODALL_DATABASE_URL=postgresql://env-user:env-pass@db/env-db\n"
    )
    assert (
        load_migration_database_url(
            fallback="postgresql://fallback/db",
            env_file=env_file,
        )
        == "postgresql://env-user:env-pass@db/env-db"
    )


def test_migration_database_url_prefers_mounted_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODALL_DATABASE_URL", raising=False)
    monkeypatch.delenv("MODALL_DATABASE_URL_FILE", raising=False)
    secret = tmp_path / "database-url"
    secret.write_text("postgresql://secret-user:secret-pass@db/secret-db")
    env_file = tmp_path / ".env"
    env_file.write_text(f"MODALL_DATABASE_URL_FILE={secret}\n")

    assert (
        load_migration_database_url(fallback="postgresql://fallback/db", env_file=env_file)
        == "postgresql://secret-user:secret-pass@db/secret-db"
    )


def test_migration_database_url_rejects_relative_deployed_secret_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MODALL_DATABASE_URL_FILE", raising=False)
    monkeypatch.delenv("MODALL_ENVIRONMENT", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MODALL_ENVIRONMENT=staging\nMODALL_DATABASE_URL_FILE=relative-database-url\n"
    )

    with pytest.raises(ValueError, match="must be absolute"):
        load_migration_database_url(fallback="postgresql://fallback/db", env_file=env_file)


def test_database_probe_reports_ready_and_closes() -> None:
    async def scenario() -> None:
        engine = create_engine("sqlite+aiosqlite:///:memory:")
        probe = DatabaseProbe(engine)
        assert await probe.ready() is True
        await probe.close()

    asyncio.run(scenario())


def test_database_probe_fails_closed(tmp_path: Path) -> None:
    async def scenario() -> None:
        missing_parent = tmp_path / "missing" / "database.sqlite"
        engine: AsyncEngine = create_engine(f"sqlite+aiosqlite:///{missing_parent}")
        probe = DatabaseProbe(engine)
        assert await probe.ready() is False
        await probe.close()

    asyncio.run(scenario())


def test_database_probe_bounds_hanging_connections() -> None:
    class HangingConnection:
        async def __aenter__(self) -> None:
            await asyncio.Event().wait()

        async def __aexit__(self, *args: object) -> None:
            return None

    class HangingEngine:
        def connect(self) -> HangingConnection:
            return HangingConnection()

        async def dispose(self) -> None:
            return None

    async def scenario() -> None:
        probe = DatabaseProbe(cast(AsyncEngine, HangingEngine()), timeout_seconds=0.01)
        assert await probe.ready() is False
        await probe.close()

    asyncio.run(scenario())


def test_database_probe_rejects_invalid_timeout() -> None:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    try:
        for timeout in (0, float("inf"), float("nan")):
            with pytest.raises(ValueError):
                DatabaseProbe(engine, timeout_seconds=timeout)
    finally:
        asyncio.run(engine.dispose())


def test_after_rollback_cleanup_is_bounded_and_preserves_original_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        engine = create_engine("sqlite+aiosqlite:///:memory:")
        factory = create_session_factory(engine)
        cleanups_started = 0

        async def hanging_cleanup(session: AsyncSession) -> None:
            nonlocal cleanups_started
            del session
            cleanups_started += 1
            await asyncio.Event().wait()

        monkeypatch.setattr(database, "_AFTER_ROLLBACK_TIMEOUT_SECONDS", 0.01)
        try:
            with pytest.raises(RuntimeError, match="original failure"):
                async with transaction(factory) as session:
                    register_after_rollback(session, hanging_cleanup)
                    register_after_rollback(session, hanging_cleanup)
                    raise RuntimeError("original failure")
            assert cleanups_started == 1
        finally:
            await engine.dispose()

    with caplog.at_level(logging.WARNING, logger="modall.persistence"):
        asyncio.run(scenario())
    assert "after_rollback_cleanup_failed" in caplog.text
    assert "original failure" not in caplog.text


def test_after_rollback_cleanup_finishes_before_cancellation_propagates() -> None:
    async def scenario() -> None:
        engine = create_engine("sqlite+aiosqlite:///:memory:")
        factory = create_session_factory(engine)
        cleanup_started = asyncio.Event()
        finish_cleanup = asyncio.Event()
        cleanup_finished = False

        async def delayed_cleanup(session: AsyncSession) -> None:
            nonlocal cleanup_finished
            del session
            cleanup_started.set()
            await finish_cleanup.wait()
            cleanup_finished = True

        async def failing_transaction() -> None:
            async with transaction(factory) as session:
                register_after_rollback(session, delayed_cleanup)
                raise RuntimeError("original failure")

        try:
            task = asyncio.create_task(failing_transaction())
            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            task.cancel()
            await asyncio.sleep(0)
            assert task.done() is False
            finish_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
            assert cleanup_finished is True
        finally:
            await engine.dispose()

    asyncio.run(scenario())

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from modall.persistence.database import (
    DatabaseProbe,
    alembic_database_url,
    async_database_url,
    create_engine,
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

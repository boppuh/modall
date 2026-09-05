import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from modall.persistence.database import (
    DatabaseProbe,
    alembic_database_url,
    async_database_url,
    create_engine,
)


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
        async_database_url("postgresql+asyncpg://user:pass@db/database")
        == "postgresql+asyncpg://user:pass@db/database"
    )
    assert async_database_url("sqlite+aiosqlite:///:memory:") == "sqlite+aiosqlite:///:memory:"


def test_alembic_database_url_escapes_configparser_interpolation() -> None:
    assert (
        alembic_database_url("postgresql://user:p%40ss@db/database")
        == "postgresql+asyncpg://user:p%%40ss@db/database"
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

"""Async SQLAlchemy engine and transaction boundaries."""

import asyncio
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(database_url, echo=echo, pool_pre_ping=True)


def async_database_url(database_url: str) -> str:
    scheme, separator, remainder = database_url.partition("://")
    if separator and (scheme in {"postgres", "postgresql"} or scheme.startswith("postgresql+")):
        return f"postgresql+asyncpg://{remainder}"
    return database_url


def alembic_database_url(database_url: str) -> str:
    """Return an async URL escaped for Alembic's ConfigParser-backed config."""

    return async_database_url(database_url).replace("%", "%%")


class DatabaseProbe:
    def __init__(self, engine: AsyncEngine, *, timeout_seconds: float = 2.5) -> None:
        if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise ValueError("database probe timeout must be positive and finite")
        self._engine = engine
        self._timeout_seconds = timeout_seconds

    async def ready(self) -> bool:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    async def close(self) -> None:
        await self._engine.dispose()


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Commit all domain and audit writes together, or roll everything back."""

    async with session_factory() as session, session.begin():
        yield session

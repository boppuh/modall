"""Async SQLAlchemy engine and transaction boundaries."""

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_AFTER_ROLLBACK_CALLBACKS = "modall_after_rollback_callbacks"
_AFTER_ROLLBACK_TIMEOUT_SECONDS = 2.5


def register_after_rollback(
    session: AsyncSession, callback: Callable[[AsyncSession], Awaitable[None]]
) -> None:
    """Register durable cleanup that runs only after this transaction rolls back."""

    callbacks = session.info.get(_AFTER_ROLLBACK_CALLBACKS)
    if not isinstance(callbacks, list):
        raise RuntimeError("session is not managed by the Modall transaction boundary")
    callbacks.append(callback)


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

    async with session_factory() as session:
        callbacks: list[Callable[[AsyncSession], Awaitable[None]]] = []
        session.info[_AFTER_ROLLBACK_CALLBACKS] = callbacks
        try:
            async with session.begin():
                yield session
        except BaseException:
            cleanup = asyncio.create_task(_run_after_rollback_callbacks(session_factory, callbacks))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError from None
            raise


async def _run_after_rollback_callbacks(
    session_factory: async_sessionmaker[AsyncSession],
    callbacks: list[Callable[[AsyncSession], Awaitable[None]]],
) -> None:
    try:
        async with asyncio.timeout(_AFTER_ROLLBACK_TIMEOUT_SECONDS):
            for callback in callbacks:
                try:
                    async with (
                        session_factory() as cleanup_session,
                        cleanup_session.begin(),
                    ):
                        await callback(cleanup_session)
                except Exception:
                    logging.getLogger("modall.persistence").warning("after_rollback_cleanup_failed")
    except TimeoutError:
        logging.getLogger("modall.persistence").warning("after_rollback_cleanup_failed")

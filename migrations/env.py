"""Alembic environment backed by the application settings."""

import asyncio
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from modall.persistence.database import alembic_database_url
from modall.persistence.migration_config import load_migration_database_url
from modall.persistence.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

fallback_database_url = config.get_main_option("sqlalchemy.url")
if not fallback_database_url:
    raise RuntimeError("a database URL is required to run migrations")
database_url = load_migration_database_url(
    fallback=fallback_database_url,
    env_file=Path(__file__).resolve().parents[1] / ".env",
)
config.set_main_option("sqlalchemy.url", alembic_database_url(database_url))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def configure_and_run(connection: object) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(configure_and_run)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

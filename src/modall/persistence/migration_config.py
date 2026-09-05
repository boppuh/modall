"""Database-only configuration for migration processes."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class MigrationSettings(BaseSettings):
    """Load only migration inputs without validating application runtime modes."""

    model_config = SettingsConfigDict(env_prefix="MODALL_", extra="ignore")

    database_url: str | None = None


def load_migration_database_url(*, fallback: str, env_file: Path) -> str:
    settings = MigrationSettings(_env_file=env_file)
    return settings.database_url or fallback

"""Database-only configuration for migration processes."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from modall.config import read_database_url_secret


class MigrationSettings(BaseSettings):
    """Load only migration inputs without validating application runtime modes."""

    model_config = SettingsConfigDict(env_prefix="MODALL_", extra="ignore")

    database_url: str | None = None
    database_url_file: Path | None = None


def load_migration_database_url(*, fallback: str, env_file: Path) -> str:
    settings = MigrationSettings(_env_file=env_file)
    if settings.database_url_file is not None:
        return str(read_database_url_secret(settings.database_url_file))
    return settings.database_url or fallback

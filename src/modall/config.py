"""Process configuration shared by the API and worker."""

from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed process settings with an explicit Modall prefix."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="MODALL_",
        extra="ignore",
    )

    environment: Literal["local", "test", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    database_url: PostgresDsn = PostgresDsn("postgresql://modall:modall@localhost:5432/modall")
    worker_poll_interval_seconds: float = 1.0


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings object per process."""

    return Settings()

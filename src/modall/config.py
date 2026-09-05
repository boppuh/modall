"""Process configuration shared by the API and worker."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, PostgresDsn, model_validator
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
    worker_poll_interval_seconds: Annotated[float, Field(gt=0, le=60, allow_inf_nan=False)] = 1.0
    auth_mode: Literal["local", "oidc"] = "local"
    oidc_issuer: HttpUrl | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: HttpUrl | None = None
    local_subject: str = "local-developer"
    secret_provider: Literal["fixture", "mounted_file"] = "fixture"
    secret_mount_root: Path = Path("/run/secrets")

    @model_validator(mode="after")
    def validate_security_modes(self) -> "Settings":
        """Prevent development identity or secret fixtures in deployed modes."""

        deployed = self.environment in {"staging", "production"}
        if deployed and self.auth_mode != "oidc":
            raise ValueError("deployed environments require OIDC authentication")
        if deployed and self.secret_provider != "mounted_file":
            raise ValueError("deployed environments require the mounted-file secret provider")
        if self.auth_mode == "oidc" and not all(
            (self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)
        ):
            raise ValueError("OIDC mode requires issuer, audience, and JWKS URL")
        if self.auth_mode == "oidc" and (
            self.oidc_issuer is not None
            and self.oidc_jwks_url is not None
            and (self.oidc_issuer.scheme != "https" or self.oidc_jwks_url.scheme != "https")
        ):
            raise ValueError("OIDC issuer and JWKS URL require HTTPS")
        if not self.local_subject.strip():
            raise ValueError("local subject must not be blank")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings object per process."""

    return Settings()

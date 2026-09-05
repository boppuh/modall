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
    # Preserve the issuer byte-for-byte for OIDC's exact identifier comparison.
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: HttpUrl | None = None
    local_subject: str = "local-developer"
    secret_provider: Literal["fixture", "mounted_file"] = "fixture"
    secret_mount_root: Path = Path("/run/secrets")

    @model_validator(mode="after")
    def validate_security_modes(self) -> "Settings":
        """Prevent development identity or secret fixtures in deployed modes."""

        issuer_url: HttpUrl | None = None
        if self.oidc_issuer is not None:
            if self.oidc_issuer != self.oidc_issuer.strip() or len(self.oidc_issuer) > 512:
                raise ValueError("OIDC issuer must be at most 512 characters without whitespace")
            issuer_url = HttpUrl(self.oidc_issuer)
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
            issuer_url is not None
            and self.oidc_jwks_url is not None
            and (
                issuer_url.scheme != "https"
                or issuer_url.username is not None
                or issuer_url.password is not None
                or issuer_url.query is not None
                or issuer_url.fragment is not None
                or self.oidc_jwks_url.scheme != "https"
            )
        ):
            raise ValueError("OIDC issuer or JWKS URL is not conforming")
        if not self.local_subject.strip() or len(self.local_subject) > 512:
            raise ValueError("local subject must contain between 1 and 512 characters")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings object per process."""

    return Settings()

"""Process configuration shared by the API and worker."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, HttpUrl, IPvAnyAddress, PostgresDsn, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_KEY_VERSION = re.compile(r"[A-Za-z0-9._-]{1,32}\Z")


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
    worker_maintenance_timeout_seconds: Annotated[
        float, Field(gt=0, le=60, allow_inf_nan=False)
    ] = 5.0
    worker_maintenance_interval_seconds: Annotated[
        float, Field(gt=0, le=3600, allow_inf_nan=False)
    ] = 60.0
    worker_lease_duration_seconds: Annotated[float, Field(ge=15, le=300, allow_inf_nan=False)] = (
        30.0
    )
    worker_metrics_port: Annotated[int, Field(ge=1024, le=65535)] = 9101
    api_max_concurrency: Annotated[int, Field(ge=1, le=1024)] = 64
    api_queue_timeout_seconds: Annotated[float, Field(gt=0, le=10, allow_inf_nan=False)] = 0.25
    api_rate_limit_per_minute: Annotated[int, Field(ge=1, le=100_000)] = 600
    trusted_proxy_addresses: tuple[IPvAnyAddress, ...] = ()
    max_argument_bytes: Annotated[int, Field(ge=1_024, le=1_048_576)] = 65_536
    max_result_bytes: Annotated[int, Field(ge=1_024, le=1_048_576)] = 262_144
    confirmation_ttl_seconds: Annotated[int, Field(ge=1, le=300)] = 120
    max_run_seconds: Annotated[int, Field(ge=1, le=3600)] = 300
    argument_retention_days: Annotated[int, Field(ge=1, le=14)] = 14
    result_retention_days: Annotated[int, Field(ge=1, le=14)] = 14
    run_retention_days: Annotated[int, Field(ge=14, le=365)] = 90
    max_active_runs_per_workspace: Annotated[int, Field(ge=1, le=100)] = 100
    reconciliation_batch_size: Annotated[int, Field(ge=1, le=1000)] = 100
    schema_validation_timeout_seconds: Annotated[float, Field(gt=0, le=5, allow_inf_nan=False)] = (
        2.0
    )
    schema_validation_memory_bytes: Annotated[int, Field(ge=67_108_864, le=268_435_456)] = (
        268_435_456
    )
    confirmation_hmac_key_versions: tuple[str, ...] = ("v1",)
    idempotency_hmac_key_versions: tuple[str, ...] = ("v1",)
    cors_allowed_origins: tuple[str, ...] = ()
    auth_mode: Literal["local", "oidc"] = "local"
    # Preserve the issuer byte-for-byte for OIDC's exact identifier comparison.
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: HttpUrl | None = None
    local_subject: str = "local-developer"
    secret_provider: Literal["fixture", "mounted_file"] = "fixture"
    secret_mount_root: Path = Path("/run/secrets")
    fixture_secret_root: Path | None = None

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
        if deployed and not self.trusted_proxy_addresses:
            raise ValueError("deployed environments require at least one trusted ingress proxy")
        if deployed and self.fixture_secret_root is not None:
            raise ValueError("deployed environments cannot configure fixture secrets")
        if self.secret_provider != "fixture" and self.fixture_secret_root is not None:
            raise ValueError("fixture secret root requires the fixture provider")
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
        for versions in (
            self.confirmation_hmac_key_versions,
            self.idempotency_hmac_key_versions,
        ):
            if (
                not 1 <= len(versions) <= 8
                or len(set(versions)) != len(versions)
                or any(_KEY_VERSION.fullmatch(version) is None for version in versions)
            ):
                raise ValueError("invalid HMAC key versions")
        for origin in self.cors_allowed_origins:
            parsed_origin = HttpUrl(origin)
            canonical_origin = str(parsed_origin).rstrip("/")
            if (
                origin != canonical_origin
                or parsed_origin.username is not None
                or parsed_origin.password is not None
                or parsed_origin.query is not None
                or parsed_origin.fragment is not None
                or parsed_origin.path not in {None, "/"}
            ):
                raise ValueError("CORS origins must be bare HTTP origins")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings object per process."""

    return Settings()

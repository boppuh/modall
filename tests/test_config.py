import math

import pytest
from pydantic import ValidationError

from modall.config import Settings


def test_settings_use_safe_local_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "local"
    assert settings.log_level == "INFO"
    assert settings.worker_poll_interval_seconds == 1.0
    assert str(settings.database_url) == "postgresql://modall:modall@localhost:5432/modall"


@pytest.mark.parametrize("interval", [0, -1, math.inf, math.nan, 60.1])
def test_settings_reject_unsafe_poll_intervals(interval: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, worker_poll_interval_seconds=interval)


@pytest.mark.parametrize(
    "overrides",
    [
        {"environment": "production"},
        {
            "environment": "production",
            "auth_mode": "oidc",
            "oidc_issuer": "https://issuer.example",
            "oidc_audience": "modall",
            "oidc_jwks_url": "https://issuer.example/jwks",
        },
        {"auth_mode": "oidc"},
        {
            "auth_mode": "oidc",
            "oidc_issuer": "http://issuer.example",
            "oidc_audience": "modall",
            "oidc_jwks_url": "https://issuer.example/jwks",
        },
        {"local_subject": "  "},
    ],
)
def test_settings_reject_confused_security_modes(overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_deployed_security_mode_requires_oidc_and_mounted_secrets() -> None:
    settings = Settings(
        _env_file=None,
        environment="staging",
        auth_mode="oidc",
        oidc_issuer="https://issuer.example",
        oidc_audience="modall",
        oidc_jwks_url="https://issuer.example/jwks",
        secret_provider="mounted_file",
    )

    assert settings.auth_mode == "oidc"
    assert settings.secret_provider == "mounted_file"

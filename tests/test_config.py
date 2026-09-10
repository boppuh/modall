import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from modall.config import Settings
from modall.execution.runtime import build_execution_limits


def test_settings_use_safe_local_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODALL_DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)

    assert settings.environment == "local"
    assert settings.log_level == "INFO"
    assert settings.worker_poll_interval_seconds == 1.0
    assert settings.worker_maintenance_timeout_seconds == 5.0
    assert settings.worker_maintenance_interval_seconds == 60.0
    assert str(settings.database_url) == "postgresql://modall:modall@localhost:5432/modall"


def test_execution_limits_are_built_from_configuration() -> None:
    settings = Settings(
        _env_file=None,
        max_argument_bytes=2048,
        max_result_bytes=4096,
        confirmation_ttl_seconds=30,
        max_run_seconds=60,
        argument_retention_days=3,
        result_retention_days=4,
        run_retention_days=30,
        max_active_runs_per_workspace=7,
        reconciliation_batch_size=11,
        schema_validation_timeout_seconds=1.5,
        schema_validation_memory_bytes=67_108_864,
    )

    limits = build_execution_limits(settings)

    assert limits.max_argument_bytes == 2048
    assert limits.max_result_bytes == 4096
    assert limits.confirmation_ttl_seconds == 30
    assert limits.max_run_seconds == 60
    assert limits.argument_retention_days == 3
    assert limits.result_retention_days == 4
    assert limits.run_retention_days == 30
    assert limits.max_active_runs_per_workspace == 7
    assert limits.reconciliation_batch_size == 11
    assert limits.schema_validation_timeout_seconds == 1.5
    assert limits.schema_validation_memory_bytes == 67_108_864


@pytest.mark.parametrize("interval", [0, -1, math.inf, math.nan, 60.1])
def test_settings_reject_unsafe_poll_intervals(interval: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, worker_poll_interval_seconds=interval)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, worker_maintenance_timeout_seconds=interval)


@pytest.mark.parametrize("interval", [0, -1, math.inf, math.nan, 3600.1])
def test_settings_reject_unsafe_maintenance_intervals(interval: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, worker_maintenance_interval_seconds=interval)


@pytest.mark.parametrize("lease_seconds", [0, 10, 10.001, 14.999, math.inf, math.nan, 300.1])
def test_worker_lease_includes_invocation_finalization_margin(lease_seconds: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, worker_lease_duration_seconds=lease_seconds)


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
            "oidc_issuer": " https://issuer.example",
            "oidc_audience": "modall",
            "oidc_jwks_url": "https://issuer.example/jwks",
        },
        {
            "auth_mode": "oidc",
            "oidc_issuer": "http://issuer.example",
            "oidc_audience": "modall",
            "oidc_jwks_url": "https://issuer.example/jwks",
        },
        {"local_subject": "  "},
        {"local_subject": "s" * 513},
        {"secret_provider": "mounted_file", "fixture_secret_root": "/tmp/fixtures"},
        {
            "environment": "production",
            "auth_mode": "oidc",
            "oidc_issuer": "https://issuer.example",
            "oidc_audience": "modall",
            "oidc_jwks_url": "https://issuer.example/jwks",
            "secret_provider": "mounted_file",
            "fixture_secret_root": "/tmp/fixtures",
        },
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
        trusted_proxy_addresses=("10.0.0.10",),
        metrics_trusted_peer_addresses=("10.0.1.10",),
    )

    assert settings.auth_mode == "oidc"
    assert settings.oidc_issuer == "https://issuer.example"
    assert settings.secret_provider == "mounted_file"
    assert tuple(map(str, settings.trusted_proxy_addresses)) == ("10.0.0.10",)

    with pytest.raises(ValidationError, match="trusted metrics peer"):
        Settings(
            _env_file=None,
            environment="staging",
            auth_mode="oidc",
            oidc_issuer="https://issuer.example",
            oidc_audience="modall",
            oidc_jwks_url="https://issuer.example/jwks",
            secret_provider="mounted_file",
            trusted_proxy_addresses=("10.0.0.10",),
        )


def test_cloudflare_access_mode_requires_a_deployed_trusted_proxy() -> None:
    with pytest.raises(ValidationError, match="Cloudflare Access"):
        Settings(_env_file=None, auth_token_source="cloudflare_access")

    settings = Settings(
        _env_file=None,
        environment="staging",
        auth_mode="oidc",
        auth_token_source="cloudflare_access",
        oidc_issuer="https://team.cloudflareaccess.com",
        oidc_audience="access-audience",
        oidc_jwks_url="https://team.cloudflareaccess.com/cdn-cgi/access/certs",
        secret_provider="mounted_file",
        trusted_proxy_addresses=("172.30.0.10",),
        metrics_trusted_peer_addresses=("172.31.0.10",),
    )

    assert settings.auth_token_source == "cloudflare_access"


def test_database_url_can_be_loaded_from_a_bounded_secret_file(tmp_path: Path) -> None:
    secret = tmp_path / "database-url"
    secret.write_text("postgresql://staging:secret@database.example/modall")

    settings = Settings(_env_file=None, database_url_file=secret)

    assert str(settings.database_url) == "postgresql://staging:secret@database.example/modall"


def test_worker_metrics_host_requires_ipv4() -> None:
    assert str(Settings(_env_file=None, worker_metrics_host="127.0.0.1").worker_metrics_host) == (
        "127.0.0.1"
    )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, worker_metrics_host="::1")


@pytest.mark.parametrize("content", ("", " postgresql://db/modall", "postgresql://db/modall\n"))
def test_database_url_secret_rejects_invalid_content(tmp_path: Path, content: str) -> None:
    secret = tmp_path / "database-url"
    secret.write_text(content)

    with pytest.raises(ValidationError, match="database URL secret"):
        Settings(_env_file=None, database_url_file=secret)


def test_database_url_secret_rejects_missing_or_oversized_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ValidationError, match="database URL secret"):
        Settings(_env_file=None, database_url_file=missing)

    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"x" * 2049)
    with pytest.raises(ValidationError, match="database URL secret"):
        Settings(_env_file=None, database_url_file=oversized)


def test_deployed_database_url_secret_path_must_be_absolute() -> None:
    with pytest.raises(ValidationError, match="must be absolute"):
        Settings(
            _env_file=None,
            environment="staging",
            database_url_file=Path("database-url"),
            auth_mode="oidc",
            oidc_issuer="https://issuer.example",
            oidc_audience="modall",
            oidc_jwks_url="https://issuer.example/jwks",
            secret_provider="mounted_file",
            trusted_proxy_addresses=("10.0.0.10",),
        )


@pytest.mark.parametrize(
    "versions",
    [(), ("duplicate", "duplicate"), ("contains space",), tuple(str(i) for i in range(9))],
)
def test_hmac_key_versions_are_bounded_and_unique(versions: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError, match="invalid HMAC key versions"):
        Settings(_env_file=None, confirmation_hmac_key_versions=versions)


@pytest.mark.parametrize(
    "origin",
    [
        "HTTP://LOCALHOST:5173",
        "http://example.com:80",
        "https://example.com/",
        "https://user@example.com",
        "https://example.com/path",
        "https://example.com?tenant=x",
    ],
)
def test_cors_origins_must_be_canonical_bare_origins(origin: str) -> None:
    with pytest.raises(ValidationError, match="bare HTTP origins"):
        Settings(_env_file=None, cors_allowed_origins=(origin,))

    assert Settings(
        _env_file=None, cors_allowed_origins=("http://localhost:5173",)
    ).cors_allowed_origins == ("http://localhost:5173",)


@pytest.mark.parametrize(
    "issuer",
    [
        "https://user@issuer.example",
        "https://issuer.example?tenant=x",
        "https://issuer.example#fragment",
    ],
)
def test_oidc_issuer_rejects_forbidden_components(issuer: str) -> None:
    with pytest.raises(ValidationError, match="not conforming"):
        Settings(
            _env_file=None,
            environment="test",
            auth_mode="oidc",
            oidc_issuer=issuer,
            oidc_audience="modall",
            oidc_jwks_url="https://issuer.example/jwks",
        )

from modall.config import Settings


def test_settings_use_safe_local_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.environment == "local"
    assert settings.log_level == "INFO"
    assert settings.worker_poll_interval_seconds == 1.0
    assert str(settings.database_url) == "postgresql://modall:modall@localhost:5432/modall"

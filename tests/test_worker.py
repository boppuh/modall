import logging

import pytest

from modall.config import Settings
from modall.worker import main
from modall.worker.main import configure_logging, run_once


def test_worker_poll_emits_no_payload(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(environment="test", log_level="DEBUG")
    configure_logging(settings)

    with caplog.at_level(logging.DEBUG):
        run_once(settings)

    assert "worker_poll environment=test" in caplog.text


def test_worker_run_polls_with_configured_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(environment="test", worker_poll_interval_seconds=0.25)
    polls: list[Settings] = []
    sleeps: list[float] = []

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "run_once", polls.append)

    def stop_after_first_poll(seconds: float) -> None:
        sleeps.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr("modall.worker.main.time.sleep", stop_after_first_poll)

    with pytest.raises(KeyboardInterrupt):
        main.run()

    assert polls == [settings]
    assert sleeps == [0.25]

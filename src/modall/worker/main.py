"""Worker process entry point.

The durable PostgreSQL lease loop lands with the execution ledger. This process
shell exists now so packaging, deployment, and observability conventions are
validated before domain work depends on them.
"""

import logging
import time

from modall.config import Settings, get_settings


def configure_logging(settings: Settings) -> None:
    """Configure payload-free process logging."""

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def run_once(settings: Settings) -> None:
    """Execute one scaffold poll without claiming domain work."""

    logging.getLogger("modall.worker").debug("worker_poll environment=%s", settings.environment)


def run() -> None:
    """Run the worker shell until the process receives a termination signal."""

    settings = get_settings()
    configure_logging(settings)
    logger = logging.getLogger("modall.worker")
    logger.info("worker_started environment=%s", settings.environment)
    while True:
        run_once(settings)
        time.sleep(settings.worker_poll_interval_seconds)


if __name__ == "__main__":
    run()

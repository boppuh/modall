"""Worker process entry point.

The durable PostgreSQL lease loop lands with the execution ledger. This process
shell exists now so packaging, deployment, and observability conventions are
validated before domain work depends on them.
"""

import asyncio
import logging

from modall.config import Settings, get_settings
from modall.persistence.database import (
    async_database_url,
    create_engine,
    create_session_factory,
    transaction,
)
from modall.registry.official import purge_expired_registry_cache


def configure_logging(settings: Settings) -> None:
    """Configure payload-free process logging."""

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def run_once(settings: Settings) -> None:
    """Execute one scaffold poll without claiming domain work."""

    logging.getLogger("modall.worker").debug("worker_poll environment=%s", settings.environment)


async def run_worker(settings: Settings) -> None:
    """Poll durable maintenance work with one reusable database pool."""

    logger = logging.getLogger("modall.worker")
    engine = create_engine(async_database_url(str(settings.database_url)))
    session_factory = create_session_factory(engine)
    try:
        while True:
            run_once(settings)
            try:
                async with asyncio.timeout(settings.worker_maintenance_timeout_seconds):
                    async with transaction(session_factory) as session:
                        await purge_expired_registry_cache(session)
            except Exception:
                logger.warning("registry_cache_cleanup_failed")
            await asyncio.sleep(settings.worker_poll_interval_seconds)
    finally:
        await engine.dispose()


def run() -> None:
    """Run the worker shell until the process receives a termination signal."""

    settings = get_settings()
    configure_logging(settings)
    logger = logging.getLogger("modall.worker")
    logger.info("worker_started environment=%s", settings.environment)
    asyncio.run(run_worker(settings))


if __name__ == "__main__":
    run()

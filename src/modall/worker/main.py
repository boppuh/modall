"""Durable invocation and bounded-maintenance worker entry point."""

import asyncio
import logging
import os
import socket
import time
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modall.config import Settings, get_settings
from modall.execution.runner import ExecutionServiceFactory, InvocationRunner
from modall.execution.runtime import build_execution_keyrings
from modall.execution.service import ExecutionService
from modall.execution.types import ExecutionLimits
from modall.mcp_adapter.client import McpClientAdapter
from modall.mcp_adapter.policy import EndpointPolicy, TransportLimits
from modall.persistence.database import (
    async_database_url,
    create_engine,
    create_session_factory,
    transaction,
)
from modall.registry.official import purge_expired_registry_cache
from modall.secrets.provider import build_secret_provider

_INVOCATION_PROTOCOL_OVERHEAD_BYTES = 65_536
_MAX_JSON_ESCAPE_EXPANSION = 6


def configure_logging(settings: Settings) -> None:
    """Configure payload-free process logging."""

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def run_once(settings: Settings) -> None:
    """Emit one payload-free poll marker."""

    logging.getLogger("modall.worker").debug("worker_poll environment=%s", settings.environment)


async def run_worker(settings: Settings) -> None:
    """Poll durable maintenance work with one reusable database pool."""

    logger = logging.getLogger("modall.worker")
    engine = create_engine(async_database_url(str(settings.database_url)))
    session_factory = create_session_factory(engine)
    try:
        invocation_runner, execution_service_factory = build_execution_runtime(
            settings, session_factory
        )
        worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
        next_maintenance_at = 0.0
        while True:
            run_once(settings)
            work_claimed = False
            try:
                work_claimed = await invocation_runner.claim_and_run(
                    worker_id=worker_id,
                    lease_duration=timedelta(seconds=settings.worker_lease_duration_seconds),
                )
            except Exception:
                logger.warning("invocation_poll_failed")
            now = time.monotonic()
            if now >= next_maintenance_at:
                await _run_maintenance(
                    settings=settings,
                    session_factory=session_factory,
                    execution_service_factory=execution_service_factory,
                )
                next_maintenance_at = now + settings.worker_maintenance_interval_seconds
            if work_claimed:
                continue
            await asyncio.sleep(settings.worker_poll_interval_seconds)
    finally:
        await engine.dispose()


async def _run_maintenance(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    execution_service_factory: ExecutionServiceFactory,
) -> None:
    operations = (
        ("registry_cache_cleanup_failed", purge_expired_registry_cache),
        (
            "result_cleanup_failed",
            lambda session: execution_service_factory(session).expire_retained_results(),
        ),
        (
            "argument_cleanup_failed",
            lambda session: execution_service_factory(session).expire_retained_content(),
        ),
        (
            "run_metadata_cleanup_failed",
            lambda session: execution_service_factory(session).delete_expired_run_metadata(),
        ),
    )
    logger = logging.getLogger("modall.worker")
    for failure_code, operation in operations:
        try:
            async with asyncio.timeout(settings.worker_maintenance_timeout_seconds):
                async with transaction(session_factory) as session:
                    await operation(session)
        except Exception:
            logger.warning(failure_code)


def build_execution_runtime(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[InvocationRunner, ExecutionServiceFactory]:
    """Build one worker-scoped invocation runtime from secret-backed keyrings."""

    secret_provider = build_secret_provider(settings)
    confirmation_keys, idempotency_keys = build_execution_keyrings(settings)
    limits = ExecutionLimits()

    def execution_service_factory(session: AsyncSession) -> ExecutionService:
        return ExecutionService(
            session,
            confirmation_keys=confirmation_keys,
            idempotency_keys=idempotency_keys,
            limits=limits,
        )

    def adapter_factory(policy_version: str) -> McpClientAdapter:
        if policy_version != "v1":
            raise KeyError("unknown endpoint policy version")
        return McpClientAdapter(
            endpoint_policy=EndpointPolicy(environment=settings.environment),
            limits=_invocation_transport_limits(limits),
            max_result_bytes=limits.max_result_bytes,
            schema_validation_timeout_seconds=limits.schema_validation_timeout_seconds,
            schema_validation_memory_bytes=limits.schema_validation_memory_bytes,
        )

    return (
        InvocationRunner(
            session_factory=session_factory,
            execution_service_factory=execution_service_factory,
            secret_provider=secret_provider,
            adapter_factory=adapter_factory,
        ),
        execution_service_factory,
    )


def _invocation_transport_limits(limits: ExecutionLimits) -> TransportLimits:
    """Allow a bounded JSON-escaped result to normalize to the retention limit."""

    return TransportLimits(
        response_bytes=(
            limits.max_result_bytes * _MAX_JSON_ESCAPE_EXPANSION
            + _INVOCATION_PROTOCOL_OVERHEAD_BYTES
        )
    )


def run() -> None:
    """Run the worker shell until the process receives a termination signal."""

    settings = get_settings()
    configure_logging(settings)
    logger = logging.getLogger("modall.worker")
    logger.info("worker_started environment=%s", settings.environment)
    asyncio.run(run_worker(settings))


if __name__ == "__main__":
    run()

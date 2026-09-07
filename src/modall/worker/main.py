"""Durable invocation and bounded-maintenance worker entry point."""

import asyncio
import logging
import os
import socket
import time
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modall.api.idempotency import purge_expired_api_idempotency
from modall.config import Settings, get_settings
from modall.execution.runner import ExecutionServiceFactory, InvocationRunner
from modall.execution.runtime import build_execution_keyrings, build_execution_limits
from modall.execution.service import ExecutionService
from modall.execution.types import ExecutionLimits
from modall.mcp_adapter.client import McpClientAdapter
from modall.mcp_adapter.policy import EndpointPolicy, TransportLimits
from modall.ops.telemetry import (
    MetricsRegistry,
    WorkerLiveness,
    configure_json_logging,
    log_event,
    start_metrics_server,
)
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
_MAINTENANCE_METRIC_OPERATIONS = (
    "registry_cache_cleanup",
    "api_idempotency_cleanup",
    "result_cleanup",
    "argument_cleanup",
    "run_metadata_cleanup",
)


def configure_logging(settings: Settings) -> None:
    """Configure payload-free process logging."""

    configure_json_logging(settings.log_level)


def run_once(settings: Settings) -> None:
    """Emit one payload-free poll marker."""

    log_event(
        logging.getLogger("modall.worker"),
        logging.DEBUG,
        "worker_poll",
        environment=settings.environment,
    )


async def run_worker(settings: Settings) -> None:
    """Poll durable maintenance work with one reusable database pool."""

    logger = logging.getLogger("modall.worker")
    engine = create_engine(async_database_url(str(settings.database_url)))
    session_factory = create_session_factory(engine)
    metrics = MetricsRegistry()
    _initialize_worker_metrics(metrics)
    liveness = WorkerLiveness(
        metrics,
        stale_after_seconds=(
            settings.max_run_seconds
            + settings.worker_lease_duration_seconds
            + (5 * settings.worker_maintenance_timeout_seconds)
            + settings.worker_poll_interval_seconds
            + 30
        ),
    )
    metrics_server = start_metrics_server(
        metrics,
        host="0.0.0.0",
        port=settings.worker_metrics_port,
        liveness_probe=liveness.is_live,
    )
    try:
        invocation_runner, execution_service_factory = build_execution_runtime(
            settings, session_factory, metrics=metrics
        )
        worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"
        next_maintenance_at = 0.0
        while True:
            liveness.touch()
            run_once(settings)
            work_claimed = False
            try:
                work_claimed = await invocation_runner.claim_and_run(
                    worker_id=worker_id,
                    lease_duration=timedelta(seconds=settings.worker_lease_duration_seconds),
                )
            except Exception:
                metrics.increment("modall_worker_polls_total", outcome="failed")
                log_event(logger, logging.WARNING, "invocation_poll_failed")
            else:
                metrics.increment(
                    "modall_worker_polls_total", outcome="claimed" if work_claimed else "idle"
                )
            liveness.touch()
            now = time.monotonic()
            if now >= next_maintenance_at:
                await _run_maintenance(
                    settings=settings,
                    session_factory=session_factory,
                    execution_service_factory=execution_service_factory,
                    metrics=metrics,
                )
                next_maintenance_at = now + settings.worker_maintenance_interval_seconds
                liveness.touch()
            if work_claimed:
                continue
            await asyncio.sleep(settings.worker_poll_interval_seconds)
    finally:
        await asyncio.to_thread(metrics_server.shutdown)
        metrics_server.server_close()
        await engine.dispose()


async def _run_maintenance(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    execution_service_factory: ExecutionServiceFactory,
    metrics: MetricsRegistry | None = None,
) -> dict[str, str]:
    operations = (
        ("registry_cache_cleanup_failed", purge_expired_registry_cache),
        ("api_idempotency_cleanup_failed", purge_expired_api_idempotency),
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
    outcomes: dict[str, str] = {}
    for failure_code, operation in operations:
        operation_name = failure_code.removesuffix("_failed")
        try:
            async with asyncio.timeout(settings.worker_maintenance_timeout_seconds):
                async with transaction(session_factory) as session:
                    await operation(session)
        except Exception:
            outcomes[operation_name] = "failed"
            if metrics is not None:
                metrics.increment(
                    "modall_worker_maintenance_total", operation=operation_name, outcome="failed"
                )
            log_event(logger, logging.WARNING, failure_code)
        else:
            outcomes[operation_name] = "succeeded"
            if metrics is not None:
                metrics.increment(
                    "modall_worker_maintenance_total",
                    operation=operation_name,
                    outcome="succeeded",
                )
    return outcomes


def _initialize_worker_metrics(metrics: MetricsRegistry) -> None:
    for outcome in ("claimed", "failed", "idle"):
        metrics.increment("modall_worker_polls_total", amount=0, outcome=outcome)
    for outcome in ("failed", "indeterminate"):
        metrics.increment(
            "modall_worker_invocations_total",
            amount=0,
            event="invocation_terminal",
            outcome=outcome,
        )
    for operation in _MAINTENANCE_METRIC_OPERATIONS:
        metrics.increment(
            "modall_worker_maintenance_total",
            amount=0,
            operation=operation,
            outcome="failed",
        )


def build_execution_runtime(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    metrics: MetricsRegistry | None = None,
) -> tuple[InvocationRunner, ExecutionServiceFactory]:
    """Build one worker-scoped invocation runtime from secret-backed keyrings."""

    secret_provider = build_secret_provider(settings)
    confirmation_keys, idempotency_keys = build_execution_keyrings(settings)
    limits = build_execution_limits(settings)

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
            endpoint_policy=_invocation_endpoint_policy(settings),
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
            metrics=metrics,
        ),
        execution_service_factory,
    )


def _invocation_endpoint_policy(settings: Settings) -> EndpointPolicy:
    return EndpointPolicy(
        environment=settings.environment,
        allow_loopback_http=settings.environment in {"local", "test"},
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
    log_event(logger, logging.INFO, "worker_started", environment=settings.environment)
    asyncio.run(run_worker(settings))


if __name__ == "__main__":
    run()

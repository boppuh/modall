"""Durable invocation and bounded-maintenance worker entry point."""

import asyncio
import logging
import os
import socket
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modall.config import Settings, get_settings
from modall.execution.runner import ExecutionServiceFactory, InvocationRunner
from modall.execution.service import ExecutionService
from modall.execution.types import ExecutionLimits, HmacKeyVersion
from modall.mcp_adapter.client import McpClientAdapter
from modall.mcp_adapter.policy import EndpointPolicy, TransportLimits
from modall.persistence.database import (
    async_database_url,
    create_engine,
    create_session_factory,
    transaction,
)
from modall.registry.official import purge_expired_registry_cache
from modall.secrets.provider import SecretProvider, SecretReference, build_secret_provider

_CONFIRMATION_KEY_REFERENCE = "system-confirmation-hmac"
_IDEMPOTENCY_KEY_REFERENCE = "system-idempotency-hmac"
_INVOCATION_PROTOCOL_OVERHEAD_BYTES = 65_536


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
        while True:
            run_once(settings)
            try:
                await invocation_runner.claim_and_run(
                    worker_id=worker_id,
                    lease_duration=timedelta(seconds=settings.worker_lease_duration_seconds),
                )
            except Exception:
                logger.warning("invocation_poll_failed")
            try:
                async with asyncio.timeout(settings.worker_maintenance_timeout_seconds):
                    async with transaction(session_factory) as session:
                        await purge_expired_registry_cache(session)
            except Exception:
                logger.warning("registry_cache_cleanup_failed")
            try:
                async with asyncio.timeout(settings.worker_maintenance_timeout_seconds):
                    async with transaction(session_factory) as session:
                        await execution_service_factory(session).expire_retained_results()
            except Exception:
                logger.warning("result_cleanup_failed")
            try:
                async with asyncio.timeout(settings.worker_maintenance_timeout_seconds):
                    async with transaction(session_factory) as session:
                        await execution_service_factory(session).expire_retained_content()
            except Exception:
                logger.warning("argument_cleanup_failed")
            try:
                async with asyncio.timeout(settings.worker_maintenance_timeout_seconds):
                    async with transaction(session_factory) as session:
                        await execution_service_factory(session).delete_expired_run_metadata()
            except Exception:
                logger.warning("run_metadata_cleanup_failed")
            await asyncio.sleep(settings.worker_poll_interval_seconds)
    finally:
        await engine.dispose()


def build_execution_runtime(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[InvocationRunner, ExecutionServiceFactory]:
    """Build one worker-scoped invocation runtime from secret-backed keyrings."""

    fixture_values: dict[tuple[str, str], bytes] | None = None
    if settings.environment in {"local", "test"} and settings.secret_provider == "fixture":
        fixture_values = {
            (
                _CONFIRMATION_KEY_REFERENCE,
                version,
            ): f"local-confirmation-{version}-key-material".encode()
            for version in settings.confirmation_hmac_key_versions
        }
        fixture_values.update(
            {
                (
                    _IDEMPOTENCY_KEY_REFERENCE,
                    version,
                ): f"local-idempotency-{version}-key-material".encode()
                for version in settings.idempotency_hmac_key_versions
            }
        )
    secret_provider = build_secret_provider(settings, fixture_values=fixture_values)
    confirmation_keys = _load_keyring(
        secret_provider,
        settings.secret_provider,
        _CONFIRMATION_KEY_REFERENCE,
        settings.confirmation_hmac_key_versions,
    )
    idempotency_keys = _load_keyring(
        secret_provider,
        settings.secret_provider,
        _IDEMPOTENCY_KEY_REFERENCE,
        settings.idempotency_hmac_key_versions,
    )
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
            limits=TransportLimits(
                response_bytes=limits.max_result_bytes + _INVOCATION_PROTOCOL_OVERHEAD_BYTES
            ),
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


def _load_keyring(
    provider: SecretProvider,
    provider_name: str,
    reference: str,
    versions: tuple[str, ...],
) -> tuple[HmacKeyVersion, ...]:
    keys: list[HmacKeyVersion] = []
    for version in versions:
        with provider.retrieve(
            SecretReference(
                provider=provider_name,
                external_reference=reference,
                version=version,
            )
        ) as secret:
            if len(secret) < 32:
                raise ValueError("HMAC key material is too short")
            keys.append(HmacKeyVersion(version=version, secret=bytes(secret)))
    return tuple(keys)


def run() -> None:
    """Run the worker shell until the process receives a termination signal."""

    settings = get_settings()
    configure_logging(settings)
    logger = logging.getLogger("modall.worker")
    logger.info("worker_started environment=%s", settings.environment)
    asyncio.run(run_worker(settings))


if __name__ == "__main__":
    run()

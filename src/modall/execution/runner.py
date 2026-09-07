"""One-shot MCP invocation orchestration across durable fence transactions."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modall.execution.service import ExecutionService
from modall.execution.types import (
    AcceptedToolResult,
    ExecutionError,
    ExecutionFailureCode,
    JobLease,
    RunFailureCode,
    RunStatus,
)
from modall.mcp_adapter.client import (
    InvocationError,
    InvocationFailureCode,
    InvocationFenceRejected,
    InvocationIndeterminate,
    McpClientAdapter,
)
from modall.ops.telemetry import MetricsRegistry, log_event
from modall.persistence.database import transaction
from modall.persistence.models import (
    CapabilityVersion,
    McpToolBinding,
    Run,
    SecretBinding,
    ServerConnectionVersion,
)
from modall.secrets.provider import SecretProvider, SecretProviderError, SecretReference

ExecutionServiceFactory = Callable[[AsyncSession], ExecutionService]
AdapterFactory = Callable[[str], McpClientAdapter]


@dataclass(frozen=True, slots=True)
class _InvocationTarget:
    endpoint: str
    policy_version: str
    tool_name: str
    arguments: dict[str, object]
    output_schema: dict[str, object] | None
    secret_reference: SecretReference | None


class InvocationRunner:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        execution_service_factory: ExecutionServiceFactory,
        secret_provider: SecretProvider,
        adapter_factory: AdapterFactory,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._execution_service_factory = execution_service_factory
        self._secret_provider = secret_provider
        self._adapter_factory = adapter_factory
        self._metrics = metrics
        self._logger = logging.getLogger("modall.worker.invocation")

    async def claim_and_run(self, *, worker_id: str, lease_duration: timedelta) -> bool:
        """Claim at most one durable job and execute it outside the claim transaction."""

        async with transaction(self._session_factory) as session:
            lease = await self._execution_service_factory(session).claim_job(
                worker_id=worker_id,
                lease_duration=lease_duration,
            )
        if lease is None:
            return False
        self._event("job_claimed", lease)
        await self.run(lease, lease_duration=lease_duration)
        return True

    async def run(
        self, lease: JobLease, *, lease_duration: timedelta | None = None
    ) -> RunStatus | None:
        self._event("invocation_started", lease)
        try:
            target = await self._load_target(lease)
            adapter = self._adapter_factory(target.policy_version)
            if target.secret_reference is None:
                result = await adapter.invoke(
                    target.endpoint,
                    tool_name=target.tool_name,
                    arguments=target.arguments,
                    output_schema=target.output_schema,
                    before_session=lambda: self._fence_session(lease, lease_duration),
                    before_dispatch=lambda: self._fence_dispatch(lease),
                )
            else:
                with self._secret_provider.retrieve(target.secret_reference) as credential:
                    result = await adapter.invoke(
                        target.endpoint,
                        tool_name=target.tool_name,
                        arguments=target.arguments,
                        output_schema=target.output_schema,
                        bearer_token=credential,
                        before_session=lambda: self._fence_session(lease, lease_duration),
                        before_dispatch=lambda: self._fence_dispatch(lease),
                    )
        except InvocationFenceRejected:
            self._event("invocation_fence_rejected", lease, outcome="fenced")
            return None
        except (KeyError, ValueError, SecretProviderError):
            return await self._complete_failure(
                lease,
                RunStatus.FAILED,
                RunFailureCode.PREPARATION_FAILED,
                lease_duration=lease_duration,
            )
        except InvocationIndeterminate:
            return await self._complete_failure(
                lease,
                RunStatus.INDETERMINATE,
                RunFailureCode.UPSTREAM_OUTCOME_UNKNOWN,
                lease_duration=lease_duration,
            )
        except InvocationError as error:
            if error.code == InvocationFailureCode.PREPARATION_FAILED:
                code = RunFailureCode.PREPARATION_FAILED
            elif (
                not error.dispatched
                or error.code == InvocationFailureCode.SESSION_INITIALIZATION_FAILED
            ):
                code = RunFailureCode.SESSION_INITIALIZATION_FAILED
            elif error.code == InvocationFailureCode.TOOL_CALL_FAILED:
                code = RunFailureCode.TOOL_CALL_FAILED
            elif error.code == InvocationFailureCode.UNSUPPORTED_RESULT_CONTENT:
                code = RunFailureCode.UNSUPPORTED_TOOL_RESULT
            elif error.code == InvocationFailureCode.SENSITIVE_RESULT:
                code = RunFailureCode.SENSITIVE_TOOL_RESULT
            else:
                code = RunFailureCode.INVALID_TOOL_RESULT
            return await self._complete_failure(
                lease, RunStatus.FAILED, code, lease_duration=lease_duration
            )
        try:
            lease = await self._renew_lease(lease, lease_duration)
            async with transaction(self._session_factory) as session:
                completed = await self._execution_service_factory(
                    session
                ).complete_invocation_success(
                    lease,
                    result=AcceptedToolResult(
                        payload=result.payload,
                        canonical_digest=result.canonical_digest,
                        byte_count=result.byte_count,
                    ),
                )
        except ExecutionError as error:
            if error.code == ExecutionFailureCode.INVALID_ARGUMENTS:
                return await self._complete_failure(
                    lease,
                    RunStatus.FAILED,
                    RunFailureCode.INVALID_TOOL_RESULT,
                    lease_duration=lease_duration,
                )
            return None
        status = RunStatus(completed.status)
        self._event("invocation_terminal", lease, outcome=status.value)
        return status

    def _event(self, event: str, lease: JobLease, *, outcome: str | None = None) -> None:
        fields: dict[str, object] = {
            "correlation_id": lease.correlation_id,
            "workspace_id": lease.workspace_id,
            "run_id": lease.run_id,
            "job_id": lease.job_id,
            "lease_epoch": lease.lease_epoch,
        }
        if outcome is not None:
            fields["outcome"] = outcome
        log_event(self._logger, logging.INFO, event, **fields)
        if self._metrics is not None:
            self._metrics.increment(
                "modall_worker_invocations_total", event=event, outcome=outcome or "none"
            )

    async def _fence_session(self, lease: JobLease, lease_duration: timedelta | None) -> None:
        try:
            async with transaction(self._session_factory) as session:
                execution = self._execution_service_factory(session)
                if lease_duration is not None:
                    await execution.heartbeat(lease, lease_duration=lease_duration)
                await execution.fence_session(lease)
            self._event("mcp_session_fenced", lease)
        except ExecutionError as exc:
            raise InvocationFenceRejected(
                InvocationFailureCode.SESSION_INITIALIZATION_FAILED
            ) from exc

    async def _fence_dispatch(self, lease: JobLease) -> None:
        try:
            async with transaction(self._session_factory) as session:
                await self._execution_service_factory(session).fence_dispatch(lease)
            self._event("mcp_dispatch_fenced", lease)
        except ExecutionError as exc:
            raise InvocationFenceRejected(InvocationFailureCode.TOOL_CALL_FAILED) from exc

    async def _complete_failure(
        self,
        lease: JobLease,
        status: RunStatus,
        code: RunFailureCode,
        *,
        lease_duration: timedelta | None = None,
    ) -> RunStatus | None:
        try:
            lease = await self._renew_lease(lease, lease_duration)
            async with transaction(self._session_factory) as session:
                run = await self._execution_service_factory(session).complete_lease(
                    lease, status=status, safe_error_code=code
                )
                result = RunStatus(run.status)
                self._event("invocation_terminal", lease, outcome=result.value)
                return result
        except ExecutionError:
            return None

    async def _renew_lease(self, lease: JobLease, lease_duration: timedelta | None) -> JobLease:
        """Renew immediately before persisting a definitive upstream outcome."""

        if lease_duration is None:
            return lease
        async with transaction(self._session_factory) as session:
            return await self._execution_service_factory(session).heartbeat(
                lease, lease_duration=lease_duration
            )

    async def _load_target(self, lease: JobLease) -> _InvocationTarget:
        async with self._session_factory() as session:
            run = await session.scalar(
                select(Run).where(
                    Run.id == lease.run_id,
                    Run.workspace_id == lease.workspace_id,
                    Run.arguments.is_not(None),
                )
            )
            if run is None or run.arguments is None:
                raise ValueError("invocation target is unavailable")
            version = await session.scalar(
                select(CapabilityVersion).where(
                    CapabilityVersion.id == run.capability_version_id,
                    CapabilityVersion.workspace_id == lease.workspace_id,
                )
            )
            binding = await session.scalar(
                select(McpToolBinding).where(
                    McpToolBinding.capability_version_id == run.capability_version_id,
                    McpToolBinding.connection_version_id == run.connection_version_id,
                    McpToolBinding.workspace_id == lease.workspace_id,
                )
            )
            connection = await session.scalar(
                select(ServerConnectionVersion).where(
                    ServerConnectionVersion.id == run.connection_version_id,
                    ServerConnectionVersion.workspace_id == lease.workspace_id,
                )
            )
            if version is None or binding is None or connection is None:
                raise ValueError("invocation target is unavailable")
            secret_reference = None
            if connection.secret_binding_id is not None:
                secret = await session.scalar(
                    select(SecretBinding).where(
                        SecretBinding.id == connection.secret_binding_id,
                        SecretBinding.workspace_id == lease.workspace_id,
                    )
                )
                if secret is None:
                    raise SecretProviderError("secret binding is unavailable")
                secret_reference = SecretReference(
                    provider=secret.provider,
                    external_reference=secret.external_reference,
                    version=secret.version,
                )
            return _InvocationTarget(
                endpoint=connection.endpoint_url,
                policy_version=connection.policy_version,
                tool_name=binding.tool_name,
                arguments=run.arguments,
                output_schema=version.output_schema,
                secret_reference=secret_reference,
            )

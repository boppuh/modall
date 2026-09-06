"""One-shot MCP invocation orchestration across durable fence transactions."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modall.execution.service import ExecutionService
from modall.execution.types import (
    AcceptedToolResult,
    ExecutionError,
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
    ) -> None:
        self._session_factory = session_factory
        self._execution_service_factory = execution_service_factory
        self._secret_provider = secret_provider
        self._adapter_factory = adapter_factory

    async def claim_and_run(self, *, worker_id: str, lease_duration: timedelta) -> bool:
        """Claim at most one durable job and execute it outside the claim transaction."""

        async with transaction(self._session_factory) as session:
            lease = await self._execution_service_factory(session).claim_job(
                worker_id=worker_id,
                lease_duration=lease_duration,
            )
        if lease is None:
            return False
        await self.run(lease)
        return True

    async def run(self, lease: JobLease) -> RunStatus | None:
        try:
            target = await self._load_target(lease)
            adapter = self._adapter_factory(target.policy_version)
            if target.secret_reference is None:
                result = await adapter.invoke(
                    target.endpoint,
                    tool_name=target.tool_name,
                    arguments=target.arguments,
                    output_schema=target.output_schema,
                    before_session=lambda: self._fence_session(lease),
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
                        before_session=lambda: self._fence_session(lease),
                        before_dispatch=lambda: self._fence_dispatch(lease),
                    )
        except InvocationFenceRejected:
            return None
        except (KeyError, ValueError, SecretProviderError):
            return await self._complete_failure(
                lease, RunStatus.FAILED, RunFailureCode.PREPARATION_FAILED
            )
        except InvocationIndeterminate:
            return await self._complete_failure(
                lease, RunStatus.INDETERMINATE, RunFailureCode.UPSTREAM_OUTCOME_UNKNOWN
            )
        except InvocationError as error:
            if error.code == InvocationFailureCode.SESSION_INITIALIZATION_FAILED:
                code = RunFailureCode.SESSION_INITIALIZATION_FAILED
            elif error.code == InvocationFailureCode.TOOL_CALL_FAILED:
                code = RunFailureCode.TOOL_CALL_FAILED
            else:
                code = RunFailureCode.INVALID_TOOL_RESULT
            return await self._complete_failure(lease, RunStatus.FAILED, code)
        try:
            async with transaction(self._session_factory) as session:
                await self._execution_service_factory(session).complete_invocation_success(
                    lease,
                    result=AcceptedToolResult(
                        payload=result.payload,
                        canonical_digest=result.canonical_digest,
                        byte_count=result.byte_count,
                    ),
                )
        except ExecutionError:
            return None
        return RunStatus.SUCCEEDED

    async def _fence_session(self, lease: JobLease) -> None:
        try:
            async with transaction(self._session_factory) as session:
                await self._execution_service_factory(session).fence_session(lease)
        except ExecutionError as exc:
            raise InvocationFenceRejected(
                InvocationFailureCode.SESSION_INITIALIZATION_FAILED
            ) from exc

    async def _fence_dispatch(self, lease: JobLease) -> None:
        try:
            async with transaction(self._session_factory) as session:
                await self._execution_service_factory(session).fence_dispatch(lease)
        except ExecutionError as exc:
            raise InvocationFenceRejected(InvocationFailureCode.TOOL_CALL_FAILED) from exc

    async def _complete_failure(
        self, lease: JobLease, status: RunStatus, code: RunFailureCode
    ) -> RunStatus | None:
        try:
            async with transaction(self._session_factory) as session:
                run = await self._execution_service_factory(session).complete_lease(
                    lease, status=status, safe_error_code=code
                )
                return RunStatus(run.status)
        except ExecutionError:
            return None

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

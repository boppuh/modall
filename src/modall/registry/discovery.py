"""Durable refresh leasing and atomic publication of bounded MCP discovery results."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modall.identity.repository import AuthorizationDenied, require_current_role
from modall.identity.types import Role, WorkspaceContext
from modall.mcp_adapter.client import DiscoveryResult
from modall.persistence.models import (
    Capability,
    CapabilityStatusEvent,
    DiscoveryPayload,
    DiscoveryRefreshJob,
    DiscoverySnapshot,
    DiscoverySnapshotCapability,
    ServerConnection,
)
from modall.registry.service import (
    CapabilityService,
    ConnectionService,
    InvalidConnectionTransition,
)
from modall.registry.types import (
    CapabilityStatus,
    ConnectionLifecycle,
    DiscoveryFailureCode,
    RefreshJobStatus,
)


@dataclass(frozen=True, slots=True)
class RefreshLease:
    job_id: UUID
    worker_id: str
    lease_epoch: int
    connection_id: UUID
    connection_version_id: UUID
    control_epoch: int
    generation: int


class RefreshJobService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self, *, context: WorkspaceContext, connection_id: UUID
    ) -> DiscoveryRefreshJob:
        generation, control_epoch, version_id = await ConnectionService(
            self._session
        ).allocate_refresh_generation(context=context, connection_id=connection_id)
        older_jobs = (
            await self._session.scalars(
                select(DiscoveryRefreshJob)
                .where(
                    DiscoveryRefreshJob.workspace_id == context.workspace_id,
                    DiscoveryRefreshJob.connection_id == connection_id,
                    DiscoveryRefreshJob.status.in_(
                        (RefreshJobStatus.QUEUED.value, RefreshJobStatus.LEASED.value)
                    ),
                )
                .with_for_update()
            )
        ).all()
        for older in older_jobs:
            older.status = RefreshJobStatus.OBSOLETE.value
            older.lease_owner = None
            older.lease_expires_at = None
        job = DiscoveryRefreshJob(
            workspace_id=context.workspace_id,
            connection_id=connection_id,
            connection_version_id=version_id,
            generation=generation,
            control_epoch=control_epoch,
            status=RefreshJobStatus.QUEUED.value,
            lease_owner=None,
            lease_epoch=0,
            lease_expires_at=None,
        )
        self._session.add(job)
        await self._session.flush()
        return job

    async def claim(
        self,
        *,
        context: WorkspaceContext,
        job_id: UUID,
        worker_id: str,
        lease_duration: timedelta,
    ) -> RefreshLease:
        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        if not worker_id or len(worker_id) > 128 or lease_duration <= timedelta(0):
            raise ValueError("invalid refresh lease")
        claimed_at = await _database_now(self._session)
        job = await self._session.scalar(
            select(DiscoveryRefreshJob)
            .where(
                DiscoveryRefreshJob.id == job_id,
                DiscoveryRefreshJob.workspace_id == context.workspace_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None:
            raise AuthorizationDenied("workspace access denied")
        expires_at = job.lease_expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        reclaimable = job.status == RefreshJobStatus.QUEUED.value or (
            job.status == RefreshJobStatus.LEASED.value
            and expires_at is not None
            and expires_at <= claimed_at
        )
        if not reclaimable:
            raise InvalidConnectionTransition("refresh job cannot be claimed")
        await _lock_eligible_connection(
            self._session,
            context=context,
            connection_id=job.connection_id,
            connection_version_id=job.connection_version_id,
            control_epoch=job.control_epoch,
            generation=job.generation,
        )
        job.status = RefreshJobStatus.LEASED.value
        job.lease_owner = worker_id
        job.lease_epoch += 1
        job.lease_expires_at = claimed_at + lease_duration
        await self._session.flush()
        return RefreshLease(
            job_id=job.id,
            worker_id=worker_id,
            lease_epoch=job.lease_epoch,
            connection_id=job.connection_id,
            connection_version_id=job.connection_version_id,
            control_epoch=job.control_epoch,
            generation=job.generation,
        )

    async def renew(
        self,
        *,
        context: WorkspaceContext,
        lease: RefreshLease,
        lease_duration: timedelta,
    ) -> RefreshLease:
        """Extend an unexpired lease without changing its fencing epoch."""

        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        if lease_duration <= timedelta(0):
            raise ValueError("invalid refresh lease")
        renewed_at = await _database_now(self._session)
        job = await self._locked_job_for_lease(context, lease)
        expires_at = job.lease_expires_at
        if expires_at is None:
            raise InvalidConnectionTransition("refresh lease rejected")
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= renewed_at:
            raise InvalidConnectionTransition("refresh lease rejected")
        await _lock_eligible_connection(
            self._session,
            context=context,
            connection_id=lease.connection_id,
            connection_version_id=lease.connection_version_id,
            control_epoch=lease.control_epoch,
            generation=lease.generation,
        )
        job.lease_expires_at = max(expires_at, renewed_at + lease_duration)
        await self._session.flush()
        return lease

    async def validate_for_contact(self, *, context: WorkspaceContext, lease: RefreshLease) -> None:
        """Reject stale work immediately before any MCP endpoint contact."""

        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        await self._locked_job_for_lease(context, lease, require_unexpired=True)
        await _lock_eligible_connection(
            self._session,
            context=context,
            connection_id=lease.connection_id,
            connection_version_id=lease.connection_version_id,
            control_epoch=lease.control_epoch,
            generation=lease.generation,
        )

    async def _locked_job_for_lease(
        self,
        context: WorkspaceContext,
        lease: RefreshLease,
        *,
        require_unexpired: bool = False,
    ) -> DiscoveryRefreshJob:
        conditions = [
            DiscoveryRefreshJob.id == lease.job_id,
            DiscoveryRefreshJob.workspace_id == context.workspace_id,
            DiscoveryRefreshJob.status == RefreshJobStatus.LEASED.value,
            DiscoveryRefreshJob.lease_owner == lease.worker_id,
            DiscoveryRefreshJob.lease_epoch == lease.lease_epoch,
            DiscoveryRefreshJob.connection_id == lease.connection_id,
            DiscoveryRefreshJob.connection_version_id == lease.connection_version_id,
            DiscoveryRefreshJob.control_epoch == lease.control_epoch,
            DiscoveryRefreshJob.generation == lease.generation,
        ]
        if require_unexpired:
            conditions.append(DiscoveryRefreshJob.lease_expires_at > func.current_timestamp())
        job = await self._session.scalar(
            select(DiscoveryRefreshJob)
            .where(*conditions)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None:
            raise InvalidConnectionTransition("refresh lease rejected")
        return job


class DiscoveryPublicationService:
    """Publish observations only while every allocation fence still matches."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def publish_success(
        self,
        *,
        context: WorkspaceContext,
        lease: RefreshLease,
        result: DiscoveryResult,
    ) -> DiscoverySnapshot:
        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        job = await self._locked_active_job(context, lease)
        connection = await _lock_eligible_connection(
            self._session,
            context=context,
            connection_id=lease.connection_id,
            connection_version_id=lease.connection_version_id,
            control_epoch=lease.control_epoch,
            generation=lease.generation,
        )
        payload = await self._session.scalar(
            select(DiscoveryPayload).where(
                DiscoveryPayload.workspace_id == context.workspace_id,
                DiscoveryPayload.canonical_digest == result.canonical_digest,
            )
        )
        if payload is None:
            payload = DiscoveryPayload(
                workspace_id=context.workspace_id,
                canonical_digest=result.canonical_digest,
                normalized_payload=result.normalized_payload,
                byte_count=len(result.canonical_bytes),
            )
            self._session.add(payload)
            await self._session.flush()
        elif (
            payload.byte_count != len(result.canonical_bytes)
            or payload.normalized_payload != result.normalized_payload
        ):
            raise InvalidConnectionTransition("discovery digest collision rejected")

        snapshot = DiscoverySnapshot(
            workspace_id=context.workspace_id,
            connection_id=lease.connection_id,
            connection_version_id=lease.connection_version_id,
            payload_id=payload.id,
            generation=lease.generation,
            control_epoch=lease.control_epoch,
            protocol_revision=result.protocol_revision,
        )
        self._session.add(snapshot)
        await self._session.flush()

        observed_identities: set[str] = set()
        capability_service = CapabilityService(self._session)
        for tool in result.tools:
            observed_identities.add(tool.identity)
            version = await capability_service.record_version(
                context=context,
                connection_id=lease.connection_id,
                connection_version_id=lease.connection_version_id,
                expected_control_epoch=lease.control_epoch,
                expected_refresh_generation=lease.generation,
                tool_identity=tool.identity,
                tool_name=tool.name,
                display_name=tool.display_name,
                description=tool.description,
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
                metadata_digest=tool.metadata_digest,
                protocol_revision=result.protocol_revision,
                schema_supported=tool.schema_supported,
            )
            self._session.add(
                DiscoverySnapshotCapability(
                    workspace_id=context.workspace_id,
                    connection_id=lease.connection_id,
                    connection_version_id=lease.connection_version_id,
                    snapshot_id=snapshot.id,
                    capability_version_id=version.id,
                )
            )
        await self._mark_missing_unavailable(context, lease.connection_id, observed_identities)

        if connection.pending_version_id is not None:
            connection = await ConnectionService(self._session).promote_pending(
                context=context,
                connection_id=lease.connection_id,
                expected_version_id=lease.connection_version_id,
                expected_control_epoch=lease.control_epoch,
                expected_refresh_generation=lease.generation,
            )
        else:
            connection = await ConnectionService(self._session).complete_refresh(
                context=context,
                connection_id=lease.connection_id,
                expected_version_id=lease.connection_version_id,
                expected_control_epoch=lease.control_epoch,
                expected_refresh_generation=lease.generation,
            )
        connection.current_snapshot_id = snapshot.id
        connection.last_refresh_error_code = None
        connection.last_refresh_at = await _database_now(self._session)
        job.status = RefreshJobStatus.SUCCEEDED.value
        job.lease_owner = None
        job.lease_expires_at = None
        await self._session.flush()
        return snapshot

    async def publish_failure(
        self,
        *,
        context: WorkspaceContext,
        lease: RefreshLease,
        error_code: DiscoveryFailureCode,
    ) -> ServerConnection:
        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        job = await self._locked_active_job(context, lease)
        connection = await _lock_eligible_connection(
            self._session,
            context=context,
            connection_id=lease.connection_id,
            connection_version_id=lease.connection_version_id,
            control_epoch=lease.control_epoch,
            generation=lease.generation,
        )
        connection.lifecycle = ConnectionLifecycle.DEGRADED.value
        connection.last_refresh_error_code = error_code.value
        connection.last_refresh_at = await _database_now(self._session)
        connection.allocated_control_epoch = None
        connection.allocated_target_version_id = None
        job.status = RefreshJobStatus.FAILED.value
        job.lease_owner = None
        job.lease_expires_at = None
        await self._session.flush()
        return connection

    async def _locked_active_job(
        self, context: WorkspaceContext, lease: RefreshLease
    ) -> DiscoveryRefreshJob:
        job = await self._session.scalar(
            select(DiscoveryRefreshJob)
            .where(
                DiscoveryRefreshJob.id == lease.job_id,
                DiscoveryRefreshJob.workspace_id == context.workspace_id,
                DiscoveryRefreshJob.status == RefreshJobStatus.LEASED.value,
                DiscoveryRefreshJob.lease_owner == lease.worker_id,
                DiscoveryRefreshJob.lease_epoch == lease.lease_epoch,
                DiscoveryRefreshJob.lease_expires_at > func.current_timestamp(),
                DiscoveryRefreshJob.connection_id == lease.connection_id,
                DiscoveryRefreshJob.connection_version_id == lease.connection_version_id,
                DiscoveryRefreshJob.control_epoch == lease.control_epoch,
                DiscoveryRefreshJob.generation == lease.generation,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None:
            raise InvalidConnectionTransition("refresh lease rejected")
        return job

    async def _mark_missing_unavailable(
        self,
        context: WorkspaceContext,
        connection_id: UUID,
        observed_identities: set[str],
    ) -> None:
        capabilities = (
            await self._session.scalars(
                select(Capability)
                .where(
                    Capability.workspace_id == context.workspace_id,
                    Capability.connection_id == connection_id,
                )
                .with_for_update()
            )
        ).all()
        for capability in capabilities:
            if capability.tool_identity in observed_identities or capability.typed_status in {
                CapabilityStatus.UNAVAILABLE,
                CapabilityStatus.DISABLED,
            }:
                continue
            capability.status = CapabilityStatus.UNAVAILABLE.value
            capability.status_epoch += 1
            self._session.add(
                CapabilityStatusEvent(
                    workspace_id=context.workspace_id,
                    capability_id=capability.id,
                    capability_version_id=(
                        capability.pending_version_id or capability.enabled_version_id
                    ),
                    status=capability.status,
                    status_epoch=capability.status_epoch,
                    actor_user_id=context.actor_user_id,
                )
            )
        await self._session.flush()


async def _lock_eligible_connection(
    session: AsyncSession,
    *,
    context: WorkspaceContext,
    connection_id: UUID,
    connection_version_id: UUID,
    control_epoch: int,
    generation: int,
) -> ServerConnection:
    connection = await session.scalar(
        select(ServerConnection)
        .where(
            ServerConnection.id == connection_id,
            ServerConnection.workspace_id == context.workspace_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if connection is None:
        raise AuthorizationDenied("workspace access denied")
    target = connection.pending_version_id or connection.verified_version_id
    valid_lifecycle = connection.typed_lifecycle in {
        ConnectionLifecycle.VERIFYING,
        ConnectionLifecycle.ACTIVE,
        ConnectionLifecycle.DEGRADED,
    }
    if (
        not valid_lifecycle
        or target != connection_version_id
        or connection.control_epoch != control_epoch
        or connection.refresh_generation != generation
        or connection.allocated_control_epoch != control_epoch
        or connection.allocated_target_version_id != connection_version_id
    ):
        raise InvalidConnectionTransition("connection transition rejected")
    return connection


async def _database_now(session: AsyncSession) -> datetime:
    current = await session.scalar(select(func.current_timestamp()))
    if not isinstance(current, datetime):
        raise RuntimeError("database clock is unavailable")
    if current.tzinfo is None:
        return current.replace(tzinfo=UTC)
    return current.astimezone(UTC)

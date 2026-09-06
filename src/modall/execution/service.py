"""Durable invocation admission, replay protection, and worker leasing."""

import hashlib
import hmac
import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast
from uuid import UUID, uuid4

import jwt
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modall.audit.types import AuditAction, ResourceType
from modall.execution.types import (
    ExecutionError,
    ExecutionFailureCode,
    ExecutionLimits,
    HmacKeyVersion,
    JobLease,
    JobStatus,
    RunEventType,
    RunFailureCode,
    RunPreflight,
    RunStatus,
    SystemExecutionAuthority,
)
from modall.execution.validation import SchemaValidationResult, validate_schema_arguments
from modall.identity.repository import require_current_role
from modall.identity.types import Role, WorkspaceContext
from modall.mcp_adapter.client import QUALIFIED_PROTOCOL_REVISION
from modall.persistence.models import (
    AuditEvent,
    Capability,
    CapabilityVersion,
    ConfirmationNonce,
    DiscoverySnapshotCapability,
    IdempotencyRecord,
    Job,
    McpToolBinding,
    Run,
    RunAttempt,
    RunEvent,
    ServerConnection,
    SystemExecutionState,
    Workspace,
    WorkspaceMembership,
)
from modall.registry.types import CapabilityStatus, ConnectionLifecycle
from modall.security.metadata import (
    MetadataValidationError,
    contains_sensitive_json,
    validate_bounded_json,
)

_TOKEN_ISSUER = "modall"
_TOKEN_AUDIENCE = "modall-run-confirmation"
_RUN_METHOD = "POST"
_RUN_ROUTE = "/v1/runs"
_KEY_VERSION = re.compile(r"[A-Za-z0-9._-]{1,32}\Z")
_TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
        RunStatus.INDETERMINATE,
    }
)
_ACTIVE_RUN_STATUS_VALUES = (
    RunStatus.QUEUED.value,
    RunStatus.PREPARING.value,
    RunStatus.SESSION_FENCED.value,
    RunStatus.DISPATCH_FENCED.value,
)


@dataclass(frozen=True, slots=True)
class _AdmissionTarget:
    capability: Capability
    version: CapabilityVersion
    binding: McpToolBinding
    connection: ServerConnection


@dataclass(frozen=True, slots=True)
class _ConfirmationClaims:
    workspace_id: UUID
    actor_user_id: UUID
    capability_version_id: UUID
    capability_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    argument_digest: str
    route: str
    nonce: str
    key_version: str
    issued_at: datetime
    expires_at: datetime


class ExecutionService:
    """Workspace-scoped execution admission and durable worker coordination."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        confirmation_keys: Sequence[HmacKeyVersion],
        idempotency_keys: Sequence[HmacKeyVersion],
        limits: ExecutionLimits | None = None,
        now: Callable[[], datetime] | None = None,
        system_authority: SystemExecutionAuthority | None = None,
    ) -> None:
        self._session = session
        self._limits = limits or ExecutionLimits()
        self._confirmation_keys = self._validate_keyring(confirmation_keys)
        self._idempotency_keys = self._validate_keyring(idempotency_keys)
        self._clock_override = now
        self._now = now or (lambda: datetime.now(UTC))
        self._system_authority = system_authority

    async def preflight(
        self,
        *,
        context: WorkspaceContext,
        capability_version_id: UUID,
        arguments: dict[str, object],
    ) -> RunPreflight:
        await require_current_role(self._session, context, Role.ADMIN, Role.OPERATOR)
        target = await self._load_admission_target(context, capability_version_id)
        normalized, argument_digest = await self._validate_arguments(
            arguments, target.version.input_schema
        )
        del normalized
        now = await self._durable_now()
        expires_at = datetime.fromtimestamp(
            math.ceil((now + timedelta(seconds=self._limits.confirmation_ttl_seconds)).timestamp()),
            UTC,
        )
        nonce = uuid4().hex
        active = self._confirmation_keys[0]
        claims = {
            "iss": _TOKEN_ISSUER,
            "aud": _TOKEN_AUDIENCE,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
            "jti": nonce,
            "w": str(context.workspace_id),
            "a": str(context.actor_user_id),
            "cv": str(capability_version_id),
            "c": str(target.capability.id),
            "cn": str(target.connection.id),
            "cnv": str(target.binding.connection_version_id),
            "ad": argument_digest,
            "r": _RUN_ROUTE,
        }
        token = jwt.encode(
            claims, active.secret, algorithm="HS256", headers={"kid": active.version}
        )
        return RunPreflight(
            confirmation_token=token,
            capability_version_id=capability_version_id,
            connection_version_id=target.binding.connection_version_id,
            argument_digest=argument_digest,
            expires_at=expires_at,
        )

    async def create_run(
        self,
        *,
        context: WorkspaceContext,
        capability_version_id: UUID,
        arguments: dict[str, object],
        confirmation_token: str,
        idempotency_key: str,
        deadline: datetime | None = None,
        correlation_id: UUID | None = None,
    ) -> Run:
        # Global execution state is always locked before workspace-scoped rows.
        # Restore reconciliation uses the same order with an exclusive state lock.
        state = await self._execution_state(lock="shared")
        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
        )
        normalized, argument_digest = self._normalize_arguments(arguments)
        await self._require_confirmation_key_history()
        await self._require_idempotency_key_history()
        claims = self._decode_confirmation(confirmation_token)
        now = await self._durable_now()
        self._require_confirmation_bindings(
            claims=claims,
            context=context,
            capability_version_id=capability_version_id,
            argument_digest=argument_digest,
            now=now,
        )
        requested_deadline = self._canonicalize_deadline(deadline)
        raw_key = self._validate_idempotency_key(idempotency_key)
        canonical_request = self._canonical_request(
            capability_id=claims.capability_id,
            capability_version_id=capability_version_id,
            connection_id=claims.connection_id,
            connection_version_id=claims.connection_version_id,
            argument_digest=argument_digest,
            deadline=requested_deadline,
        )
        existing_record = await self._find_idempotency_record(context, raw_key)
        nonce_digest = _sha256(claims.nonce.encode())
        if existing_record is not None:
            return await self._replay_existing_run(
                context=context,
                existing_record=existing_record,
                canonical_request=canonical_request,
                claims=claims,
                nonce_digest=nonce_digest,
            )

        # The savepoint makes the admission lock releasable when a concurrent
        # creator wins while this transaction waits. PostgreSQL releases locks
        # acquired after a savepoint when it is rolled back, letting replay
        # restart in the canonical run -> idempotency -> workspace order.
        admission_lock = await self._session.begin_nested()
        try:
            await require_current_role(
                self._session,
                context,
                Role.ADMIN,
                Role.OPERATOR,
                serialize_workspace=True,
            )
            now = await self._durable_now()
            await self._require_confirmation_key_history()
            await self._require_idempotency_key_history()
            existing_record = await self._find_idempotency_record(context, raw_key)
        except BaseException:
            await admission_lock.rollback()
            raise
        if existing_record is not None:
            await admission_lock.rollback()
            return await self._replay_existing_run(
                context=context,
                existing_record=existing_record,
                canonical_request=canonical_request,
                claims=claims,
                nonce_digest=nonce_digest,
            )
        await admission_lock.commit()
        used_nonce = await self._session.scalar(
            select(ConfirmationNonce)
            .where(ConfirmationNonce.nonce_digest == nonce_digest)
            .with_for_update()
        )
        if claims.expires_at <= now:
            raise ExecutionError(ExecutionFailureCode.CONFIRMATION_EXPIRED)
        if used_nonce is not None:
            raise ExecutionError(ExecutionFailureCode.CONFIRMATION_REPLAYED)

        effective_deadline = self._admission_deadline(requested_deadline, now)
        target = await self._load_admission_target(context, capability_version_id)
        if (
            claims.capability_id != target.capability.id
            or claims.connection_id != target.connection.id
            or claims.connection_version_id != target.binding.connection_version_id
        ):
            raise ExecutionError(ExecutionFailureCode.INVALID_CONFIRMATION)
        await self._validate_normalized_arguments(normalized, target.version.input_schema)
        now = await self._durable_now()
        if claims.expires_at <= now:
            raise ExecutionError(ExecutionFailureCode.CONFIRMATION_EXPIRED)
        effective_deadline = self._admission_deadline(requested_deadline, now)

        if state.dispatch_quarantined:
            raise ExecutionError(ExecutionFailureCode.DISPATCH_QUARANTINED)
        run_id = uuid4()
        run = Run(
            id=run_id,
            workspace_id=context.workspace_id,
            actor_user_id=context.actor_user_id,
            capability_id=target.capability.id,
            capability_version_id=target.version.id,
            connection_id=target.connection.id,
            connection_version_id=target.binding.connection_version_id,
            connection_control_epoch=target.connection.control_epoch,
            capability_status_epoch=target.capability.status_epoch,
            protocol_revision=target.binding.protocol_revision,
            status=RunStatus.QUEUED.value,
            arguments=normalized,
            argument_digest=argument_digest,
            arguments_expires_at=now + timedelta(days=self._limits.argument_retention_days),
            deadline=effective_deadline,
            cancellation_requested=False,
            safe_error_code=None,
            created_at=now,
            updated_at=now,
            terminal_at=None,
        )
        self._session.add(run)
        if not await _flush_execution_payload(self._session):
            raise ExecutionError(ExecutionFailureCode.PERSISTENCE_FAILURE)
        self._session.add(
            Job(
                workspace_id=context.workspace_id,
                run_id=run_id,
                actor_user_id=context.actor_user_id,
                execution_epoch=state.execution_epoch,
                status=JobStatus.QUEUED.value,
                lease_owner=None,
                lease_epoch=0,
                lease_expires_at=None,
                available_at=now,
                deadline=effective_deadline,
                completed_at=None,
                created_at=now,
            )
        )
        self._append_event(run, RunEventType.ADMITTED, RunStatus.QUEUED, now=now, sequence=1)
        self._session.add(
            ConfirmationNonce(
                workspace_id=context.workspace_id,
                actor_user_id=context.actor_user_id,
                nonce_digest=nonce_digest,
                run_id=run_id,
                key_version=claims.key_version,
                expires_at=claims.expires_at,
                consumed_at=now,
            )
        )
        active_key = self._idempotency_keys[0]
        self._session.add(
            IdempotencyRecord(
                workspace_id=context.workspace_id,
                actor_user_id=context.actor_user_id,
                method=_RUN_METHOD,
                route=_RUN_ROUTE,
                confirmation_key_version=claims.key_version,
                key_version=active_key.version,
                key_hmac=self._idempotency_hmac(active_key, context, raw_key),
                request_hmac=_hmac_hex(active_key.secret, b"request\0" + canonical_request),
                resource_type=ResourceType.RUN.value,
                resource_id=run_id,
                created_at=now,
                completed_at=now,
                expires_at=now + timedelta(days=self._limits.run_retention_days),
            )
        )
        self._session.add(
            AuditEvent.succeeded(
                workspace_id=context.workspace_id,
                actor_user_id=context.actor_user_id,
                action=AuditAction.RUN_CREATED,
                resource_type=ResourceType.RUN,
                resource_id=run_id,
                correlation_id=correlation_id or uuid4(),
            )
        )
        if not await _flush_execution_payload(self._session):
            raise ExecutionError(ExecutionFailureCode.PERSISTENCE_FAILURE)
        return run

    async def claim_job(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta,
    ) -> JobLease | None:
        if not worker_id or len(worker_id) > 128 or lease_duration <= timedelta(0):
            raise ValueError("invalid worker lease")
        now = await self._durable_now()
        state = await self._execution_state(lock="shared")
        if state.dispatch_quarantined:
            raise ExecutionError(ExecutionFailureCode.DISPATCH_QUARANTINED)
        await self._terminalize_expired_jobs(now)
        await self._terminalize_abandoned_dispatches(now)
        run = await self._session.scalar(
            select(Run)
            .join(
                Job,
                and_(Job.workspace_id == Run.workspace_id, Job.run_id == Run.id),
            )
            .where(
                Job.execution_epoch == state.execution_epoch,
                Job.available_at <= now,
                Job.deadline > now,
                Run.status.in_(
                    [
                        RunStatus.QUEUED.value,
                        RunStatus.PREPARING.value,
                        RunStatus.SESSION_FENCED.value,
                    ]
                ),
                or_(
                    Job.status == JobStatus.QUEUED.value,
                    and_(
                        Job.status == JobStatus.LEASED.value,
                        Job.lease_expires_at <= now,
                    ),
                ),
            )
            .order_by(Job.created_at, Job.id)
            .limit(1)
            .with_for_update(of=Run, skip_locked=True)
        )
        if run is None:
            return None
        job = await self._session.scalar(select(Job).where(Job.run_id == run.id).with_for_update())
        if job is None:
            return None
        now = await self._durable_now()
        if _utc(job.deadline) <= now:
            await self._terminalize_run(
                run,
                RunStatus.TIMED_OUT,
                now,
                RunFailureCode.DEADLINE_EXCEEDED,
            )
            await self._session.flush()
            return None
        target_is_current = await self._claim_target_is_current(run)
        now = await self._durable_now()
        if _utc(job.deadline) <= now:
            await self._terminalize_run(
                run,
                RunStatus.TIMED_OUT,
                now,
                RunFailureCode.DEADLINE_EXCEEDED,
            )
            await self._session.flush()
            return None
        if not target_is_current:
            await self._terminalize_run(
                run,
                RunStatus.FAILED,
                now,
                RunFailureCode.PREPARATION_FAILED,
            )
            await self._session.flush()
            return None
        if job.status == JobStatus.LEASED.value:
            previous = await self._active_attempt(run.id)
            if previous is not None:
                previous.status = RunStatus.FAILED.value
                previous.safe_error_code = RunFailureCode.WORKER_LOST_BEFORE_DISPATCH.value
                previous.terminal_at = now
                await self._append_next_event(
                    run,
                    RunEventType.LEASE_LOST,
                    RunStatus(run.status),
                    safe_error_code=RunFailureCode.WORKER_LOST_BEFORE_DISPATCH.value,
                    now=now,
                )
                await self._session.flush()
        job.status = JobStatus.LEASED.value
        job.lease_owner = worker_id
        job.lease_epoch += 1
        job.lease_expires_at = min(now + lease_duration, _utc(job.deadline))
        latest_attempt = await self._session.scalar(
            select(func.max(RunAttempt.sequence)).where(RunAttempt.run_id == run.id)
        )
        self._session.add(
            RunAttempt(
                workspace_id=run.workspace_id,
                run_id=run.id,
                job_id=job.id,
                sequence=(latest_attempt or 0) + 1,
                lease_epoch=job.lease_epoch,
                status=RunStatus.PREPARING.value,
                started_at=now,
                terminal_at=None,
                safe_error_code=None,
            )
        )
        run.status = RunStatus.PREPARING.value
        run.updated_at = now
        await self._append_next_event(
            run, RunEventType.ATTEMPT_STARTED, RunStatus.PREPARING, now=now
        )
        await self._session.flush()
        return JobLease(
            job_id=job.id,
            run_id=job.run_id,
            workspace_id=job.workspace_id,
            worker_id=worker_id,
            lease_epoch=job.lease_epoch,
            execution_epoch=job.execution_epoch,
            expires_at=job.lease_expires_at,
        )

    async def fence_session(self, lease: JobLease) -> Run:
        """Fence a valid preparing attempt immediately before endpoint contact."""

        return await self._fence_attempt(
            lease,
            expected=RunStatus.PREPARING,
            target=RunStatus.SESSION_FENCED,
            event_type=RunEventType.SESSION_FENCED,
        )

    async def fence_dispatch(self, lease: JobLease) -> Run:
        """Fence a valid session immediately before its one permitted tool call."""

        return await self._fence_attempt(
            lease,
            expected=RunStatus.SESSION_FENCED,
            target=RunStatus.DISPATCH_FENCED,
            event_type=RunEventType.DISPATCH_FENCED,
        )

    async def heartbeat(self, lease: JobLease, *, lease_duration: timedelta) -> JobLease:
        if lease_duration <= timedelta(0):
            raise ValueError("invalid worker lease")
        state = await self._execution_state(lock="shared")
        run, job = await self._locked_lease_lineage(lease)
        now = await self._durable_now()
        if (
            state.dispatch_quarantined
            or state.execution_epoch != lease.execution_epoch
            or job.status != JobStatus.LEASED.value
            or job.lease_owner != lease.worker_id
            or job.lease_epoch != lease.lease_epoch
            or job.lease_expires_at is None
            or _utc(job.lease_expires_at) <= now
            or _utc(job.deadline) <= now
        ):
            raise ExecutionError(ExecutionFailureCode.LEASE_LOST)
        if RunStatus(run.status) in _TERMINAL_RUN_STATUSES:
            raise ExecutionError(ExecutionFailureCode.LEASE_LOST)
        job.lease_expires_at = min(now + lease_duration, _utc(job.deadline))
        await self._session.flush()
        return JobLease(
            job_id=job.id,
            run_id=job.run_id,
            workspace_id=job.workspace_id,
            worker_id=lease.worker_id,
            lease_epoch=lease.lease_epoch,
            execution_epoch=lease.execution_epoch,
            expires_at=job.lease_expires_at,
        )

    async def complete_lease(
        self,
        lease: JobLease,
        *,
        status: RunStatus,
        safe_error_code: RunFailureCode | None = None,
    ) -> Run:
        if status not in _TERMINAL_RUN_STATUSES:
            raise ExecutionError(ExecutionFailureCode.INVALID_TRANSITION)
        if safe_error_code is not None and not isinstance(safe_error_code, RunFailureCode):
            raise ExecutionError(ExecutionFailureCode.INVALID_TRANSITION)
        if status == RunStatus.SUCCEEDED and safe_error_code is not None:
            raise ExecutionError(ExecutionFailureCode.INVALID_TRANSITION)
        state = await self._execution_state(lock="shared")
        run, job = await self._locked_lease_lineage(lease)
        now = await self._durable_now()
        if (
            state.execution_epoch != lease.execution_epoch
            or job.status != JobStatus.LEASED.value
            or job.lease_owner != lease.worker_id
            or job.lease_epoch != lease.lease_epoch
            or job.lease_expires_at is None
        ):
            raise ExecutionError(ExecutionFailureCode.LEASE_LOST)
        if RunStatus(run.status) in _TERMINAL_RUN_STATUSES:
            raise ExecutionError(ExecutionFailureCode.INVALID_TRANSITION)
        if _utc(job.deadline) <= now:
            deadline_status = (
                RunStatus.INDETERMINATE
                if run.status == RunStatus.DISPATCH_FENCED.value
                else RunStatus.TIMED_OUT
            )
            await self._terminalize_run(
                run,
                deadline_status,
                now,
                RunFailureCode.DEADLINE_EXCEEDED,
            )
            await self._session.flush()
            return run
        if _utc(job.lease_expires_at) <= now:
            raise ExecutionError(ExecutionFailureCode.LEASE_LOST)
        attempt = await self._active_attempt(run.id)
        if attempt is None or attempt.lease_epoch != lease.lease_epoch:
            raise ExecutionError(ExecutionFailureCode.LEASE_LOST)
        if not self._valid_worker_completion(run, attempt, status, safe_error_code):
            raise ExecutionError(ExecutionFailureCode.INVALID_TRANSITION)
        run.status = status.value
        persisted_error_code = safe_error_code.value if safe_error_code is not None else None
        run.safe_error_code = persisted_error_code
        run.updated_at = now
        run.terminal_at = now
        attempt.status = status.value
        attempt.safe_error_code = persisted_error_code
        attempt.terminal_at = now
        job.status = JobStatus(status.value).value
        job.lease_owner = None
        job.lease_expires_at = None
        job.completed_at = now
        await self._append_next_event(
            run,
            RunEventType.TERMINAL,
            status,
            safe_error_code=persisted_error_code,
            now=now,
        )
        await self._session.flush()
        return run

    async def cancel_run(
        self,
        *,
        context: WorkspaceContext,
        run_id: UUID,
        correlation_id: UUID | None = None,
    ) -> Run:
        # Fail closed before the resource lookup so a stale context cannot use
        # cancellation responses as a run-existence oracle. The serialized
        # check below still linearizes the mutation after the run lock.
        await require_current_role(self._session, context, Role.ADMIN, Role.OPERATOR)
        run = await self._locked_run(context.workspace_id, run_id)
        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        status = RunStatus(run.status)
        if status in _TERMINAL_RUN_STATUSES:
            return run
        now = await self._durable_now()
        run.cancellation_requested = True
        if status in {RunStatus.QUEUED, RunStatus.PREPARING, RunStatus.SESSION_FENCED}:
            await self._terminalize_run(
                run,
                RunStatus.CANCELLED,
                now,
                RunFailureCode.CANCELLED_BEFORE_DISPATCH,
                event_type=RunEventType.CANCEL_REQUESTED,
            )
        else:
            run.updated_at = now
            await self._append_next_event(run, RunEventType.CANCEL_REQUESTED, status, now=now)
        self._session.add(
            AuditEvent.succeeded(
                workspace_id=context.workspace_id,
                actor_user_id=context.actor_user_id,
                action=(
                    AuditAction.RUN_CANCELLED
                    if run.status == RunStatus.CANCELLED.value
                    else AuditAction.RUN_CANCELLATION_REQUESTED
                ),
                resource_type=ResourceType.RUN,
                resource_id=run.id,
                correlation_id=correlation_id or uuid4(),
            )
        )
        await self._session.flush()
        return run

    async def enter_restore_quarantine(self) -> int:
        """Enter quarantine and advance the epoch; commit before reconciliation."""

        self._require_system_authority()
        now = await self._durable_now()
        state = await self._execution_state(lock="exclusive")
        state.execution_epoch += 1
        state.dispatch_quarantined = True
        state.updated_at = now
        await self._session.flush()
        return state.execution_epoch

    async def reconcile_restore_quarantine(self, *, batch_size: int = 100) -> int:
        """Terminalize one bounded batch after quarantine entry has committed."""

        self._require_system_authority()
        if batch_size <= 0 or batch_size > 1000:
            raise ValueError("invalid reconciliation batch")
        state = await self._execution_state(lock="shared")
        if not state.dispatch_quarantined:
            raise ExecutionError(ExecutionFailureCode.INVALID_TRANSITION)
        now = await self._durable_now()
        runs = list(
            (
                await self._session.scalars(
                    select(Run)
                    .where(Run.status.in_(_ACTIVE_RUN_STATUS_VALUES))
                    .order_by(Run.created_at, Run.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for run in runs:
            terminal = (
                RunStatus.INDETERMINATE
                if run.status == RunStatus.DISPATCH_FENCED.value
                else RunStatus.CANCELLED
            )
            await self._terminalize_run(
                run,
                terminal,
                now,
                RunFailureCode.RESTORE_RECONCILIATION,
                event_type=RunEventType.RESTORE_RECONCILED,
            )
        await self._session.flush()
        return len(runs)

    async def clear_restore_quarantine(self) -> int:
        self._require_system_authority()
        state = await self._execution_state(lock="exclusive")
        unreconciled_run = await self._session.scalar(
            select(Run.id)
            .where(Run.status.not_in([status.value for status in _TERMINAL_RUN_STATUSES]))
            .limit(1)
        )
        if not state.dispatch_quarantined or unreconciled_run is not None:
            raise ExecutionError(ExecutionFailureCode.INVALID_TRANSITION)
        state.dispatch_quarantined = False
        state.updated_at = await self._durable_now()
        await self._session.flush()
        return state.execution_epoch

    async def expire_retained_content(self, *, batch_size: int = 500) -> int:
        if batch_size <= 0 or batch_size > 1000:
            raise ValueError("invalid cleanup batch")
        now = await self._durable_now()
        runs = list(
            (
                await self._session.scalars(
                    select(Run)
                    .where(Run.arguments.is_not(None), Run.arguments_expires_at <= now)
                    .order_by(Run.arguments_expires_at, Run.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for run in runs:
            status = RunStatus(run.status)
            if status not in _TERMINAL_RUN_STATUSES:
                terminal = (
                    RunStatus.INDETERMINATE
                    if status == RunStatus.DISPATCH_FENCED
                    else RunStatus.TIMED_OUT
                )
                await self._terminalize_run(
                    run,
                    terminal,
                    now,
                    RunFailureCode.CONTENT_RETENTION_DEADLINE,
                    event_type=RunEventType.CONTENT_EXPIRED,
                )
            else:
                await self._append_next_event(
                    run,
                    RunEventType.CONTENT_EXPIRED,
                    status,
                    safe_error_code=run.safe_error_code,
                    now=now,
                )
            run.arguments = None
            run.argument_digest = None
            run.updated_at = now
        await self._session.flush()
        return len(runs)

    async def delete_expired_run_metadata(self, *, batch_size: int = 500) -> int:
        if batch_size <= 0 or batch_size > 1000:
            raise ValueError("invalid cleanup batch")
        cutoff = await self._durable_now() - timedelta(days=self._limits.run_retention_days)
        run_ids = list(
            (
                await self._session.scalars(
                    select(Run.id)
                    .where(
                        Run.terminal_at.is_not(None),
                        Run.terminal_at <= cutoff,
                    )
                    .order_by(Run.terminal_at, Run.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        if run_ids:
            await self._session.execute(delete(Run).where(Run.id.in_(run_ids)))
        return len(run_ids)

    @staticmethod
    def replay_projection(events: Sequence[RunEvent]) -> RunStatus:
        if not events:
            raise ValueError("run event history is empty")
        expected_sequence = 1
        current = RunStatus.QUEUED
        for event in sorted(events, key=lambda candidate: candidate.sequence):
            if event.sequence != expected_sequence:
                raise ValueError("run event history is not contiguous")
            current = RunStatus(event.status)
            expected_sequence += 1
        return current

    async def _load_admission_target(
        self, context: WorkspaceContext, capability_version_id: UUID
    ) -> _AdmissionTarget:
        version = await self._session.scalar(
            select(CapabilityVersion).where(
                CapabilityVersion.id == capability_version_id,
                CapabilityVersion.workspace_id == context.workspace_id,
            )
        )
        if version is None:
            raise ExecutionError(ExecutionFailureCode.CAPABILITY_UNAVAILABLE)
        capability = await self._session.scalar(
            select(Capability).where(
                Capability.id == version.capability_id,
                Capability.workspace_id == context.workspace_id,
            )
        )
        binding = await self._session.scalar(
            select(McpToolBinding).where(
                McpToolBinding.capability_version_id == version.id,
                McpToolBinding.workspace_id == context.workspace_id,
            )
        )
        if capability is None or binding is None:
            raise ExecutionError(ExecutionFailureCode.CAPABILITY_UNAVAILABLE)
        connection = await self._session.scalar(
            select(ServerConnection).where(
                ServerConnection.id == binding.connection_id,
                ServerConnection.workspace_id == context.workspace_id,
            )
        )
        observed = None
        if connection is not None and connection.current_snapshot_id is not None:
            observed = await self._session.scalar(
                select(DiscoverySnapshotCapability.id).where(
                    DiscoverySnapshotCapability.workspace_id == context.workspace_id,
                    DiscoverySnapshotCapability.connection_id == connection.id,
                    DiscoverySnapshotCapability.connection_version_id
                    == binding.connection_version_id,
                    DiscoverySnapshotCapability.snapshot_id == connection.current_snapshot_id,
                    DiscoverySnapshotCapability.capability_version_id == version.id,
                )
            )
        if (
            connection is None
            or observed is None
            or not version.schema_supported
            or capability.status != CapabilityStatus.ENABLED.value
            or capability.enabled_version_id != version.id
            or capability.pending_version_id is not None
            or connection.lifecycle != ConnectionLifecycle.ACTIVE.value
            or connection.pending_version_id is not None
            or connection.verified_version_id != binding.connection_version_id
            or binding.protocol_revision != QUALIFIED_PROTOCOL_REVISION
        ):
            raise ExecutionError(ExecutionFailureCode.CAPABILITY_UNAVAILABLE)
        return _AdmissionTarget(capability, version, binding, connection)

    async def _validate_arguments(
        self, arguments: dict[str, object], schema: dict[str, object]
    ) -> tuple[dict[str, object], str]:
        normalized, digest = self._normalize_arguments(arguments)
        await self._validate_normalized_arguments(normalized, schema)
        return normalized, digest

    def _normalize_arguments(self, arguments: dict[str, object]) -> tuple[dict[str, object], str]:
        try:
            validate_bounded_json(arguments)
            encoded = json.dumps(
                arguments,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            if len(encoded) > self._limits.max_argument_bytes:
                raise ExecutionError(ExecutionFailureCode.ARGUMENT_LIMIT)
            normalized = json.loads(encoded)
        except ExecutionError:
            raise
        except (MetadataValidationError, TypeError, ValueError, UnicodeError, RecursionError):
            raise ExecutionError(ExecutionFailureCode.INVALID_ARGUMENTS) from None
        if not isinstance(normalized, dict):
            raise ExecutionError(ExecutionFailureCode.INVALID_ARGUMENTS)
        return normalized, _sha256(encoded)

    async def _validate_normalized_arguments(
        self, arguments: dict[str, object], schema: dict[str, object]
    ) -> None:
        try:
            contains_secret = contains_sensitive_json(arguments)
        except Exception:
            raise ExecutionError(ExecutionFailureCode.SCANNER_FAILED) from None
        if contains_secret:
            raise ExecutionError(ExecutionFailureCode.SENSITIVE_ARGUMENTS)
        result = await validate_schema_arguments(
            arguments,
            schema,
            timeout_seconds=self._limits.schema_validation_timeout_seconds,
            memory_limit_bytes=self._limits.schema_validation_memory_bytes,
        )
        if result == SchemaValidationResult.INVALID_ARGUMENTS:
            raise ExecutionError(ExecutionFailureCode.INVALID_ARGUMENTS) from None
        if result == SchemaValidationResult.INVALID_SCHEMA:
            raise ExecutionError(ExecutionFailureCode.CAPABILITY_UNAVAILABLE) from None
        if result != SchemaValidationResult.VALID:
            raise ExecutionError(ExecutionFailureCode.SCANNER_FAILED) from None

    def _decode_confirmation(self, token: str) -> _ConfirmationClaims:
        if not token or len(token) > 4096:
            raise ExecutionError(ExecutionFailureCode.INVALID_CONFIRMATION)
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            key = self._key_by_version(self._confirmation_keys, kid if isinstance(kid, str) else "")
            if key is None:
                raise ExecutionError(ExecutionFailureCode.INVALID_CONFIRMATION)
            payload = jwt.decode(
                token,
                key.secret,
                algorithms=["HS256"],
                audience=_TOKEN_AUDIENCE,
                issuer=_TOKEN_ISSUER,
                options={
                    "verify_exp": False,
                    "verify_iat": False,
                    "require": ["exp", "iat", "jti"],
                },
            )
            return _ConfirmationClaims(
                workspace_id=UUID(payload["w"]),
                actor_user_id=UUID(payload["a"]),
                capability_version_id=UUID(payload["cv"]),
                capability_id=UUID(payload["c"]),
                connection_id=UUID(payload["cn"]),
                connection_version_id=UUID(payload["cnv"]),
                argument_digest=str(payload["ad"]),
                route=str(payload["r"]),
                nonce=str(payload["jti"]),
                key_version=key.version,
                issued_at=datetime.fromtimestamp(int(payload["iat"]), UTC),
                expires_at=datetime.fromtimestamp(int(payload["exp"]), UTC),
            )
        except ExecutionError:
            raise
        except Exception:
            raise ExecutionError(ExecutionFailureCode.INVALID_CONFIRMATION) from None

    @staticmethod
    def _require_confirmation_bindings(
        *,
        claims: _ConfirmationClaims,
        context: WorkspaceContext,
        capability_version_id: UUID,
        argument_digest: str,
        now: datetime,
    ) -> None:
        if (
            claims.workspace_id != context.workspace_id
            or claims.actor_user_id != context.actor_user_id
            or claims.capability_version_id != capability_version_id
            or claims.argument_digest != argument_digest
            or claims.route != _RUN_ROUTE
            or not claims.nonce
            or claims.issued_at > now
            or claims.expires_at <= claims.issued_at
        ):
            raise ExecutionError(ExecutionFailureCode.INVALID_CONFIRMATION)

    async def _find_idempotency_record(
        self, context: WorkspaceContext, raw_key: bytes
    ) -> IdempotencyRecord | None:
        candidates = [
            and_(
                IdempotencyRecord.key_version == key.version,
                IdempotencyRecord.key_hmac == self._idempotency_hmac(key, context, raw_key),
            )
            for key in self._idempotency_keys
        ]
        return cast(
            IdempotencyRecord | None,
            await self._session.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.workspace_id == context.workspace_id,
                    IdempotencyRecord.actor_user_id == context.actor_user_id,
                    IdempotencyRecord.method == _RUN_METHOD,
                    IdempotencyRecord.route == _RUN_ROUTE,
                    or_(*candidates),
                )
            ),
        )

    async def _replay_existing_run(
        self,
        *,
        context: WorkspaceContext,
        existing_record: IdempotencyRecord,
        canonical_request: bytes,
        claims: _ConfirmationClaims,
        nonce_digest: str,
    ) -> Run:
        key = self._key_by_version(self._idempotency_keys, existing_record.key_version)
        if key is None or not hmac.compare_digest(
            existing_record.request_hmac,
            _hmac_hex(key.secret, b"request\0" + canonical_request),
        ):
            raise ExecutionError(ExecutionFailureCode.IDEMPOTENCY_CONFLICT)
        run_statement = select(Run).where(
            Run.id == existing_record.resource_id,
            Run.workspace_id == context.workspace_id,
        )
        run = await self._session.scalar(run_statement.with_for_update())
        if run is None:
            raise ExecutionError(ExecutionFailureCode.IDEMPOTENCY_CONFLICT)
        locked_record = await self._session.scalar(
            select(IdempotencyRecord)
            .where(IdempotencyRecord.id == existing_record.id)
            .with_for_update()
        )
        if locked_record is None:
            raise ExecutionError(ExecutionFailureCode.IDEMPOTENCY_CONFLICT)
        await require_current_role(
            self._session,
            context,
            Role.ADMIN,
            Role.OPERATOR,
            serialize_workspace=True,
        )
        if (
            run.capability_id != claims.capability_id
            or run.capability_version_id != claims.capability_version_id
            or run.connection_id != claims.connection_id
            or run.connection_version_id != claims.connection_version_id
        ):
            raise ExecutionError(ExecutionFailureCode.IDEMPOTENCY_CONFLICT)
        used_nonce = await self._session.scalar(
            select(ConfirmationNonce)
            .where(ConfirmationNonce.nonce_digest == nonce_digest)
            .with_for_update()
        )
        now = await self._durable_now()
        if used_nonce is not None and used_nonce.run_id != run.id:
            raise ExecutionError(ExecutionFailureCode.CONFIRMATION_REPLAYED)
        if used_nonce is None:
            self._session.add(
                ConfirmationNonce(
                    workspace_id=context.workspace_id,
                    actor_user_id=context.actor_user_id,
                    nonce_digest=nonce_digest,
                    run_id=run.id,
                    key_version=claims.key_version,
                    expires_at=claims.expires_at,
                    consumed_at=now,
                )
            )
            await self._session.flush()
        return run

    async def _require_idempotency_key_history(self) -> None:
        configured_versions = [key.version for key in self._idempotency_keys]
        missing_version = await self._session.scalar(
            select(IdempotencyRecord.key_version)
            .where(
                IdempotencyRecord.key_version.not_in(configured_versions),
            )
            .limit(1)
        )
        if missing_version is not None:
            raise ExecutionError(ExecutionFailureCode.IDEMPOTENCY_KEY_HISTORY_INCOMPLETE)

    async def _require_confirmation_key_history(self) -> None:
        configured_versions = [key.version for key in self._confirmation_keys]
        missing_version = await self._session.scalar(
            select(IdempotencyRecord.confirmation_key_version)
            .where(IdempotencyRecord.confirmation_key_version.not_in(configured_versions))
            .limit(1)
        )
        if missing_version is not None:
            raise ExecutionError(ExecutionFailureCode.CONFIRMATION_KEY_HISTORY_INCOMPLETE)
        missing_nonce_version = await self._session.scalar(
            select(ConfirmationNonce.key_version)
            .where(ConfirmationNonce.key_version.not_in(configured_versions))
            .limit(1)
        )
        if missing_nonce_version is not None:
            raise ExecutionError(ExecutionFailureCode.CONFIRMATION_KEY_HISTORY_INCOMPLETE)

    @staticmethod
    def _idempotency_hmac(key: HmacKeyVersion, context: WorkspaceContext, raw_key: bytes) -> str:
        scope = (
            b"key\0"
            + str(context.workspace_id).encode()
            + b"\0"
            + str(context.actor_user_id).encode()
            + b"\0"
            + _RUN_METHOD.encode()
            + b"\0"
            + _RUN_ROUTE.encode()
            + b"\0"
        )
        return _hmac_hex(key.secret, scope + raw_key)

    @staticmethod
    def _canonical_request(
        *,
        capability_id: UUID,
        capability_version_id: UUID,
        connection_id: UUID,
        connection_version_id: UUID,
        argument_digest: str,
        deadline: datetime | None,
    ) -> bytes:
        return json.dumps(
            {
                "argument_digest": argument_digest,
                "capability_id": str(capability_id),
                "capability_version_id": str(capability_version_id),
                "connection_id": str(connection_id),
                "connection_version_id": str(connection_version_id),
                "deadline": deadline.isoformat() if deadline is not None else None,
                "method": _RUN_METHOD,
                "route": _RUN_ROUTE,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    @staticmethod
    def _canonicalize_deadline(deadline: datetime | None) -> datetime | None:
        if deadline is None:
            return None
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ExecutionError(ExecutionFailureCode.INVALID_ARGUMENTS)
        return deadline.astimezone(UTC)

    def _admission_deadline(self, requested: datetime | None, now: datetime) -> datetime:
        if requested is None:
            return now + timedelta(seconds=self._limits.max_run_seconds)
        if requested <= now or requested > now + timedelta(seconds=self._limits.max_run_seconds):
            raise ExecutionError(ExecutionFailureCode.INVALID_ARGUMENTS)
        return requested

    def _validate_idempotency_key(self, value: str) -> bytes:
        if (
            not value
            or len(value) > self._limits.max_idempotency_key_characters
            or any(ord(character) < 33 or ord(character) > 126 for character in value)
        ):
            raise ExecutionError(ExecutionFailureCode.INVALID_IDEMPOTENCY_KEY)
        return value.encode("ascii")

    async def _execution_state(
        self, *, lock: Literal["none", "shared", "exclusive"]
    ) -> SystemExecutionState:
        statement = select(SystemExecutionState).where(SystemExecutionState.id == 1)
        if lock != "none":
            statement = statement.with_for_update(read=lock == "shared")
        state = await self._session.scalar(statement)
        if state is None:
            state = SystemExecutionState(
                id=1,
                execution_epoch=1,
                dispatch_quarantined=False,
                updated_at=await self._durable_now(),
            )
            self._session.add(state)
            await self._session.flush()
        return state

    async def _locked_job(self, lease: JobLease) -> Job:
        job = await self._session.scalar(
            select(Job)
            .where(Job.id == lease.job_id, Job.workspace_id == lease.workspace_id)
            .with_for_update()
        )
        if job is None or job.run_id != lease.run_id:
            raise ExecutionError(ExecutionFailureCode.LEASE_LOST)
        return job

    async def _fence_attempt(
        self,
        lease: JobLease,
        *,
        expected: RunStatus,
        target: RunStatus,
        event_type: RunEventType,
    ) -> Run:
        state = await self._execution_state(lock="shared")
        lineage = await self._session.execute(
            select(Job.workspace_id).where(
                Job.id == lease.job_id,
                Job.workspace_id == lease.workspace_id,
                Job.run_id == lease.run_id,
            )
        )
        if lineage.one_or_none() is None:
            raise ExecutionError(ExecutionFailureCode.LEASE_LOST)
        run, job = await self._locked_lease_lineage(lease)
        workspace_id = await self._session.scalar(
            select(Workspace.id).where(Workspace.id == lease.workspace_id).with_for_update()
        )
        if workspace_id is None:
            raise ExecutionError(ExecutionFailureCode.LEASE_LOST)
        membership = await self._session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == run.workspace_id,
                WorkspaceMembership.user_id == run.actor_user_id,
            )
        )
        capability = await self._session.scalar(
            select(Capability)
            .where(
                Capability.id == run.capability_id,
                Capability.workspace_id == run.workspace_id,
            )
            .with_for_update()
        )
        connection = await self._session.scalar(
            select(ServerConnection)
            .where(
                ServerConnection.id == run.connection_id,
                ServerConnection.workspace_id == run.workspace_id,
            )
            .with_for_update()
        )
        observed = None
        if connection is not None and connection.current_snapshot_id is not None:
            observed = await self._session.scalar(
                select(DiscoverySnapshotCapability.id).where(
                    DiscoverySnapshotCapability.workspace_id == run.workspace_id,
                    DiscoverySnapshotCapability.connection_id == run.connection_id,
                    DiscoverySnapshotCapability.connection_version_id == run.connection_version_id,
                    DiscoverySnapshotCapability.snapshot_id == connection.current_snapshot_id,
                    DiscoverySnapshotCapability.capability_version_id == run.capability_version_id,
                )
            )
        binding = await self._session.scalar(
            select(McpToolBinding).where(
                McpToolBinding.workspace_id == run.workspace_id,
                McpToolBinding.capability_id == run.capability_id,
                McpToolBinding.capability_version_id == run.capability_version_id,
                McpToolBinding.connection_id == run.connection_id,
                McpToolBinding.connection_version_id == run.connection_version_id,
                McpToolBinding.protocol_revision == run.protocol_revision,
            )
        )
        attempt = await self._active_attempt(run.id)
        now = await self._durable_now()
        if (
            state.dispatch_quarantined
            or state.execution_epoch != lease.execution_epoch
            or job.execution_epoch != lease.execution_epoch
            or job.status != JobStatus.LEASED.value
            or job.lease_owner != lease.worker_id
            or job.lease_epoch != lease.lease_epoch
            or job.lease_expires_at is None
            or _utc(job.lease_expires_at) <= now
            or _utc(job.deadline) <= now
            or run.cancellation_requested
            or run.status != expected.value
            or run.protocol_revision != QUALIFIED_PROTOCOL_REVISION
            or membership is None
            or membership.role not in {Role.ADMIN.value, Role.OPERATOR.value}
            or capability is None
            or capability.status != CapabilityStatus.ENABLED.value
            or capability.enabled_version_id != run.capability_version_id
            or capability.pending_version_id is not None
            or capability.status_epoch != run.capability_status_epoch
            or connection is None
            or connection.lifecycle != ConnectionLifecycle.ACTIVE.value
            or connection.pending_version_id is not None
            or connection.verified_version_id != run.connection_version_id
            or connection.control_epoch != run.connection_control_epoch
            or observed is None
            or binding is None
            or attempt is None
            or attempt.lease_epoch != lease.lease_epoch
            or attempt.status != expected.value
        ):
            raise ExecutionError(ExecutionFailureCode.LEASE_LOST)
        run.status = target.value
        run.updated_at = now
        attempt.status = target.value
        await self._append_next_event(run, event_type, target, now=now)
        await self._session.flush()
        return run

    async def _claim_target_is_current(self, run: Run) -> bool:
        """Lock and revalidate the authority/configuration pinned by an admitted run."""

        workspace_id = await self._session.scalar(
            select(Workspace.id).where(Workspace.id == run.workspace_id).with_for_update()
        )
        membership = await self._session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == run.workspace_id,
                WorkspaceMembership.user_id == run.actor_user_id,
            )
        )
        capability = await self._session.scalar(
            select(Capability)
            .where(Capability.id == run.capability_id, Capability.workspace_id == run.workspace_id)
            .with_for_update()
        )
        connection = await self._session.scalar(
            select(ServerConnection)
            .where(
                ServerConnection.id == run.connection_id,
                ServerConnection.workspace_id == run.workspace_id,
            )
            .with_for_update()
        )
        observed = None
        if connection is not None and connection.current_snapshot_id is not None:
            observed = await self._session.scalar(
                select(DiscoverySnapshotCapability.id).where(
                    DiscoverySnapshotCapability.workspace_id == run.workspace_id,
                    DiscoverySnapshotCapability.connection_id == run.connection_id,
                    DiscoverySnapshotCapability.connection_version_id == run.connection_version_id,
                    DiscoverySnapshotCapability.snapshot_id == connection.current_snapshot_id,
                    DiscoverySnapshotCapability.capability_version_id == run.capability_version_id,
                )
            )
        binding = await self._session.scalar(
            select(McpToolBinding).where(
                McpToolBinding.workspace_id == run.workspace_id,
                McpToolBinding.capability_id == run.capability_id,
                McpToolBinding.capability_version_id == run.capability_version_id,
                McpToolBinding.connection_id == run.connection_id,
                McpToolBinding.connection_version_id == run.connection_version_id,
                McpToolBinding.protocol_revision == run.protocol_revision,
            )
        )
        return bool(
            workspace_id is not None
            and run.protocol_revision == QUALIFIED_PROTOCOL_REVISION
            and membership is not None
            and membership.role in {Role.ADMIN.value, Role.OPERATOR.value}
            and capability is not None
            and capability.status == CapabilityStatus.ENABLED.value
            and capability.enabled_version_id == run.capability_version_id
            and capability.pending_version_id is None
            and capability.status_epoch == run.capability_status_epoch
            and connection is not None
            and connection.lifecycle == ConnectionLifecycle.ACTIVE.value
            and connection.pending_version_id is None
            and connection.verified_version_id == run.connection_version_id
            and connection.control_epoch == run.connection_control_epoch
            and observed is not None
            and binding is not None
        )

    async def _locked_lease_lineage(self, lease: JobLease) -> tuple[Run, Job]:
        lineage = await self._session.execute(
            select(Job.workspace_id, Job.run_id).where(
                Job.id == lease.job_id,
                Job.workspace_id == lease.workspace_id,
            )
        )
        row = lineage.one_or_none()
        if row is None or row.run_id != lease.run_id:
            raise ExecutionError(ExecutionFailureCode.LEASE_LOST)
        run = await self._session.scalar(
            select(Run)
            .where(Run.id == row.run_id, Run.workspace_id == row.workspace_id)
            .with_for_update()
        )
        if run is None:
            raise ExecutionError(ExecutionFailureCode.LEASE_LOST)
        return run, await self._locked_job(lease)

    async def _locked_run(self, workspace_id: UUID, run_id: UUID) -> Run:
        run = await self._session.scalar(
            select(Run).where(Run.id == run_id, Run.workspace_id == workspace_id).with_for_update()
        )
        if run is None:
            raise ExecutionError(ExecutionFailureCode.INVALID_TRANSITION)
        return run

    async def _active_attempt(self, run_id: UUID) -> RunAttempt | None:
        return cast(
            RunAttempt | None,
            await self._session.scalar(
                select(RunAttempt)
                .where(
                    RunAttempt.run_id == run_id,
                    RunAttempt.status.in_(
                        [
                            RunStatus.PREPARING.value,
                            RunStatus.SESSION_FENCED.value,
                            RunStatus.DISPATCH_FENCED.value,
                        ]
                    ),
                )
                .order_by(RunAttempt.sequence.desc())
                .limit(1)
                .with_for_update()
            ),
        )

    async def _terminalize_expired_jobs(self, now: datetime) -> None:
        runs = list(
            (
                await self._session.scalars(
                    select(Run)
                    .join(
                        Job,
                        and_(Job.workspace_id == Run.workspace_id, Job.run_id == Run.id),
                    )
                    .where(
                        Job.status.in_([JobStatus.QUEUED.value, JobStatus.LEASED.value]),
                        Job.deadline <= now,
                    )
                    .order_by(Job.deadline, Job.id)
                    .limit(self._limits.reconciliation_batch_size)
                    .with_for_update(of=Run, skip_locked=True)
                )
            ).all()
        )
        for run in runs:
            status = (
                RunStatus.INDETERMINATE
                if run.status == RunStatus.DISPATCH_FENCED.value
                else RunStatus.TIMED_OUT
            )
            await self._terminalize_run(run, status, now, RunFailureCode.DEADLINE_EXCEEDED)

    async def _terminalize_abandoned_dispatches(self, now: datetime) -> None:
        runs = list(
            (
                await self._session.scalars(
                    select(Run)
                    .join(
                        Job,
                        and_(Job.workspace_id == Run.workspace_id, Job.run_id == Run.id),
                    )
                    .where(
                        Job.status == JobStatus.LEASED.value,
                        Job.lease_expires_at <= now,
                    )
                    .order_by(Job.lease_expires_at, Job.id)
                    .limit(self._limits.reconciliation_batch_size)
                    .with_for_update(of=Run, skip_locked=True)
                )
            ).all()
        )
        for run in runs:
            attempt = await self._active_attempt(run.id)
            if attempt is not None and attempt.status == RunStatus.DISPATCH_FENCED.value:
                await self._terminalize_run(
                    run,
                    RunStatus.INDETERMINATE,
                    now,
                    RunFailureCode.WORKER_LOST_AFTER_DISPATCH,
                )
        await self._session.flush()

    async def _terminalize_run(
        self,
        run: Run,
        status: RunStatus,
        now: datetime,
        safe_error_code: RunFailureCode,
        *,
        event_type: RunEventType = RunEventType.TERMINAL,
    ) -> None:
        run.status = status.value
        run.safe_error_code = safe_error_code.value
        run.updated_at = now
        run.terminal_at = now
        job = await self._session.scalar(select(Job).where(Job.run_id == run.id).with_for_update())
        if job is not None:
            job.status = JobStatus(status.value).value
            job.lease_owner = None
            job.lease_expires_at = None
            job.completed_at = now
        attempt = await self._active_attempt(run.id)
        if attempt is not None:
            attempt.status = status.value
            attempt.safe_error_code = safe_error_code.value
            attempt.terminal_at = now
        await self._append_next_event(
            run,
            event_type,
            status,
            safe_error_code=safe_error_code.value,
            now=now,
        )

    async def _append_next_event(
        self,
        run: Run,
        event_type: RunEventType,
        status: RunStatus,
        *,
        safe_error_code: str | None = None,
        now: datetime,
    ) -> None:
        sequence = await self._session.scalar(
            select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run.id)
        )
        self._append_event(
            run,
            event_type,
            status,
            safe_error_code=safe_error_code,
            now=now,
            sequence=(sequence or 0) + 1,
        )

    def _append_event(
        self,
        run: Run,
        event_type: RunEventType,
        status: RunStatus,
        *,
        now: datetime,
        sequence: int,
        safe_error_code: str | None = None,
    ) -> None:
        self._session.add(
            RunEvent(
                workspace_id=run.workspace_id,
                run_id=run.id,
                sequence=sequence,
                event_type=event_type.value,
                status=status.value,
                safe_error_code=safe_error_code,
                occurred_at=now,
            )
        )

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution clock must be timezone-aware")
        return value.astimezone(UTC)

    async def _durable_now(self) -> datetime:
        """Use the database clock in production; injected clocks are a test seam only."""

        if self._clock_override is not None:
            return self._utc_now()
        clock = (
            func.clock_timestamp()
            if self._session.get_bind().dialect.name == "postgresql"
            else func.current_timestamp()
        )
        value = await self._session.scalar(select(clock))
        if not isinstance(value, datetime):
            raise RuntimeError("database clock is unavailable")
        return _utc(value)

    @staticmethod
    def _valid_worker_completion(
        run: Run,
        attempt: RunAttempt,
        status: RunStatus,
        safe_error_code: RunFailureCode | None,
    ) -> bool:
        source = RunStatus(run.status)
        if attempt.status != source.value:
            return False
        if status == RunStatus.SUCCEEDED:
            return source == RunStatus.DISPATCH_FENCED and safe_error_code is None
        if status == RunStatus.FAILED:
            allowed_codes = {
                RunStatus.PREPARING: {RunFailureCode.PREPARATION_FAILED},
                RunStatus.SESSION_FENCED: {RunFailureCode.SESSION_INITIALIZATION_FAILED},
                RunStatus.DISPATCH_FENCED: {
                    RunFailureCode.TOOL_CALL_FAILED,
                    RunFailureCode.INVALID_TOOL_RESULT,
                },
            }
            return safe_error_code in allowed_codes.get(source, set())
        if status == RunStatus.CANCELLED:
            return (
                run.cancellation_requested
                and source in {RunStatus.PREPARING, RunStatus.SESSION_FENCED}
                and safe_error_code == RunFailureCode.CANCELLED_BEFORE_DISPATCH
            )
        if status == RunStatus.INDETERMINATE:
            return (
                source == RunStatus.DISPATCH_FENCED
                and safe_error_code == RunFailureCode.UPSTREAM_OUTCOME_UNKNOWN
            )
        return False

    def _require_system_authority(self) -> None:
        if self._system_authority is None:
            raise PermissionError("system execution authority required")

    def _validate_keyring(self, values: Sequence[HmacKeyVersion]) -> tuple[HmacKeyVersion, ...]:
        keys = tuple(values)
        if (
            not keys
            or len(keys) > self._limits.max_historical_hmac_keys
            or len({key.version for key in keys}) != len(keys)
            or any(_KEY_VERSION.fullmatch(key.version) is None for key in keys)
            or any(len(key.secret) < 32 for key in keys)
        ):
            raise ValueError("invalid HMAC keyring")
        return keys

    @staticmethod
    def _key_by_version(keys: Sequence[HmacKeyVersion], version: str) -> HmacKeyVersion | None:
        return next((key for key in keys if key.version == version), None)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac_hex(secret: bytes, value: bytes) -> str:
    return hmac.new(secret, value, hashlib.sha256).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _flush_execution_payload(session: AsyncSession) -> bool:
    try:
        await session.flush()
    except SQLAlchemyError:
        return False
    return True

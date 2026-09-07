import asyncio
import hashlib
import json
import resource
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import jwt
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modall.audit.types import AuditAction
from modall.execution import validation as schema_validation
from modall.execution.runner import InvocationRunner
from modall.execution.service import ExecutionService
from modall.execution.types import (
    AcceptedToolResult,
    ExecutionError,
    ExecutionFailureCode,
    ExecutionLimits,
    HmacKeyVersion,
    JobLease,
    RunFailureCode,
    RunStatus,
    SystemExecutionAuthority,
)
from modall.identity.repository import AuthorizationDenied, AuthorizationService
from modall.identity.service import IdentityService
from modall.identity.types import Permission, Principal, Role, WorkspaceContext
from modall.mcp_adapter.client import (
    InvocationError,
    InvocationFailureCode,
    InvocationIndeterminate,
    InvocationResult,
    McpClientAdapter,
)
from modall.ops.telemetry import MetricsRegistry
from modall.persistence.database import create_engine, create_session_factory, transaction
from modall.persistence.models import (
    AuditEvent,
    Base,
    CapabilityVersion,
    ConfirmationNonce,
    DiscoveryPayload,
    DiscoverySnapshot,
    DiscoverySnapshotCapability,
    IdempotencyRecord,
    Job,
    Run,
    RunAttempt,
    RunEvent,
    RunResult,
    SecretBinding,
    SystemExecutionState,
    WorkspaceMembership,
)
from modall.registry.service import CapabilityService, ConnectionService
from modall.secrets.provider import FixtureSecretProvider

CONFIRMATION_KEYS = (HmacKeyVersion("confirm-v1", b"c" * 32),)
IDEMPOTENCY_KEYS = (HmacKeyVersion("idem-v1", b"i" * 32),)
SYSTEM_AUTHORITY = SystemExecutionAuthority()


@asynccontextmanager
async def database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection: object, connection_record: object) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()


async def bootstrap(
    factory: async_sessionmaker[AsyncSession], *, subject: str
) -> tuple[UUID, UUID]:
    async with transaction(factory) as session:
        identity = IdentityService(session)
        user = await identity.resolve_user(Principal("issuer", subject, subject))
        workspace = await identity.create_workspace(owner=user, name=f"Workspace {subject}")
        return user.id, workspace.id


async def context_for(
    session: AsyncSession, *, user_id: UUID, workspace_id: UUID
) -> WorkspaceContext:
    return await AuthorizationService(session).authorize(
        user_id=user_id,
        workspace_id=workspace_id,
        permission=Permission.INVOKE,
    )


async def create_executable_target(
    session: AsyncSession,
    context: WorkspaceContext,
    *,
    input_schema: dict[str, object] | None = None,
    secret_binding_id: UUID | None = None,
) -> CapabilityVersion:
    connection = await ConnectionService(session).create(
        context=context,
        name="Execution fixture",
        endpoint_url="https://mcp.example/tools",
        secret_binding_id=secret_binding_id,
        policy_version="v1",
    )
    connection_version_id = connection.pending_version_id
    assert connection_version_id is not None
    generation, control_epoch, _ = await ConnectionService(session).allocate_refresh_generation(
        context=context, connection_id=connection.id
    )
    capability_service = CapabilityService(session)
    version = await capability_service.record_version(
        context=context,
        connection_id=connection.id,
        connection_version_id=connection_version_id,
        expected_control_epoch=control_epoch,
        expected_refresh_generation=generation,
        tool_identity="tools/search",
        tool_name="search",
        display_name="Search",
        description="Search public records",
        input_schema=input_schema
        or {
            "type": "object",
            "properties": {"query": {"type": "string", "maxLength": 100}},
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema=None,
        metadata_digest="a" * 64,
        protocol_revision="2025-06-18",
    )
    payload = DiscoveryPayload(
        workspace_id=context.workspace_id,
        canonical_digest=connection.id.hex * 2,
        normalized_payload={"tools": []},
        byte_count=2,
    )
    session.add(payload)
    await session.flush()
    snapshot = DiscoverySnapshot(
        workspace_id=context.workspace_id,
        connection_id=connection.id,
        connection_version_id=connection_version_id,
        payload_id=payload.id,
        generation=generation,
        control_epoch=control_epoch,
        protocol_revision="2025-06-18",
    )
    session.add(snapshot)
    await session.flush()
    session.add(
        DiscoverySnapshotCapability(
            workspace_id=context.workspace_id,
            connection_id=connection.id,
            connection_version_id=connection_version_id,
            snapshot_id=snapshot.id,
            capability_version_id=version.id,
        )
    )
    await ConnectionService(session).promote_pending(
        context=context,
        connection_id=connection.id,
        expected_version_id=connection_version_id,
        expected_control_epoch=control_epoch,
        expected_refresh_generation=generation,
    )
    connection.current_snapshot_id = snapshot.id
    await session.flush()
    await capability_service.enable(
        context=context,
        capability_id=version.capability_id,
        expected_version_id=version.id,
    )
    return version


def service(
    session: AsyncSession,
    *,
    now: datetime,
    confirmation_keys: tuple[HmacKeyVersion, ...] = CONFIRMATION_KEYS,
    idempotency_keys: tuple[HmacKeyVersion, ...] = IDEMPOTENCY_KEYS,
    limits: ExecutionLimits | None = None,
    system_authority: SystemExecutionAuthority | None = None,
) -> ExecutionService:
    return ExecutionService(
        session,
        confirmation_keys=confirmation_keys,
        idempotency_keys=idempotency_keys,
        limits=limits,
        now=lambda: now,
        system_authority=system_authority,
    )


def test_preflight_creates_exact_lineage_and_idempotent_replays() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="admit")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                first_preflight = await service(session, now=now).preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "weather"},
                )
                first = await service(session, now=now).create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "weather"},
                    confirmation_token=first_preflight.confirmation_token,
                    idempotency_key="request-one",
                )
                second_preflight = await service(session, now=now).preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "weather"},
                )
                replay = await service(session, now=now + timedelta(seconds=30)).create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "weather"},
                    confirmation_token=second_preflight.confirmation_token,
                    idempotency_key="request-one",
                )
                assert replay.id == first.id
                assert first.capability_version_id == version.id
                assert first.connection_version_id == first_preflight.connection_version_id
                assert first.arguments == {"query": "weather"}
                expired_replay_token = await service(session, now=now).preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "weather"},
                )
                expired_replay = await service(
                    session, now=now + timedelta(seconds=121)
                ).create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "weather"},
                    confirmation_token=expired_replay_token.confirmation_token,
                    idempotency_key="request-one",
                )
                assert expired_replay.id == first.id
                assert first.argument_digest == first_preflight.argument_digest
                assert first.status == RunStatus.QUEUED.value
                assert await session.scalar(select(func.count()).select_from(Job)) == 1
                job = await session.scalar(select(Job).where(Job.run_id == first.id))
                assert job is not None
                assert job.created_at.replace(tzinfo=UTC) == now
                assert (
                    await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
                )
                assert (
                    await session.scalar(select(func.count()).select_from(ConfirmationNonce)) == 3
                )
                events = list(
                    (
                        await session.scalars(select(RunEvent).where(RunEvent.run_id == first.id))
                    ).all()
                )
                assert ExecutionService.replay_projection(events) == RunStatus.QUEUED

    asyncio.run(scenario())


def test_preflight_returns_the_exact_expiry_encoded_in_confirmation() -> None:
    async def scenario() -> None:
        # Deliberately differs from the process wall clock: JWT temporal claims
        # must be evaluated against the same durable clock used to issue them.
        now = datetime(2035, 9, 6, 12, 0, 0, 987654, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="token-expiry")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                preflight = await service(session, now=now).preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "expiry"},
                )
                claims = jwt.decode(
                    preflight.confirmation_token,
                    options={"verify_signature": False},
                    algorithms=["HS256"],
                )
                assert preflight.expires_at == datetime.fromtimestamp(claims["exp"], UTC)
                assert preflight.expires_at.microsecond == 0
                minimum_ttl = ExecutionLimits(confirmation_ttl_seconds=1)
                short_preflight = await service(session, now=now, limits=minimum_ttl).preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "expiry"},
                )
                assert short_preflight.expires_at >= now + timedelta(seconds=1)
                short_run = await service(
                    session,
                    now=now + timedelta(seconds=1),
                    limits=minimum_ttl,
                ).create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "expiry"},
                    confirmation_token=short_preflight.confirmation_token,
                    idempotency_key="minimum-durable-clock",
                )
                assert short_run.status == RunStatus.QUEUED.value
                run = await service(session, now=now).create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "expiry"},
                    confirmation_token=preflight.confirmation_token,
                    idempotency_key="durable-clock",
                )
                assert run.status == RunStatus.QUEUED.value

    asyncio.run(scenario())


def test_admission_enforces_the_bounded_active_run_snapshot() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        limits = ExecutionLimits(max_active_runs_per_workspace=1)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="active-limit")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(session, now=now, limits=limits)
                first = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "first"},
                )
                await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "first"},
                    confirmation_token=first.confirmation_token,
                    idempotency_key="active-first",
                )
                second = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "second"},
                )
                with pytest.raises(ExecutionError) as raised:
                    await execution.create_run(
                        context=context,
                        capability_version_id=version.id,
                        arguments={"query": "second"},
                        confirmation_token=second.confirmation_token,
                        idempotency_key="active-second",
                    )
                assert raised.value.code is ExecutionFailureCode.ACTIVE_RUN_LIMIT

    asyncio.run(scenario())


def test_preflight_and_confirmation_fail_closed_without_argument_persistence() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="guardrail")
            other_id, other_workspace = await bootstrap(factory, subject="other")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(session, now=now)
                rejected_cases: tuple[tuple[dict[str, object], ExecutionFailureCode], ...] = (
                    ({"wrong": "shape"}, ExecutionFailureCode.INVALID_ARGUMENTS),
                    (
                        {"query": "authorization: bearer AbCdEfGhIjKlMnOpQrStUvWx"},
                        ExecutionFailureCode.SENSITIVE_ARGUMENTS,
                    ),
                )
                for arguments, expected in rejected_cases:
                    with pytest.raises(ExecutionError) as raised:
                        await execution.preflight(
                            context=context,
                            capability_version_id=version.id,
                            arguments=arguments,
                        )
                    assert raised.value.code == expected
                preflight = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "safe"},
                )

            async with transaction(factory) as session:
                other_context = await context_for(
                    session, user_id=other_id, workspace_id=other_workspace
                )
                with pytest.raises(ExecutionError) as transferred:
                    await service(session, now=now).create_run(
                        context=other_context,
                        capability_version_id=version.id,
                        arguments={"query": "safe"},
                        confirmation_token=preflight.confirmation_token,
                        idempotency_key="transfer",
                    )
                assert transferred.value.code == ExecutionFailureCode.INVALID_CONFIRMATION

            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(Run)) == 0

    asyncio.run(scenario())


def test_idempotency_survives_key_rotation_and_detects_conflict() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        rotated = HmacKeyVersion("idem-v2", b"n" * 32)
        rotated_confirmation = HmacKeyVersion("confirm-v2", b"r" * 32)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="rotation")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                initial = service(session, now=now)
                token = await initial.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "same"},
                )
                run = await initial.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "same"},
                    confirmation_token=token.confirmation_token,
                    idempotency_key="rotating-key",
                )
                confirmation_replay_service = service(
                    session,
                    now=now,
                    confirmation_keys=(rotated_confirmation, *CONFIRMATION_KEYS),
                )
                confirmation_replay_token = await confirmation_replay_service.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "same"},
                )
                assert (
                    await confirmation_replay_service.create_run(
                        context=context,
                        capability_version_id=version.id,
                        arguments={"query": "same"},
                        confirmation_token=confirmation_replay_token.confirmation_token,
                        idempotency_key="rotating-key",
                    )
                ).id == run.id
                replay_nonce = await session.scalar(
                    select(ConfirmationNonce).where(
                        ConfirmationNonce.key_version == rotated_confirmation.version
                    )
                )
                assert replay_nonce is not None
                retained_key_token = await initial.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "retained confirmation key"},
                )
                with pytest.raises(ExecutionError) as retained_confirmation_history:
                    await initial.create_run(
                        context=context,
                        capability_version_id=version.id,
                        arguments={"query": "retained confirmation key"},
                        confirmation_token=retained_key_token.confirmation_token,
                        idempotency_key="retained-confirmation-key",
                    )
                assert (
                    retained_confirmation_history.value.code
                    == ExecutionFailureCode.CONFIRMATION_KEY_HISTORY_INCOMPLETE
                )
                rotated_service = service(
                    session,
                    now=now,
                    confirmation_keys=(rotated_confirmation, *CONFIRMATION_KEYS),
                    idempotency_keys=(rotated, *IDEMPOTENCY_KEYS),
                )
                replay_token = await rotated_service.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "same"},
                )
                replay = await rotated_service.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "same"},
                    confirmation_token=replay_token.confirmation_token,
                    idempotency_key="rotating-key",
                )
                assert replay.id == run.id
                retired_key_token = await rotated_service.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "new request"},
                )
                with pytest.raises(ExecutionError) as incomplete_history:
                    await service(
                        session,
                        now=now + timedelta(days=91),
                        confirmation_keys=(rotated_confirmation, *CONFIRMATION_KEYS),
                        idempotency_keys=(rotated,),
                    ).create_run(
                        context=context,
                        capability_version_id=version.id,
                        arguments={"query": "new request"},
                        confirmation_token=retired_key_token.confirmation_token,
                        idempotency_key="retired-history",
                    )
                assert (
                    incomplete_history.value.code
                    == ExecutionFailureCode.IDEMPOTENCY_KEY_HISTORY_INCOMPLETE
                )
                confirmation_rotation = service(
                    session,
                    now=now,
                    confirmation_keys=(rotated_confirmation,),
                    idempotency_keys=(rotated, *IDEMPOTENCY_KEYS),
                )
                confirmation_rotation_token = await confirmation_rotation.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "confirmation rotation"},
                )
                with pytest.raises(ExecutionError) as missing_confirmation_history:
                    await confirmation_rotation.create_run(
                        context=context,
                        capability_version_id=version.id,
                        arguments={"query": "confirmation rotation"},
                        confirmation_token=confirmation_rotation_token.confirmation_token,
                        idempotency_key="confirmation-rotation",
                    )
                assert (
                    missing_confirmation_history.value.code
                    == ExecutionFailureCode.CONFIRMATION_KEY_HISTORY_INCOMPLETE
                )
                changed_token = await rotated_service.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "changed"},
                )
                with pytest.raises(ExecutionError) as conflict:
                    await rotated_service.create_run(
                        context=context,
                        capability_version_id=version.id,
                        arguments={"query": "changed"},
                        confirmation_token=changed_token.confirmation_token,
                        idempotency_key="rotating-key",
                    )
                assert conflict.value.code == ExecutionFailureCode.IDEMPOTENCY_CONFLICT

                deadline = now + timedelta(seconds=30)
                deadline_token = await rotated_service.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "deadline"},
                )
                deadline_run = await rotated_service.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "deadline"},
                    confirmation_token=deadline_token.confirmation_token,
                    idempotency_key="deadline-replay",
                    deadline=deadline,
                )
                replay_token = await service(
                    session,
                    now=now + timedelta(seconds=31),
                    confirmation_keys=(rotated_confirmation, *CONFIRMATION_KEYS),
                    idempotency_keys=(rotated, *IDEMPOTENCY_KEYS),
                ).preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "deadline"},
                )
                past_deadline_replay = await service(
                    session,
                    now=now + timedelta(seconds=31),
                    confirmation_keys=(rotated_confirmation, *CONFIRMATION_KEYS),
                    idempotency_keys=(rotated, *IDEMPOTENCY_KEYS),
                ).create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "deadline"},
                    confirmation_token=replay_token.confirmation_token,
                    idempotency_key="deadline-replay",
                    deadline=deadline,
                )
                assert past_deadline_replay.id == deadline_run.id

    asyncio.run(scenario())


def test_job_leasing_reclaims_only_with_a_new_epoch_and_rejects_stale_heartbeat() -> None:
    async def scenario() -> None:
        current = datetime(2026, 9, 6, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="lease")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(session, now=current)
                token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "lease"},
                )
                run = await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "lease"},
                    confirmation_token=token.confirmation_token,
                    idempotency_key="lease-key",
                )

            async with transaction(factory) as session:
                first = await service(session, now=current).claim_job(
                    worker_id="worker-one", lease_duration=timedelta(seconds=10)
                )
                assert first is not None
                assert first.run_id == run.id
                assert first.correlation_id == run.correlation_id
                assert first.lease_epoch == 1
                await service(session, now=current).fence_session(first)

            current += timedelta(seconds=11)
            async with transaction(factory) as session:
                second = await service(session, now=current).claim_job(
                    worker_id="worker-two", lease_duration=timedelta(seconds=10)
                )
                assert second is not None
                assert second.lease_epoch == 2
                with pytest.raises(ExecutionError) as stale:
                    await service(session, now=current).heartbeat(
                        first, lease_duration=timedelta(seconds=10)
                    )
                assert stale.value.code == ExecutionFailureCode.LEASE_LOST
                second_service = service(session, now=current)
                await second_service.fence_session(second)
                await second_service.fence_dispatch(second)
                completed = await second_service.complete_lease(
                    second,
                    status=RunStatus.SUCCEEDED,
                )
                assert completed.status == RunStatus.SUCCEEDED.value
                attempts = list(
                    (
                        await session.scalars(
                            select(RunAttempt)
                            .where(RunAttempt.run_id == run.id)
                            .order_by(RunAttempt.sequence)
                        )
                    ).all()
                )
                assert [(attempt.sequence, attempt.lease_epoch) for attempt in attempts] == [
                    (1, 1),
                    (2, 2),
                ]
                events = list(
                    (
                        await session.scalars(
                            select(RunEvent)
                            .where(RunEvent.run_id == run.id)
                            .order_by(RunEvent.sequence)
                        )
                    ).all()
                )
                lease_lost = next(event for event in events if event.event_type == "lease_lost")
                assert lease_lost.status == RunStatus.SESSION_FENCED.value
                assert ExecutionService.replay_projection(events) == RunStatus.SUCCEEDED

    asyncio.run(scenario())


def test_claim_terminalizes_work_with_stale_pinned_authority() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="stale-claim")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(session, now=now)
                token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "stale"},
                )
                run = await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "stale"},
                    confirmation_token=token.confirmation_token,
                    idempotency_key="stale-claim",
                )

            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                await CapabilityService(session).disable(
                    context=context,
                    capability_id=version.capability_id,
                    expected_version_id=version.id,
                )

            async with transaction(factory) as session:
                assert (
                    await service(session, now=now).claim_job(
                        worker_id="worker", lease_duration=timedelta(seconds=10)
                    )
                    is None
                )
                stale = await session.get(Run, run.id)
                assert stale is not None
                assert stale.status == RunStatus.FAILED.value
                assert stale.safe_error_code == RunFailureCode.PREPARATION_FAILED.value
                assert await session.scalar(select(func.count()).select_from(RunAttempt)) == 0

    asyncio.run(scenario())


def test_restore_quarantine_fences_old_jobs_and_retention_erases_content() -> None:
    async def scenario() -> None:
        current = datetime(2026, 9, 6, tzinfo=UTC)
        limits = ExecutionLimits(
            argument_retention_days=1,
            result_retention_days=1,
            run_retention_days=2,
        )
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="restore")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(
                    session,
                    now=current,
                    limits=limits,
                    system_authority=SYSTEM_AUTHORITY,
                )
                with pytest.raises(ExecutionError) as not_quarantined:
                    await execution.clear_restore_quarantine()
                assert not_quarantined.value.code == ExecutionFailureCode.INVALID_TRANSITION
                token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "retain"},
                )
                run = await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "retain"},
                    confirmation_token=token.confirmation_token,
                    idempotency_key="restore-key",
                )
                old_epoch = (await session.get(SystemExecutionState, 1)).execution_epoch  # type: ignore[union-attr]
                with pytest.raises(PermissionError, match="system execution authority"):
                    await service(session, now=current).enter_restore_quarantine()
                run_id = run.id

            async with transaction(factory) as session:
                execution = service(
                    session,
                    now=current,
                    limits=limits,
                    system_authority=SYSTEM_AUTHORITY,
                )
                new_epoch = await execution.enter_restore_quarantine()
                assert new_epoch == old_epoch + 1

            async with transaction(factory) as session:
                execution = service(
                    session,
                    now=current,
                    limits=limits,
                    system_authority=SYSTEM_AUTHORITY,
                )
                with pytest.raises(ExecutionError) as quarantined:
                    await execution.claim_job(
                        worker_id="blocked", lease_duration=timedelta(seconds=10)
                    )
                assert quarantined.value.code == ExecutionFailureCode.DISPATCH_QUARANTINED
                with pytest.raises(ExecutionError) as unreconciled:
                    await execution.clear_restore_quarantine()
                assert unreconciled.value.code == ExecutionFailureCode.INVALID_TRANSITION
                assert await execution.reconcile_restore_quarantine(batch_size=1) == 1
                assert await execution.reconcile_restore_quarantine(batch_size=1) == 0
                restored = await session.get(Run, run_id)
                assert restored is not None
                assert restored.status == RunStatus.CANCELLED.value
                assert await execution.clear_restore_quarantine() == new_epoch

            current += timedelta(days=1, seconds=1)
            async with transaction(factory) as session:
                execution = service(session, now=current, limits=limits)
                assert await execution.expire_retained_content() == 1
                retained = await session.get(Run, run_id)
                assert retained is not None
                assert retained.arguments is None
                assert retained.argument_digest is None
                assert await execution.expire_retained_content() == 0

            current += timedelta(days=2)
            async with transaction(factory) as session:
                execution = service(session, now=current, limits=limits)
                assert await execution.delete_expired_run_metadata() == 1
            async with factory() as session:
                assert await session.get(Run, run_id) is None
                assert (
                    await session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0
                )

    asyncio.run(scenario())


def test_cancellation_terminalizes_unfenced_work_and_is_idempotent() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="cancel")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(session, now=now)
                token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "cancel"},
                )
                run = await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "cancel"},
                    confirmation_token=token.confirmation_token,
                    idempotency_key="cancel-key",
                )
                cancelled = await execution.cancel_run(context=context, run_id=run.id)
                assert cancelled.status == RunStatus.CANCELLED.value
                assert cancelled.cancellation_requested is True
                assert (await execution.cancel_run(context=context, run_id=run.id)).id == run.id
                job = await session.scalar(select(Job).where(Job.run_id == run.id))
                assert job is not None
                assert job.status == RunStatus.CANCELLED.value
                assert (
                    await execution.claim_job(
                        worker_id="worker", lease_duration=timedelta(seconds=10)
                    )
                    is None
                )

                dispatched_token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "dispatched"},
                )
                dispatched = await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "dispatched"},
                    confirmation_token=dispatched_token.confirmation_token,
                    idempotency_key="dispatch-cancel-key",
                )
                lease = await execution.claim_job(
                    worker_id="worker", lease_duration=timedelta(seconds=10)
                )
                assert lease is not None
                await execution.fence_session(lease)
                await execution.fence_dispatch(lease)
                requested = await execution.cancel_run(context=context, run_id=dispatched.id)
                assert requested.status == RunStatus.DISPATCH_FENCED.value
                assert requested.cancellation_requested is True
                audit_actions = set(
                    (
                        await session.scalars(
                            select(AuditEvent.action).where(AuditEvent.resource_id == dispatched.id)
                        )
                    ).all()
                )
                assert AuditAction.RUN_CANCELLATION_REQUESTED.value in audit_actions
                assert AuditAction.RUN_CANCELLED.value not in audit_actions
                assert lease.run_id == dispatched.id
                assert (
                    await service(session, now=now + timedelta(seconds=11)).claim_job(
                        worker_id="replacement", lease_duration=timedelta(seconds=10)
                    )
                    is None
                )
                assert dispatched.status == RunStatus.INDETERMINATE.value
                attempts = list(
                    (
                        await session.scalars(
                            select(RunAttempt).where(RunAttempt.run_id == dispatched.id)
                        )
                    ).all()
                )
                assert len(attempts) == 1
                assert attempts[0].status == RunStatus.INDETERMINATE.value
                expired = await service(
                    session, now=now + timedelta(days=15)
                ).expire_retained_content()
                assert expired == 2
                assert dispatched.status == RunStatus.INDETERMINATE.value
                assert dispatched.arguments is None
                assert dispatched.argument_digest is None

    asyncio.run(scenario())


def test_cancellation_authorizes_before_revealing_run_existence() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="revoked-cancel")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(session, now=now)
                token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "cancel"},
                )
                run = await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "cancel"},
                    confirmation_token=token.confirmation_token,
                    idempotency_key="revoked-cancel",
                )
                membership = await session.get(WorkspaceMembership, (workspace_id, user_id))
                assert membership is not None
                membership.role = Role.VIEWER.value
                await session.flush()
                for run_id in (run.id, UUID(int=0)):
                    with pytest.raises(AuthorizationDenied):
                        await execution.cancel_run(context=context, run_id=run_id)

    asyncio.run(scenario())


def test_replay_protection_rows_reject_independent_orm_deletion() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="immutable-replay")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(session, now=now)
                token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "immutable"},
                )
                await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "immutable"},
                    confirmation_token=token.confirmation_token,
                    idempotency_key="immutable-replay",
                )

            for model in (RunEvent, ConfirmationNonce, IdempotencyRecord):
                async with factory() as session:
                    row = await session.scalar(select(model))
                    assert row is not None
                    await session.delete(row)
                    with pytest.raises(ValueError, match="immutable version rows"):
                        await session.flush()
                    await session.rollback()

    asyncio.run(scenario())


def test_heartbeat_and_worker_boundary_validation() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="heartbeat")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(session, now=now)
                token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "heartbeat"},
                )
                await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "heartbeat"},
                    confirmation_token=token.confirmation_token,
                    idempotency_key="heartbeat-key",
                )
                for worker_id, duration in (("", timedelta(seconds=1)), ("worker", timedelta(0))):
                    with pytest.raises(ValueError, match="invalid worker lease"):
                        await execution.claim_job(worker_id=worker_id, lease_duration=duration)
                lease = await execution.claim_job(
                    worker_id="worker", lease_duration=timedelta(seconds=10)
                )
                assert lease is not None
                with pytest.raises(ValueError, match="invalid worker lease"):
                    await execution.heartbeat(lease, lease_duration=timedelta(0))
                renewed = await execution.heartbeat(lease, lease_duration=timedelta(seconds=20))
                assert renewed.expires_at == now + timedelta(seconds=20)
                with pytest.raises(ExecutionError) as invalid:
                    await execution.complete_lease(renewed, status=RunStatus.PREPARING)
                assert invalid.value.code == ExecutionFailureCode.INVALID_TRANSITION
                with pytest.raises(ExecutionError) as arbitrary_code:
                    await execution.complete_lease(
                        renewed,
                        status=RunStatus.FAILED,
                        safe_error_code="upstream secret",  # type: ignore[arg-type]
                    )
                assert arbitrary_code.value.code == ExecutionFailureCode.INVALID_TRANSITION
                with pytest.raises(ExecutionError) as contradictory_success:
                    await execution.complete_lease(
                        renewed,
                        status=RunStatus.SUCCEEDED,
                        safe_error_code=RunFailureCode.TOOL_CALL_FAILED,
                    )
                assert contradictory_success.value.code == ExecutionFailureCode.INVALID_TRANSITION
                for invalid_status, invalid_code in (
                    (RunStatus.TIMED_OUT, RunFailureCode.DEADLINE_EXCEEDED),
                    (RunStatus.INDETERMINATE, RunFailureCode.UPSTREAM_OUTCOME_UNKNOWN),
                    (RunStatus.CANCELLED, RunFailureCode.CANCELLED_BEFORE_DISPATCH),
                ):
                    with pytest.raises(ExecutionError) as invalid_completion:
                        await execution.complete_lease(
                            renewed,
                            status=invalid_status,
                            safe_error_code=invalid_code,
                        )
                    assert invalid_completion.value.code == ExecutionFailureCode.INVALID_TRANSITION
                await execution.fence_session(renewed)
                await execution.fence_dispatch(renewed)
                failed = await execution.complete_lease(
                    renewed,
                    status=RunStatus.FAILED,
                    safe_error_code=RunFailureCode.TOOL_CALL_FAILED,
                )
                assert failed.safe_error_code == RunFailureCode.TOOL_CALL_FAILED.value

                overdue_token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "overdue completion"},
                )
                overdue_run = await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "overdue completion"},
                    confirmation_token=overdue_token.confirmation_token,
                    idempotency_key="overdue-completion-key",
                    deadline=now + timedelta(seconds=1),
                )
                overdue_lease = await execution.claim_job(
                    worker_id="deadline-worker", lease_duration=timedelta(seconds=10)
                )
                assert overdue_lease is not None
                assert overdue_lease.expires_at == now + timedelta(seconds=1)
                with pytest.raises(ExecutionError) as late_heartbeat:
                    await service(session, now=now + timedelta(seconds=2)).heartbeat(
                        overdue_lease,
                        lease_duration=timedelta(seconds=10),
                    )
                assert late_heartbeat.value.code == ExecutionFailureCode.LEASE_LOST
                overdue_result = await service(
                    session, now=now + timedelta(seconds=2)
                ).complete_lease(overdue_lease, status=RunStatus.SUCCEEDED)
                assert overdue_result.status == RunStatus.TIMED_OUT.value
                assert overdue_run.status == RunStatus.TIMED_OUT.value
                assert overdue_run.safe_error_code == RunFailureCode.DEADLINE_EXCEEDED.value

                deadline_token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "deadline"},
                )
                deadline_run = await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "deadline"},
                    confirmation_token=deadline_token.confirmation_token,
                    idempotency_key="deadline-key",
                    deadline=now + timedelta(seconds=1),
                )
                assert (
                    await service(session, now=now + timedelta(seconds=2)).claim_job(
                        worker_id="worker", lease_duration=timedelta(seconds=10)
                    )
                    is None
                )
                assert deadline_run.status == RunStatus.TIMED_OUT.value

    asyncio.run(scenario())


def test_success_persists_only_canonical_result_and_expires_it_absolutely() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="result")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(session, now=now)
                token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "result"},
                )
                run = await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "result"},
                    confirmation_token=token.confirmation_token,
                    idempotency_key="result",
                )
                lease = await execution.claim_job(
                    worker_id="worker", lease_duration=timedelta(seconds=30)
                )
                assert lease is not None
                await execution.fence_session(lease)
                await execution.fence_dispatch(lease)
                payload: dict[str, object] = {
                    "content": [{"type": "text", "text": "safe"}],
                    "structuredContent": {"message": "safe"},
                }
                canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                completed = await execution.complete_invocation_success(
                    lease,
                    result=AcceptedToolResult(
                        payload=payload,
                        canonical_digest=hashlib.sha256(canonical).hexdigest(),
                        byte_count=len(canonical),
                    ),
                )
                assert completed.status == RunStatus.SUCCEEDED.value
                stored = await session.get(RunResult, run.id)
                assert stored is not None
                assert stored.payload == payload
                assert stored.expires_at.replace(tzinfo=UTC) == now + timedelta(days=14)

            async with transaction(factory) as session:
                assert (
                    await service(session, now=now + timedelta(days=15)).expire_retained_results()
                    == 1
                )
                assert await session.get(RunResult, run.id) is None

    asyncio.run(scenario())


def test_late_definitive_result_terminalizes_without_storing_content() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="late-result")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(session, now=now)
                token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "late"},
                )
                run = await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "late"},
                    confirmation_token=token.confirmation_token,
                    idempotency_key="late-result",
                    deadline=now + timedelta(seconds=5),
                )
                lease = await execution.claim_job(
                    worker_id="worker", lease_duration=timedelta(seconds=30)
                )
                assert lease is not None
                await execution.fence_session(lease)
                await execution.fence_dispatch(lease)

            payload: dict[str, object] = {"content": [{"type": "text", "text": "late"}]}
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            async with transaction(factory) as session:
                completed = await service(
                    session, now=now + timedelta(seconds=6)
                ).complete_invocation_success(
                    lease,
                    result=AcceptedToolResult(
                        payload=payload,
                        canonical_digest=hashlib.sha256(canonical).hexdigest(),
                        byte_count=len(canonical),
                    ),
                )
                assert completed.status == RunStatus.INDETERMINATE.value
                assert await session.get(RunResult, run.id) is None

    asyncio.run(scenario())


def test_invocation_runner_commits_both_fences_and_one_safe_result() -> None:
    class FakeAdapter:
        calls = 0

        async def invoke(
            self,
            endpoint: str,
            *,
            tool_name: str,
            arguments: dict[str, object],
            output_schema: dict[str, object] | None,
            bearer_token: bytes | bytearray | None = None,
            before_session: Callable[[], Awaitable[None]],
            before_dispatch: Callable[[], Awaitable[None]],
        ) -> InvocationResult:
            del endpoint, tool_name, arguments, output_schema, bearer_token
            await before_session()
            await before_dispatch()
            self.calls += 1
            payload: dict[str, object] = {"content": [{"type": "text", "text": "done"}]}
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            return InvocationResult(
                payload=payload,
                canonical_digest=hashlib.sha256(canonical).hexdigest(),
                byte_count=len(canonical),
            )

    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        adapter = FakeAdapter()
        heartbeat_calls = 0
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="runner")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                secret = SecretBinding(
                    workspace_id=workspace_id,
                    provider="fixture",
                    external_reference="runner-secret",
                    version="v1",
                    created_by_user_id=user_id,
                )
                session.add(secret)
                await session.flush()
                version = await create_executable_target(
                    session, context, secret_binding_id=secret.id
                )
                execution = service(session, now=now)
                token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "runner"},
                )
                run = await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "runner"},
                    confirmation_token=token.confirmation_token,
                    idempotency_key="runner",
                )

            def execution_service_factory(session: AsyncSession) -> ExecutionService:
                class RecordingExecutionService(ExecutionService):
                    async def heartbeat(
                        self, lease: JobLease, *, lease_duration: timedelta
                    ) -> JobLease:
                        nonlocal heartbeat_calls
                        heartbeat_calls += 1
                        return await super().heartbeat(lease, lease_duration=lease_duration)

                return RecordingExecutionService(
                    session,
                    confirmation_keys=CONFIRMATION_KEYS,
                    idempotency_keys=IDEMPOTENCY_KEYS,
                    now=lambda: now,
                )

            runner = InvocationRunner(
                session_factory=factory,
                execution_service_factory=execution_service_factory,
                secret_provider=FixtureSecretProvider(
                    {("runner-secret", "v1"): b"runner-credential-value-123456"}
                ),
                adapter_factory=lambda _: cast(McpClientAdapter, adapter),
            )
            assert await runner.claim_and_run(
                worker_id="runner-worker", lease_duration=timedelta(seconds=30)
            )
            assert not await runner.claim_and_run(
                worker_id="runner-worker", lease_duration=timedelta(seconds=30)
            )
            assert adapter.calls == 1
            assert heartbeat_calls == 2
            async with factory() as session:
                stored_run = await session.get(Run, run.id)
                assert stored_run is not None
                assert stored_run.status == RunStatus.SUCCEEDED.value
                assert await session.get(RunResult, run.id) is not None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_code"),
    (
        ("factory", RunStatus.FAILED, RunFailureCode.PREPARATION_FAILED),
        ("preparation", RunStatus.FAILED, RunFailureCode.PREPARATION_FAILED),
        ("session", RunStatus.FAILED, RunFailureCode.SESSION_INITIALIZATION_FAILED),
        ("pre-sensitive", RunStatus.FAILED, RunFailureCode.SESSION_INITIALIZATION_FAILED),
        ("invalid", RunStatus.FAILED, RunFailureCode.INVALID_TOOL_RESULT),
        ("unsupported", RunStatus.FAILED, RunFailureCode.UNSUPPORTED_TOOL_RESULT),
        ("sensitive", RunStatus.FAILED, RunFailureCode.SENSITIVE_TOOL_RESULT),
        ("tool", RunStatus.FAILED, RunFailureCode.TOOL_CALL_FAILED),
        ("persistence", RunStatus.FAILED, RunFailureCode.INVALID_TOOL_RESULT),
        ("indeterminate", RunStatus.INDETERMINATE, RunFailureCode.UPSTREAM_OUTCOME_UNKNOWN),
    ),
)
def test_invocation_runner_maps_only_safe_failures(
    mode: str, expected_status: RunStatus, expected_code: RunFailureCode
) -> None:
    class FailingAdapter:
        async def invoke(
            self,
            endpoint: str,
            *,
            tool_name: str,
            arguments: dict[str, object],
            output_schema: dict[str, object] | None,
            bearer_token: bytes | bytearray | None = None,
            before_session: Callable[[], Awaitable[None]],
            before_dispatch: Callable[[], Awaitable[None]],
        ) -> InvocationResult:
            del endpoint, tool_name, arguments, output_schema, bearer_token
            if mode == "preparation":
                raise InvocationError(InvocationFailureCode.PREPARATION_FAILED)
            await before_session()
            if mode == "session":
                raise InvocationError(InvocationFailureCode.SESSION_INITIALIZATION_FAILED)
            if mode == "pre-sensitive":
                raise InvocationError(InvocationFailureCode.SENSITIVE_RESULT)
            await before_dispatch()
            if mode == "indeterminate":
                raise InvocationIndeterminate(
                    InvocationFailureCode.TOOL_CALL_FAILED, dispatched=True
                )
            if mode == "unsupported":
                raise InvocationError(
                    InvocationFailureCode.UNSUPPORTED_RESULT_CONTENT, dispatched=True
                )
            if mode == "sensitive":
                raise InvocationError(InvocationFailureCode.SENSITIVE_RESULT, dispatched=True)
            if mode == "tool":
                raise InvocationError(InvocationFailureCode.TOOL_CALL_FAILED, dispatched=True)
            if mode == "persistence":
                payload: dict[str, object] = {"content": [{"type": "text", "text": "long"}]}
                canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                return InvocationResult(
                    payload=payload,
                    canonical_digest=hashlib.sha256(canonical).hexdigest(),
                    byte_count=len(canonical),
                )
            raise InvocationError(InvocationFailureCode.INVALID_UPSTREAM_OUTPUT, dispatched=True)

    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject=f"runner-{mode}")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(session, now=now)
                token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": mode},
                )
                run = await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": mode},
                    confirmation_token=token.confirmation_token,
                    idempotency_key=f"runner-{mode}",
                )
            async with transaction(factory) as session:
                lease = await service(session, now=now).claim_job(
                    worker_id="runner-worker", lease_duration=timedelta(seconds=30)
                )
                assert lease is not None

            def adapter_factory(_: str) -> McpClientAdapter:
                if mode == "factory":
                    raise KeyError("unavailable policy")
                return cast(McpClientAdapter, FailingAdapter())

            runner = InvocationRunner(
                session_factory=factory,
                execution_service_factory=lambda session: service(
                    session,
                    now=now,
                    limits=(
                        ExecutionLimits(max_result_bytes=10) if mode == "persistence" else None
                    ),
                ),
                secret_provider=FixtureSecretProvider({}),
                adapter_factory=adapter_factory,
            )
            assert await runner.run(lease) == expected_status
            async with factory() as session:
                stored = await session.get(Run, run.id)
                assert stored is not None
                assert stored.status == expected_status.value
                assert stored.safe_error_code == expected_code.value
                assert await session.get(RunResult, run.id) is None

    asyncio.run(scenario())


def test_runner_emits_terminal_metric_for_reconciled_expired_dispatch() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        current = [now]
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="runner-reconcile")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(session, now=current[0])
                token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "reconcile"},
                )
                run = await execution.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "reconcile"},
                    confirmation_token=token.confirmation_token,
                    idempotency_key="runner-reconcile",
                    deadline=now + timedelta(seconds=1),
                )
                lease = await execution.claim_job(
                    worker_id="lost-worker", lease_duration=timedelta(seconds=30)
                )
                assert lease is not None
                await execution.fence_session(lease)
                await execution.fence_dispatch(lease)

            current[0] = now + timedelta(seconds=2)
            metrics = MetricsRegistry()
            runner = InvocationRunner(
                session_factory=factory,
                execution_service_factory=lambda session: service(session, now=current[0]),
                secret_provider=FixtureSecretProvider({}),
                adapter_factory=lambda _: pytest.fail("a reconciled job must not be invoked"),
                metrics=metrics,
            )

            assert not await runner.claim_and_run(
                worker_id="reconciliation-worker", lease_duration=timedelta(seconds=30)
            )
            async with factory() as session:
                stored = await session.get(Run, run.id)
                assert stored is not None
                assert stored.status == RunStatus.INDETERMINATE.value
                assert stored.safe_error_code == RunFailureCode.DEADLINE_EXCEEDED.value
            assert (
                'modall_worker_invocations_total{event="invocation_terminal",'
                'outcome="indeterminate"} 1' in metrics.render()
            )

    asyncio.run(scenario())


def test_worker_rechecks_its_qualified_protocol_at_claim_and_each_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="protocol-worker")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(session, now=now)

                async def admit(key: str) -> Run:
                    preflight = await execution.preflight(
                        context=context,
                        capability_version_id=version.id,
                        arguments={"query": key},
                    )
                    return await execution.create_run(
                        context=context,
                        capability_version_id=version.id,
                        arguments={"query": key},
                        confirmation_token=preflight.confirmation_token,
                        idempotency_key=key,
                    )

                stale_at_claim = await admit("stale-at-claim")
                monkeypatch.setattr(
                    "modall.execution.service.QUALIFIED_PROTOCOL_REVISION", "future-revision"
                )
                assert (
                    await execution.claim_job(
                        worker_id="worker", lease_duration=timedelta(seconds=30)
                    )
                    is None
                )
                assert stale_at_claim.status == RunStatus.FAILED.value

                monkeypatch.setattr(
                    "modall.execution.service.QUALIFIED_PROTOCOL_REVISION", "2025-06-18"
                )
                stale_at_session = await admit("stale-at-session")
                session_lease = await execution.claim_job(
                    worker_id="worker", lease_duration=timedelta(seconds=30)
                )
                assert session_lease is not None
                monkeypatch.setattr(
                    "modall.execution.service.QUALIFIED_PROTOCOL_REVISION", "future-revision"
                )
                with pytest.raises(ExecutionError) as session_fence:
                    await execution.fence_session(session_lease)
                assert session_fence.value.code == ExecutionFailureCode.LEASE_LOST
                assert stale_at_session.status == RunStatus.PREPARING.value

                monkeypatch.setattr(
                    "modall.execution.service.QUALIFIED_PROTOCOL_REVISION", "2025-06-18"
                )
                await execution.fence_session(session_lease)
                monkeypatch.setattr(
                    "modall.execution.service.QUALIFIED_PROTOCOL_REVISION", "future-revision"
                )
                with pytest.raises(ExecutionError) as dispatch_fence:
                    await execution.fence_dispatch(session_lease)
                assert dispatch_fence.value.code == ExecutionFailureCode.LEASE_LOST
                assert stale_at_session.status == RunStatus.SESSION_FENCED.value

    asyncio.run(scenario())


def test_admission_and_claim_refresh_time_after_slow_validation_and_target_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        issued_at = datetime(2026, 9, 6, tzinfo=UTC)
        current = issued_at
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="slow-boundaries")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                fixed = service(session, now=issued_at)
                expired_token = await fixed.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "slow-validation"},
                )

                async def slow_validation(
                    *args: object, **kwargs: object
                ) -> schema_validation.SchemaValidationResult:
                    nonlocal current
                    del args, kwargs
                    current = issued_at + timedelta(seconds=121)
                    return schema_validation.SchemaValidationResult.VALID

                monkeypatch.setattr(
                    "modall.execution.service.validate_schema_arguments", slow_validation
                )
                changing_clock = ExecutionService(
                    session,
                    confirmation_keys=CONFIRMATION_KEYS,
                    idempotency_keys=IDEMPOTENCY_KEYS,
                    now=lambda: current,
                )
                with pytest.raises(ExecutionError) as expired:
                    await changing_clock.create_run(
                        context=context,
                        capability_version_id=version.id,
                        arguments={"query": "slow-validation"},
                        confirmation_token=expired_token.confirmation_token,
                        idempotency_key="slow-validation",
                    )
                assert expired.value.code == ExecutionFailureCode.CONFIRMATION_EXPIRED

                current = issued_at
                monkeypatch.setattr(
                    "modall.execution.service.validate_schema_arguments",
                    schema_validation.validate_schema_arguments,
                )
                claim_token = await changing_clock.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "slow-lock"},
                )
                run = await changing_clock.create_run(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "slow-lock"},
                    confirmation_token=claim_token.confirmation_token,
                    idempotency_key="slow-lock",
                    deadline=issued_at + timedelta(seconds=1),
                )

                async def slow_target(_: Run) -> bool:
                    nonlocal current
                    current = issued_at + timedelta(seconds=2)
                    return True

                monkeypatch.setattr(changing_clock, "_claim_target_is_current", slow_target)
                assert (
                    await changing_clock.claim_job(
                        worker_id="slow-worker", lease_duration=timedelta(seconds=30)
                    )
                    is None
                )
                assert run.status == RunStatus.TIMED_OUT.value
                assert await session.scalar(select(func.count()).select_from(RunAttempt)) == 0

    asyncio.run(scenario())


def test_confirmation_limits_expiry_and_key_configuration_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="validation")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                version = await create_executable_target(session, context)
                execution = service(session, now=now)
                token = await execution.preflight(
                    context=context,
                    capability_version_id=version.id,
                    arguments={"query": "valid"},
                )
                with pytest.raises(ExecutionError) as expired:
                    await service(session, now=now + timedelta(minutes=3)).create_run(
                        context=context,
                        capability_version_id=version.id,
                        arguments={"query": "valid"},
                        confirmation_token=token.confirmation_token,
                        idempotency_key="expired",
                    )
                assert expired.value.code == ExecutionFailureCode.CONFIRMATION_EXPIRED
                for bad_token in ("", "not-a-jwt", "x" * 4097):
                    with pytest.raises(ExecutionError) as invalid:
                        await execution.create_run(
                            context=context,
                            capability_version_id=version.id,
                            arguments={"query": "valid"},
                            confirmation_token=bad_token,
                            idempotency_key="invalid-token",
                        )
                    assert invalid.value.code == ExecutionFailureCode.INVALID_CONFIRMATION
                with pytest.raises(ExecutionError) as bad_key:
                    await execution.create_run(
                        context=context,
                        capability_version_id=version.id,
                        arguments={"query": "valid"},
                        confirmation_token=token.confirmation_token,
                        idempotency_key="contains space",
                    )
                assert bad_key.value.code == ExecutionFailureCode.INVALID_IDEMPOTENCY_KEY
                with pytest.raises(ExecutionError) as oversized:
                    await service(
                        session,
                        now=now,
                        limits=ExecutionLimits(max_argument_bytes=8),
                    ).preflight(
                        context=context,
                        capability_version_id=version.id,
                        arguments={"query": "too large"},
                    )
                assert oversized.value.code == ExecutionFailureCode.ARGUMENT_LIMIT
                with pytest.raises(ExecutionError) as unavailable:
                    await execution.preflight(
                        context=context,
                        capability_version_id=UUID(int=0),
                        arguments={"query": "valid"},
                    )
                assert unavailable.value.code == ExecutionFailureCode.CAPABILITY_UNAVAILABLE

                def scanner_failure(value: object) -> bool:
                    del value
                    raise RuntimeError("scanner detail must not escape")

                monkeypatch.setattr(
                    "modall.execution.service.contains_sensitive_json", scanner_failure
                )
                with pytest.raises(ExecutionError) as scanner_failed:
                    await execution.preflight(
                        context=context,
                        capability_version_id=version.id,
                        arguments={"query": "valid"},
                    )
                assert scanner_failed.value.code == ExecutionFailureCode.SCANNER_FAILED
                for invalid_deadline in (
                    datetime(2026, 9, 6),
                    now - timedelta(seconds=1),
                    now + timedelta(hours=1),
                ):
                    with pytest.raises(ExecutionError) as invalid:
                        await execution.create_run(
                            context=context,
                            capability_version_id=version.id,
                            arguments={"query": "valid"},
                            confirmation_token=token.confirmation_token,
                            idempotency_key="deadline-validation",
                            deadline=invalid_deadline,
                        )
                    assert invalid.value.code == ExecutionFailureCode.INVALID_ARGUMENTS

            for keys in ((), (HmacKeyVersion("bad key", b"x" * 32),)):
                with pytest.raises(ValueError, match="invalid HMAC keyring"):
                    ExecutionService(
                        session,
                        confirmation_keys=keys,
                        idempotency_keys=IDEMPOTENCY_KEYS,
                    )
            with pytest.raises(ValueError, match="invalid execution limits"):
                ExecutionLimits(confirmation_ttl_seconds=0)
            with pytest.raises(ValueError, match="invalid execution limits"):
                ExecutionLimits(schema_validation_memory_bytes=32 * 1024 * 1024)
            with pytest.raises(ValueError, match="invalid execution limits"):
                ExecutionLimits(schema_validation_memory_bytes=512 * 1024 * 1024)
            with pytest.raises(ValueError, match="invalid execution limits"):
                ExecutionLimits(max_active_runs_per_workspace=101)
            with pytest.raises(ValueError, match="invalid execution limits"):
                ExecutionLimits(
                    argument_retention_days=1,
                    result_retention_days=14,
                    run_retention_days=1,
                )
            with pytest.raises(ValueError, match="run event history is empty"):
                ExecutionService.replay_projection([])
            with pytest.raises(ValueError, match="invalid cleanup batch"):
                await execution.expire_retained_content(batch_size=0)
            with pytest.raises(ValueError, match="invalid cleanup batch"):
                await execution.delete_expired_run_metadata(batch_size=1001)

    asyncio.run(scenario())


def test_linux_schema_validation_memory_limit_is_mandatory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        resource,
        "setrlimit",
        lambda resource_id, limits: calls.append((resource_id, limits)),
    )
    memory_limit = 256 * 1024 * 1024
    schema_validation._apply_validation_memory_limit(memory_limit)
    assert calls == [(resource.RLIMIT_AS, (memory_limit, memory_limit))]

    def fail_to_install(resource_id: int, limits: tuple[int, int]) -> None:
        del resource_id, limits
        raise OSError("unsupported")

    monkeypatch.setattr(resource, "setrlimit", fail_to_install)
    with pytest.raises(OSError, match="unsupported"):
        schema_validation._apply_validation_memory_limit(memory_limit)


def test_schema_validation_is_killable_and_invalid_schemas_fail_closed() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="schema-sandbox")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                pathological = await create_executable_target(
                    session,
                    context,
                    input_schema={
                        "type": "object",
                        "properties": {"query": {"type": "string", "pattern": "(a+)+$"}},
                        "required": ["query"],
                    },
                )
                with pytest.raises(ExecutionError) as timed_out:
                    await service(
                        session,
                        now=now,
                        limits=ExecutionLimits(schema_validation_timeout_seconds=0.05),
                    ).preflight(
                        context=context,
                        capability_version_id=pathological.id,
                        arguments={"query": "a" * 8191 + "!"},
                    )
                assert timed_out.value.code == ExecutionFailureCode.SCANNER_FAILED

                invalid = await create_executable_target(
                    session,
                    context,
                    input_schema={"type": "not-a-json-schema-type"},
                )
                with pytest.raises(ExecutionError) as invalid_schema:
                    await service(session, now=now).preflight(
                        context=context,
                        capability_version_id=invalid.id,
                        arguments={"query": "safe"},
                    )
                assert invalid_schema.value.code == ExecutionFailureCode.CAPABILITY_UNAVAILABLE

    asyncio.run(scenario())

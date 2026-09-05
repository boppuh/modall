import asyncio
import hashlib
import json
from datetime import timedelta
from typing import cast
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from modall.identity.types import WorkspaceContext
from modall.mcp_adapter.client import (
    CredentialError,
    DiscoveryError,
    DiscoveryResult,
    McpClientAdapter,
    ProtocolMismatch,
    ToolDefinition,
)
from modall.mcp_adapter.policy import (
    EndpointPolicyError,
    EndpointResolutionError,
    ResponseLimitExceeded,
)
from modall.persistence.database import transaction
from modall.persistence.models import (
    Capability,
    CapabilityStatusEvent,
    CapabilityVersion,
    DiscoveryPayload,
    DiscoveryRefreshJob,
    DiscoverySnapshot,
    ServerConnection,
    utc_now,
)
from modall.registry.discovery import (
    DiscoveryPublicationService,
    RefreshJobService,
    RefreshLease,
)
from modall.registry.runner import DiscoveryRunner, classify_discovery_failure
from modall.registry.service import (
    CapabilityService,
    ConnectionService,
    InvalidCapabilityTransition,
    InvalidConnectionTransition,
)
from modall.registry.types import (
    CapabilityStatus,
    ConnectionLifecycle,
    DiscoveryFailureCode,
    RefreshJobStatus,
)
from modall.secrets.provider import FixtureSecretProvider, SecretProviderError
from tests.test_registry import admin_context, bootstrap, database


async def lease_for(
    session: AsyncSession, context: WorkspaceContext, connection_id: UUID, worker_id: str
) -> RefreshLease:
    job = await RefreshJobService(session).enqueue(context=context, connection_id=connection_id)
    return await RefreshJobService(session).claim(
        context=context,
        job_id=job.id,
        worker_id=worker_id,
        lease_duration=timedelta(minutes=1),
    )


def result_for(*, description: str = "Echo text", include_tool: bool = True) -> DiscoveryResult:
    tools: tuple[ToolDefinition, ...] = ()
    normalized_tools: list[dict[str, object]] = []
    if include_tool:
        normalized: dict[str, object] = {
            "name": "echo",
            "title": "Echo",
            "description": description,
            "inputSchema": {"type": "object"},
        }
        encoded_tool = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        tool = ToolDefinition(
            identity="echo",
            name="echo",
            display_name="Echo",
            description=description,
            input_schema={"type": "object"},
            output_schema=None,
            metadata_digest=hashlib.sha256(encoded_tool).hexdigest(),
            schema_supported=True,
            normalized=normalized,
        )
        tools = (tool,)
        normalized_tools.append(normalized)
    payload: dict[str, object] = {
        "protocolRevision": "2025-06-18",
        "tools": normalized_tools,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return DiscoveryResult(
        protocol_revision="2025-06-18",
        tools=tools,
        normalized_payload=payload,
        canonical_bytes=canonical,
        canonical_digest=hashlib.sha256(canonical).hexdigest(),
    )


def test_successful_refreshes_append_observations_deduplicate_and_detect_drift() -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="discovery-admin")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Discovery",
                    endpoint_url="https://mcp.example/discovery",
                    secret_binding_id=None,
                    policy_version="policy-v1",
                )
                connection_id = connection.id
                lease = await lease_for(session, context, connection_id, "worker-1")
                first = await DiscoveryPublicationService(session).publish_success(
                    context=context,
                    lease=lease,
                    result=result_for(),
                )
                capability = await session.scalar(
                    select(Capability).where(Capability.connection_id == connection_id)
                )
                assert capability is not None and capability.pending_version_id is not None
                await CapabilityService(session).enable(
                    context=context,
                    capability_id=capability.id,
                    expected_version_id=capability.pending_version_id,
                )
                first_payload_id = first.payload_id
                first_snapshot_id = first.id

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                lease = await lease_for(session, context, connection_id, "worker-2")
                second = await DiscoveryPublicationService(session).publish_success(
                    context=context,
                    lease=lease,
                    result=result_for(),
                )
                assert second.id != first_snapshot_id
                assert second.payload_id == first_payload_id
                assert (
                    await session.scalar(select(func.count()).select_from(DiscoverySnapshot)) == 2
                )
                assert await session.scalar(select(func.count()).select_from(DiscoveryPayload)) == 1
                assert (
                    await session.scalar(select(func.count()).select_from(CapabilityVersion)) == 1
                )

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                lease = await lease_for(session, context, connection_id, "worker-3")
                await DiscoveryPublicationService(session).publish_success(
                    context=context,
                    lease=lease,
                    result=result_for(description="Echo text safely"),
                )
                capability = await session.scalar(
                    select(Capability).where(Capability.connection_id == connection_id)
                )
                assert capability is not None
                assert capability.typed_status == CapabilityStatus.PENDING_REVIEW
                assert capability.pending_version_id is not None
                assert (
                    await session.scalar(select(func.count()).select_from(CapabilityVersion)) == 2
                )

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                lease = await lease_for(session, context, connection_id, "worker-4")
                await DiscoveryPublicationService(session).publish_success(
                    context=context,
                    lease=lease,
                    result=result_for(),
                )
                capability = await session.scalar(
                    select(Capability).where(Capability.connection_id == connection_id)
                )
                assert capability is not None
                assert capability.typed_status == CapabilityStatus.ENABLED
                assert capability.pending_version_id is None
                assert capability.enabled_version_id is not None
                assert (
                    await session.scalar(select(func.count()).select_from(CapabilityVersion)) == 2
                )

    asyncio.run(scenario())


def test_manual_disable_survives_drift_disappearance_and_reversion() -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="disabled-reversion-admin")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Disabled reversion",
                    endpoint_url="https://mcp.example/disabled-reversion",
                    secret_binding_id=None,
                    policy_version="policy-v1",
                )
                connection_id = connection.id
                first_lease = await lease_for(session, context, connection_id, "worker-1")
                await DiscoveryPublicationService(session).publish_success(
                    context=context, lease=first_lease, result=result_for()
                )
                capability = await session.scalar(
                    select(Capability).where(Capability.connection_id == connection_id)
                )
                assert capability is not None and capability.pending_version_id is not None
                approved_version_id = capability.pending_version_id
                await CapabilityService(session).enable(
                    context=context,
                    capability_id=capability.id,
                    expected_version_id=approved_version_id,
                )

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                drift_lease = await lease_for(session, context, connection_id, "worker-2")
                await DiscoveryPublicationService(session).publish_success(
                    context=context,
                    lease=drift_lease,
                    result=result_for(description="drifted"),
                )
                capability = await session.scalar(
                    select(Capability).where(Capability.connection_id == connection_id)
                )
                assert capability is not None and capability.pending_version_id is not None
                await CapabilityService(session).disable(
                    context=context,
                    capability_id=capability.id,
                    expected_version_id=capability.pending_version_id,
                )

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                missing_lease = await lease_for(session, context, connection_id, "worker-3")
                await DiscoveryPublicationService(session).publish_success(
                    context=context,
                    lease=missing_lease,
                    result=result_for(include_tool=False),
                )
                capability = await session.scalar(
                    select(Capability).where(Capability.connection_id == connection_id)
                )
                assert capability is not None
                assert capability.typed_status == CapabilityStatus.DISABLED

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                return_lease = await lease_for(session, context, connection_id, "worker-4")
                await DiscoveryPublicationService(session).publish_success(
                    context=context, lease=return_lease, result=result_for()
                )
                capability = await session.scalar(
                    select(Capability).where(Capability.connection_id == connection_id)
                )
                assert capability is not None
                assert capability.typed_status == CapabilityStatus.DISABLED
                assert capability.pending_version_id is None
                assert capability.enabled_version_id == approved_version_id
                assert (
                    await session.scalar(select(func.count()).select_from(CapabilityVersion)) == 2
                )

    asyncio.run(scenario())


def test_enabled_capability_reappears_without_reapproval() -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="enabled-reappearance-admin")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Enabled reappearance",
                    endpoint_url="https://mcp.example/enabled-reappearance",
                    secret_binding_id=None,
                    policy_version="policy-v1",
                )
                connection_id = connection.id
                first_lease = await lease_for(session, context, connection_id, "worker-1")
                await DiscoveryPublicationService(session).publish_success(
                    context=context, lease=first_lease, result=result_for()
                )
                capability = await session.scalar(
                    select(Capability).where(Capability.connection_id == connection_id)
                )
                assert capability is not None and capability.pending_version_id is not None
                approved_version_id = capability.pending_version_id
                await CapabilityService(session).enable(
                    context=context,
                    capability_id=capability.id,
                    expected_version_id=approved_version_id,
                )

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                missing_lease = await lease_for(session, context, connection_id, "worker-2")
                await DiscoveryPublicationService(session).publish_success(
                    context=context,
                    lease=missing_lease,
                    result=result_for(include_tool=False),
                )

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                return_lease = await lease_for(session, context, connection_id, "worker-3")
                await DiscoveryPublicationService(session).publish_success(
                    context=context, lease=return_lease, result=result_for()
                )
                capability = await session.scalar(
                    select(Capability).where(Capability.connection_id == connection_id)
                )
                assert capability is not None
                assert capability.typed_status == CapabilityStatus.ENABLED
                assert capability.enabled_version_id == approved_version_id
                assert capability.pending_version_id is None
                assert (
                    await session.scalar(select(func.count()).select_from(CapabilityVersion)) == 1
                )

    asyncio.run(scenario())


def test_connection_disable_obsoletes_queued_and_leased_refresh_jobs() -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="disable-jobs-admin")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                queued_connection = await ConnectionService(session).create(
                    context=context,
                    name="Queued refresh",
                    endpoint_url="https://mcp.example/queued",
                    secret_binding_id=None,
                    policy_version="policy-v1",
                )
                leased_connection = await ConnectionService(session).create(
                    context=context,
                    name="Leased refresh",
                    endpoint_url="https://mcp.example/leased",
                    secret_binding_id=None,
                    policy_version="policy-v1",
                )
                queued_job = await RefreshJobService(session).enqueue(
                    context=context, connection_id=queued_connection.id
                )
                leased_job = await RefreshJobService(session).enqueue(
                    context=context, connection_id=leased_connection.id
                )
                await RefreshJobService(session).claim(
                    context=context,
                    job_id=leased_job.id,
                    worker_id="worker",
                    lease_duration=timedelta(minutes=1),
                )

                await ConnectionService(session).disable(
                    context=context, connection_id=queued_connection.id
                )
                await ConnectionService(session).disable(
                    context=context, connection_id=leased_connection.id
                )
                for job_id in (queued_job.id, leased_job.id):
                    job = await session.get(DiscoveryRefreshJob, job_id)
                    assert job is not None
                    assert job.status == RefreshJobStatus.OBSOLETE.value
                    assert job.lease_owner is None
                    assert job.lease_expires_at is None

    asyncio.run(scenario())


def test_missing_tools_and_eligible_failure_update_only_current_projections() -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="failure-admin")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Failure",
                    endpoint_url="https://mcp.example/failure",
                    secret_binding_id=None,
                    policy_version="policy-v1",
                )
                connection_id = connection.id
                lease = await lease_for(session, context, connection_id, "worker-1")
                snapshot = await DiscoveryPublicationService(session).publish_success(
                    context=context,
                    lease=lease,
                    result=result_for(),
                )
                snapshot_id = snapshot.id
                capability = await session.scalar(
                    select(Capability).where(Capability.connection_id == connection_id)
                )
                assert capability is not None and capability.pending_version_id is not None
                historical_version_id = capability.pending_version_id
                await CapabilityService(session).enable(
                    context=context,
                    capability_id=capability.id,
                    expected_version_id=historical_version_id,
                )

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                lease = await lease_for(session, context, connection_id, "worker-2")
                await DiscoveryPublicationService(session).publish_success(
                    context=context,
                    lease=lease,
                    result=result_for(include_tool=False),
                )
                capability = await session.scalar(
                    select(Capability).where(Capability.connection_id == connection_id)
                )
                assert capability is not None
                assert capability.typed_status == CapabilityStatus.UNAVAILABLE
                with pytest.raises(InvalidCapabilityTransition):
                    await CapabilityService(session).enable(
                        context=context,
                        capability_id=capability.id,
                        expected_version_id=historical_version_id,
                    )

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                stale_lease = await lease_for(session, context, connection_id, "stale-worker")
                current_lease = await lease_for(session, context, connection_id, "current-worker")
                with pytest.raises(InvalidConnectionTransition):
                    await DiscoveryPublicationService(session).publish_failure(
                        context=context,
                        lease=stale_lease,
                        error_code=DiscoveryFailureCode.TIMEOUT,
                    )
                failed = await DiscoveryPublicationService(session).publish_failure(
                    context=context,
                    lease=current_lease,
                    error_code=DiscoveryFailureCode.TIMEOUT,
                )
                assert failed.typed_lifecycle == ConnectionLifecycle.DEGRADED
                assert failed.last_refresh_error_code == "timeout"
                assert failed.current_snapshot_id != snapshot_id
                current_snapshot_id = failed.current_snapshot_id

            async with transaction(factory) as session:
                loaded = await session.get(ServerConnection, connection_id)
                assert loaded is not None
                assert loaded.current_snapshot_id == current_snapshot_id
                assert (
                    await session.scalar(select(func.count()).select_from(DiscoverySnapshot)) == 2
                )

    asyncio.run(scenario())


def test_failed_pending_verification_can_be_retried_and_promoted() -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="retry-admin")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Retry",
                    endpoint_url="https://mcp.example/retry",
                    secret_binding_id=None,
                    policy_version="policy-v1",
                )
                connection_id = connection.id
                first_lease = await lease_for(session, context, connection_id, "worker-1")
                failed = await DiscoveryPublicationService(session).publish_failure(
                    context=context,
                    lease=first_lease,
                    error_code=DiscoveryFailureCode.TIMEOUT,
                )
                assert failed.typed_lifecycle == ConnectionLifecycle.DEGRADED
                pending_version_id = failed.pending_version_id

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                retry_lease = await lease_for(session, context, connection_id, "worker-2")
                retrying = await session.get(ServerConnection, connection_id)
                assert retrying is not None
                assert retrying.typed_lifecycle == ConnectionLifecycle.VERIFYING
                await DiscoveryPublicationService(session).publish_success(
                    context=context,
                    lease=retry_lease,
                    result=result_for(),
                )
                assert retrying.lifecycle == ConnectionLifecycle.ACTIVE.value
                assert retrying.pending_version_id is None
                assert retrying.verified_version_id == pending_version_id

                await ConnectionService(session).disable(
                    context=context, connection_id=connection_id
                )
                await ConnectionService(session).enable(
                    context=context, connection_id=connection_id
                )
                reverify_lease = await lease_for(session, context, connection_id, "worker-3")
                await DiscoveryPublicationService(session).publish_success(
                    context=context,
                    lease=reverify_lease,
                    result=result_for(),
                )
                assert retrying.lifecycle == ConnectionLifecycle.ACTIVE.value

    asyncio.run(scenario())


def test_pending_capability_reappearance_reuses_its_immutable_version() -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="reappearance-admin")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Reappearance",
                    endpoint_url="https://mcp.example/reappearance",
                    secret_binding_id=None,
                    policy_version="policy-v1",
                )
                connection_id = connection.id
                lease = await lease_for(session, context, connection_id, "worker-1")
                await DiscoveryPublicationService(session).publish_success(
                    context=context, lease=lease, result=result_for()
                )
                capability = await session.scalar(
                    select(Capability).where(Capability.connection_id == connection_id)
                )
                assert capability is not None and capability.pending_version_id is not None
                pending_version_id = capability.pending_version_id

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                missing_lease = await lease_for(session, context, connection_id, "worker-2")
                await DiscoveryPublicationService(session).publish_success(
                    context=context,
                    lease=missing_lease,
                    result=result_for(include_tool=False),
                )
                capability = await session.scalar(
                    select(Capability).where(Capability.connection_id == connection_id)
                )
                assert capability is not None
                assert capability.typed_status == CapabilityStatus.UNAVAILABLE
                assert capability.pending_version_id == pending_version_id
                unavailable_event = await session.scalar(
                    select(CapabilityStatusEvent).where(
                        CapabilityStatusEvent.capability_id == capability.id,
                        CapabilityStatusEvent.status == CapabilityStatus.UNAVAILABLE.value,
                    )
                )
                assert unavailable_event is not None
                assert unavailable_event.capability_version_id == pending_version_id

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                return_lease = await lease_for(session, context, connection_id, "worker-3")
                await DiscoveryPublicationService(session).publish_success(
                    context=context, lease=return_lease, result=result_for()
                )
                capability = await session.scalar(
                    select(Capability).where(Capability.connection_id == connection_id)
                )
                assert capability is not None
                assert capability.typed_status == CapabilityStatus.PENDING_REVIEW
                assert capability.pending_version_id == pending_version_id
                assert (
                    await session.scalar(select(func.count()).select_from(CapabilityVersion)) == 1
                )

    asyncio.run(scenario())


def test_refresh_lease_reclaim_invalidates_the_previous_worker() -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="lease-admin")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Lease",
                    endpoint_url="https://mcp.example/lease",
                    secret_binding_id=None,
                    policy_version="policy-v1",
                )
                job = await RefreshJobService(session).enqueue(
                    context=context, connection_id=connection.id
                )
                claimed_at = utc_now()
                old_lease = await RefreshJobService(session).claim(
                    context=context,
                    job_id=job.id,
                    worker_id="old-worker",
                    lease_duration=timedelta(seconds=1),
                    now=claimed_at,
                )
                with pytest.raises(InvalidConnectionTransition):
                    await RefreshJobService(session).claim(
                        context=context,
                        job_id=job.id,
                        worker_id="too-early",
                        lease_duration=timedelta(seconds=1),
                        now=claimed_at,
                    )
                new_lease = await RefreshJobService(session).claim(
                    context=context,
                    job_id=job.id,
                    worker_id="new-worker",
                    lease_duration=timedelta(minutes=5),
                    now=claimed_at + timedelta(seconds=2),
                )
                assert new_lease.lease_epoch == old_lease.lease_epoch + 1
                assert (
                    await RefreshJobService(session).renew(
                        context=context,
                        lease=new_lease,
                        lease_duration=timedelta(minutes=10),
                        now=claimed_at + timedelta(seconds=3),
                    )
                    == new_lease
                )
                with pytest.raises(InvalidConnectionTransition):
                    await RefreshJobService(session).renew(
                        context=context,
                        lease=old_lease,
                        lease_duration=timedelta(minutes=1),
                        now=claimed_at + timedelta(seconds=3),
                    )
                with pytest.raises(InvalidConnectionTransition):
                    await DiscoveryPublicationService(session).publish_success(
                        context=context,
                        lease=old_lease,
                        result=result_for(),
                    )
                await DiscoveryPublicationService(session).publish_success(
                    context=context,
                    lease=new_lease,
                    result=result_for(),
                )

    asyncio.run(scenario())


def test_discovery_runner_publishes_success_and_safe_failure_codes() -> None:
    class StubAdapter:
        def __init__(self, outcome: DiscoveryResult | Exception) -> None:
            self.outcome = outcome

        async def discover(
            self, endpoint: str, *, bearer_token: bytes | bytearray | None = None
        ) -> DiscoveryResult:
            assert endpoint.startswith("https://")
            assert bearer_token is None
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="runner-admin")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Runner",
                    endpoint_url="https://mcp.example/runner",
                    secret_binding_id=None,
                    policy_version="policy-v1",
                )
                connection_id = connection.id
                lease = await lease_for(session, context, connection_id, "runner-1")

            success_adapter = StubAdapter(result_for())
            selected_policies: list[str] = []

            def success_factory(policy_version: str) -> McpClientAdapter:
                selected_policies.append(policy_version)
                return cast(McpClientAdapter, success_adapter)

            runner = DiscoveryRunner(
                session_factory=factory,
                secret_provider=FixtureSecretProvider({}),
                adapter_factory=success_factory,
            )
            assert await runner.run(context=context, lease=lease) is not None
            assert selected_policies == ["policy-v1"]

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                failure_lease = await lease_for(session, context, connection_id, "runner-2")

            failure_adapter = StubAdapter(ProtocolMismatch("safe"))
            runner = DiscoveryRunner(
                session_factory=factory,
                secret_provider=FixtureSecretProvider({}),
                adapter_factory=lambda _policy_version: cast(McpClientAdapter, failure_adapter),
            )
            assert await runner.run(context=context, lease=failure_lease) is None
            async with transaction(factory) as session:
                loaded = await session.get(ServerConnection, connection_id)
                assert loaded is not None
                assert loaded.last_refresh_error_code == "protocol_mismatch"

    asyncio.run(scenario())
    assert classify_discovery_failure(RuntimeError("safe")) == (
        DiscoveryFailureCode.TRANSPORT_FAILED
    )


def test_discovery_runner_does_not_report_publication_errors_as_endpoint_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubAdapter:
        async def discover(
            self, endpoint: str, *, bearer_token: bytes | bytearray | None = None
        ) -> DiscoveryResult:
            del endpoint, bearer_token
            return result_for()

    async def fail_publication(
        service: DiscoveryPublicationService, **values: object
    ) -> DiscoverySnapshot:
        del service, values
        raise RuntimeError("database publication failed")

    monkeypatch.setattr(DiscoveryPublicationService, "publish_success", fail_publication)

    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="publication-error-admin")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Publication error",
                    endpoint_url="https://mcp.example/publication-error",
                    secret_binding_id=None,
                    policy_version="policy-v1",
                )
                connection_id = connection.id
                lease = await lease_for(session, context, connection_id, "runner")

            runner = DiscoveryRunner(
                session_factory=factory,
                secret_provider=FixtureSecretProvider({}),
                adapter_factory=lambda _policy_version: cast(McpClientAdapter, StubAdapter()),
            )
            with pytest.raises(RuntimeError, match="publication failed"):
                await runner.run(context=context, lease=lease)

            async with factory() as session:
                loaded = await session.get(ServerConnection, connection_id)
                assert loaded is not None
                assert loaded.last_refresh_error_code is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProtocolMismatch("safe"), DiscoveryFailureCode.PROTOCOL_MISMATCH),
        (CredentialError("safe"), DiscoveryFailureCode.AUTHENTICATION_FAILED),
        (EndpointPolicyError("safe"), DiscoveryFailureCode.ENDPOINT_REJECTED),
        (EndpointResolutionError("safe"), DiscoveryFailureCode.TRANSPORT_FAILED),
        (ResponseLimitExceeded("safe"), DiscoveryFailureCode.RESPONSE_LIMIT),
        (TimeoutError(), DiscoveryFailureCode.TIMEOUT),
        (SecretProviderError("safe"), DiscoveryFailureCode.AUTHENTICATION_FAILED),
        (DiscoveryError("safe"), DiscoveryFailureCode.INVALID_METADATA),
        (
            httpx.ConnectError("safe", request=httpx.Request("GET", "https://mcp.example")),
            DiscoveryFailureCode.TRANSPORT_FAILED,
        ),
        (RuntimeError("safe"), DiscoveryFailureCode.TRANSPORT_FAILED),
    ],
)
def test_discovery_failure_classification_is_allowlisted(
    error: Exception, expected: DiscoveryFailureCode
) -> None:
    assert classify_discovery_failure(ExceptionGroup("group", [error])) == expected

    request = httpx.Request("GET", "https://mcp.example")
    response = httpx.Response(401, request=request)
    authentication_error = httpx.HTTPStatusError(
        "upstream body must not escape", request=request, response=response
    )
    assert classify_discovery_failure(authentication_error) == (
        DiscoveryFailureCode.AUTHENTICATION_FAILED
    )

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modall.audit.types import AuditAction
from modall.identity.repository import AuthorizationDenied, AuthorizationService
from modall.identity.service import IdentityService
from modall.identity.types import Permission, Principal, Role, WorkspaceContext
from modall.persistence.database import create_engine, create_session_factory, transaction
from modall.persistence.models import (
    AuditEvent,
    Base,
    CapabilityStatusEvent,
    CapabilityVersion,
    McpToolBinding,
    SecretBinding,
    ServerConnection,
    ServerConnectionVersion,
)
from modall.registry.service import (
    CapabilityService,
    ConnectionService,
    InvalidCapabilityTransition,
    InvalidConnectionTransition,
)
from modall.registry.types import CapabilityStatus, ConnectionLifecycle


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


async def admin_context(
    session: AsyncSession, *, user_id: UUID, workspace_id: UUID
) -> WorkspaceContext:
    return await AuthorizationService(session).authorize(
        user_id=user_id,
        workspace_id=workspace_id,
        permission=Permission.MANAGE_CONNECTION_CONFIGURATION,
    )


def test_connection_versions_and_lifecycle_are_truthful() -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="connection-admin")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name=" Production MCP ",
                    endpoint_url="https://mcp.example/v1",
                    secret_binding_id=None,
                    policy_version="policy-v1",
                )
                connection_id = connection.id
                first_version_id = connection.pending_version_id
                assert connection.name == "Production MCP"
                assert connection.typed_lifecycle == ConnectionLifecycle.VERIFYING
                assert first_version_id is not None
                assert connection.verified_version_id is None
                assert ConnectionService.is_executable(connection, first_version_id) is False

                with pytest.raises(InvalidConnectionTransition):
                    await ConnectionService(session).promote_pending(
                        context=context,
                        connection_id=connection_id,
                        expected_version_id=first_version_id,
                        expected_control_epoch=0,
                        expected_refresh_generation=0,
                    )

                generation, epoch, target = await ConnectionService(
                    session
                ).allocate_refresh_generation(context=context, connection_id=connection_id)
                assert (generation, epoch, target) == (1, 0, first_version_id)

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                current_generation, current_epoch, _ = await ConnectionService(
                    session
                ).allocate_refresh_generation(context=context, connection_id=connection_id)
                with pytest.raises(InvalidConnectionTransition):
                    await ConnectionService(session).promote_pending(
                        context=context,
                        connection_id=connection_id,
                        expected_version_id=uuid4(),
                        expected_control_epoch=current_epoch,
                        expected_refresh_generation=current_generation,
                    )
                with pytest.raises(InvalidConnectionTransition):
                    await ConnectionService(session).promote_pending(
                        context=context,
                        connection_id=connection_id,
                        expected_version_id=first_version_id,
                        expected_control_epoch=epoch,
                        expected_refresh_generation=generation,
                    )
                promoted = await ConnectionService(session).promote_pending(
                    context=context,
                    connection_id=connection_id,
                    expected_version_id=first_version_id,
                    expected_control_epoch=current_epoch,
                    expected_refresh_generation=current_generation,
                )
                assert promoted.typed_lifecycle == ConnectionLifecycle.ACTIVE
                assert promoted.pending_version_id is None
                assert promoted.verified_version_id == first_version_id
                assert ConnectionService.is_executable(promoted, first_version_id) is True

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                version = await ConnectionService(session).append_version(
                    context=context,
                    connection_id=connection_id,
                    endpoint_url="https://mcp.example/v2",
                    secret_binding_id=None,
                    policy_version="policy-v2",
                )
                assert version.sequence == 2
                loaded_connection = await session.get(ServerConnection, connection_id)
                assert loaded_connection is not None
                assert loaded_connection.pending_version_id == version.id
                assert loaded_connection.verified_version_id == first_version_id
                assert loaded_connection.control_epoch == 1
                assert loaded_connection.typed_lifecycle == ConnectionLifecycle.VERIFYING
                assert ConnectionService.is_executable(loaded_connection, first_version_id) is False

            async with factory() as session:
                events = list(
                    (
                        await session.scalars(
                            select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
                        )
                    ).all()
                )
                assert AuditAction.CONNECTION_CREATED.value in {event.action for event in events}
                assert AuditAction.CONNECTION_VERSION_APPENDED.value in {
                    event.action for event in events
                }
                assert AuditAction.CONNECTION_VERIFIED.value in {event.action for event in events}

    asyncio.run(scenario())


def test_disable_enable_and_role_boundaries() -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="lifecycle-admin")
            async with transaction(factory) as session:
                identity = IdentityService(session)
                operator = await identity.resolve_user(Principal("issuer", "operator", None))
                operator_id = operator.id
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                await identity.set_membership_role(
                    context=context, user_id=operator_id, role=Role.OPERATOR
                )
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Lifecycle",
                    endpoint_url="https://mcp.example/service",
                    secret_binding_id=None,
                    policy_version="v1",
                )
                connection_id = connection.id
                version_id = connection.pending_version_id
                assert version_id is not None
                generation, epoch, _ = await ConnectionService(session).allocate_refresh_generation(
                    context=context, connection_id=connection_id
                )
                await ConnectionService(session).promote_pending(
                    context=context,
                    connection_id=connection_id,
                    expected_version_id=version_id,
                    expected_control_epoch=epoch,
                    expected_refresh_generation=generation,
                )

            async with transaction(factory) as session:
                operator_context = await AuthorizationService(session).authorize(
                    user_id=operator_id,
                    workspace_id=workspace_id,
                    permission=Permission.DISABLE_CONNECTION,
                )
                disabled = await ConnectionService(session).disable(
                    context=operator_context, connection_id=connection_id
                )
                assert disabled.typed_lifecycle == ConnectionLifecycle.DISABLED
                assert disabled.control_epoch == 1
                with pytest.raises(InvalidConnectionTransition):
                    await ConnectionService(session).allocate_refresh_generation(
                        context=operator_context, connection_id=connection_id
                    )
                with pytest.raises(AuthorizationDenied):
                    await ConnectionService(session).enable(
                        context=operator_context, connection_id=connection_id
                    )

            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                with pytest.raises(InvalidConnectionTransition):
                    await ConnectionService(session).append_version(
                        context=context,
                        connection_id=connection_id,
                        endpoint_url="https://mcp.example/disabled-change",
                        secret_binding_id=None,
                        policy_version="v2",
                    )
                enabled = await ConnectionService(session).enable(
                    context=context, connection_id=connection_id
                )
                assert enabled.typed_lifecycle == ConnectionLifecycle.VERIFYING
                assert enabled.control_epoch == 2
                with pytest.raises(InvalidConnectionTransition):
                    await ConnectionService(session).enable(
                        context=context, connection_id=connection_id
                    )
                generation, epoch, target = await ConnectionService(
                    session
                ).allocate_refresh_generation(context=context, connection_id=connection_id)
                assert target == version_id
                reverified = await ConnectionService(session).promote_pending(
                    context=context,
                    connection_id=connection_id,
                    expected_version_id=version_id,
                    expected_control_epoch=epoch,
                    expected_refresh_generation=generation,
                )
                assert reverified.typed_lifecycle == ConnectionLifecycle.ACTIVE
                assert reverified.pending_version_id is None
                assert reverified.verified_version_id == version_id

    asyncio.run(scenario())


def test_connection_versions_are_immutable() -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="immutable-admin")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                binding = SecretBinding(
                    workspace_id=workspace_id,
                    provider="fixture",
                    external_reference="immutable-secret",
                    version="v1",
                    created_by_user_id=admin_id,
                )
                session.add(binding)
                await session.flush()
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Immutable",
                    endpoint_url="https://mcp.example/v1",
                    secret_binding_id=binding.id,
                    policy_version="v1",
                )
                version_id = connection.pending_version_id
                binding_id = binding.id
                assert version_id is not None

            async with transaction(factory) as session:
                unchanged = await session.get(ServerConnectionVersion, version_id)
                assert unchanged is not None
                unchanged.endpoint_url = unchanged.endpoint_url
                await session.flush()

            with pytest.raises(ValueError, match="immutable"):
                async with transaction(factory) as session:
                    version = await session.get(ServerConnectionVersion, version_id)
                    assert version is not None
                    version.endpoint_url = "https://attacker.example"
                    await session.flush()

            with pytest.raises(ValueError, match="immutable"):
                async with transaction(factory) as session:
                    stored_binding = await session.get(SecretBinding, binding_id)
                    assert stored_binding is not None
                    stored_binding.external_reference = "retargeted-secret"
                    await session.flush()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://mcp.example",
        "https://user@mcp.example",
        "https://mcp.example?token=x",
        "https://mcp.example#fragment",
        "https://127.0.0.1",
        "https://2130706433",
        "https://127.1",
        "https://0x7f000001",
        "https://①②⑦.⓪.⓪.①",
        "https://\uff11\uff12\uff17.\uff10.\uff10.\uff11",
        "https://\u2113ocalhost",
        "https://localhost\u3002",
        "https://%31%32%37.0.0.1",
        "https://%6cocalhost",
        "https://mcp example/path",
        "https://-mcp.example/path",
        "https://mcp-.example/path",
        "https://mcp.example/%ZZ",
        "https://mcp.example/sk_live_abcdefghijkl",
        "https://mcp.example/%73%6b_live_abcdefghijkl",
    ],
)
def test_connection_configuration_rejects_unsafe_endpoints(endpoint: str) -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject=str(uuid4()))
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                with pytest.raises(ValueError, match="endpoint"):
                    await ConnectionService(session).create(
                        context=context,
                        name="Unsafe",
                        endpoint_url=endpoint,
                        secret_binding_id=None,
                        policy_version="v1",
                    )

    asyncio.run(scenario())


def test_capability_versions_preserve_enabled_history_and_detect_drift() -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="capability-admin")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Capability source",
                    endpoint_url="https://mcp.example/tools",
                    secret_binding_id=None,
                    policy_version="v1",
                )
                connection_version_id = connection.pending_version_id
                assert connection_version_id is not None
                refresh_generation, control_epoch, _ = await ConnectionService(
                    session
                ).allocate_refresh_generation(context=context, connection_id=connection.id)
                service = CapabilityService(session)
                first = await service.record_version(
                    context=context,
                    connection_id=connection.id,
                    connection_version_id=connection_version_id,
                    expected_control_epoch=control_epoch,
                    expected_refresh_generation=refresh_generation,
                    tool_identity="tools/review",
                    tool_name="review",
                    display_name="Review",
                    description="Review code",
                    input_schema={"type": "object"},
                    output_schema=None,
                    metadata_digest="a" * 64,
                    protocol_revision="2025-06-18",
                )
                duplicate = await service.record_version(
                    context=context,
                    connection_id=connection.id,
                    connection_version_id=connection_version_id,
                    expected_control_epoch=control_epoch,
                    expected_refresh_generation=refresh_generation,
                    tool_identity="tools/review",
                    tool_name="review",
                    display_name="Review",
                    description="Review code",
                    input_schema={"type": "object"},
                    output_schema=None,
                    metadata_digest="a" * 64,
                    protocol_revision="2025-06-18",
                )
                assert duplicate.id == first.id
                capability = await service.enable(
                    context=context,
                    capability_id=first.capability_id,
                    expected_version_id=first.id,
                )
                assert capability.typed_status == CapabilityStatus.ENABLED
                assert capability.status_epoch == 2

                refresh_generation, control_epoch, _ = await ConnectionService(
                    session
                ).allocate_refresh_generation(context=context, connection_id=connection.id)
                stale_generation = refresh_generation
                refresh_generation, control_epoch, _ = await ConnectionService(
                    session
                ).allocate_refresh_generation(context=context, connection_id=connection.id)
                with pytest.raises(InvalidConnectionTransition):
                    await service.record_version(
                        context=context,
                        connection_id=connection.id,
                        connection_version_id=connection_version_id,
                        expected_control_epoch=control_epoch,
                        expected_refresh_generation=stale_generation,
                        tool_identity="tools/review",
                        tool_name="review",
                        display_name="Review",
                        description="Stale drift",
                        input_schema={"type": "object"},
                        output_schema=None,
                        metadata_digest="b" * 64,
                        protocol_revision="2025-06-18",
                    )
                drift = await service.record_version(
                    context=context,
                    connection_id=connection.id,
                    connection_version_id=connection_version_id,
                    expected_control_epoch=control_epoch,
                    expected_refresh_generation=refresh_generation,
                    tool_identity="tools/review",
                    tool_name="review",
                    display_name="Review",
                    description="Review code and tests",
                    input_schema={"type": "object"},
                    output_schema=None,
                    metadata_digest="b" * 64,
                    protocol_revision="2025-06-18",
                )
                assert drift.sequence == 2
                assert capability.pending_version_id == drift.id
                assert capability.enabled_version_id == first.id
                assert CapabilityStatus(capability.status) == CapabilityStatus.PENDING_REVIEW
                assert capability.status_epoch == 3
                await ConnectionService(session).append_version(
                    context=context,
                    connection_id=connection.id,
                    endpoint_url="https://mcp.example/tools-v2",
                    secret_binding_id=None,
                    policy_version="v2",
                )
                with pytest.raises(InvalidCapabilityTransition):
                    await service.enable(
                        context=context,
                        capability_id=capability.id,
                        expected_version_id=drift.id,
                    )

            async with factory() as session:
                versions = list(
                    (
                        await session.scalars(
                            select(CapabilityVersion)
                            .where(CapabilityVersion.capability_id == first.capability_id)
                            .order_by(CapabilityVersion.sequence)
                        )
                    ).all()
                )
                events = list(
                    (
                        await session.scalars(
                            select(CapabilityStatusEvent)
                            .where(CapabilityStatusEvent.capability_id == first.capability_id)
                            .order_by(CapabilityStatusEvent.status_epoch)
                        )
                    ).all()
                )
                bindings = list(
                    (
                        await session.scalars(
                            select(McpToolBinding).where(
                                McpToolBinding.workspace_id == workspace_id
                            )
                        )
                    ).all()
                )
                assert [version.sequence for version in versions] == [1, 2]
                assert [event.status_epoch for event in events] == [1, 2, 3]
                assert len(bindings) == 2

            with pytest.raises(ValueError, match="immutable"):
                async with transaction(factory) as session:
                    binding = await session.get(McpToolBinding, first.id)
                    assert binding is not None
                    binding.tool_name = "retargeted"
                    await session.flush()

            with pytest.raises(ValueError, match="immutable"):
                async with transaction(factory) as session:
                    event_row = await session.scalar(
                        select(CapabilityStatusEvent).where(
                            CapabilityStatusEvent.capability_id == first.capability_id
                        )
                    )
                    assert event_row is not None
                    event_row.status = CapabilityStatus.DISABLED.value
                    await session.flush()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "schema",
    [
        {"description": "x" * 8193},
        {"default": "sk_live_abcdefghijkl"},
        {"properties": {str(index): {} for index in range(1025)}},
    ],
)
def test_capability_metadata_rejects_unbounded_or_secret_schemas(
    schema: dict[str, object],
) -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject=str(uuid4()))
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Bounded discovery",
                    endpoint_url="https://mcp.example/tools",
                    secret_binding_id=None,
                    policy_version="v1",
                )
                connection_version_id = connection.pending_version_id
                assert connection_version_id is not None
                generation, control_epoch, _ = await ConnectionService(
                    session
                ).allocate_refresh_generation(context=context, connection_id=connection.id)
                with pytest.raises(ValueError, match="schema"):
                    await CapabilityService(session).record_version(
                        context=context,
                        connection_id=connection.id,
                        connection_version_id=connection_version_id,
                        expected_control_epoch=control_epoch,
                        expected_refresh_generation=generation,
                        tool_identity="tools/unbounded",
                        tool_name="unbounded",
                        display_name="Unbounded",
                        description=None,
                        input_schema=schema,
                        output_schema=None,
                        metadata_digest="f" * 64,
                        protocol_revision="2025-06-18",
                    )

    asyncio.run(scenario())


def test_capability_metadata_rejects_excessive_schema_depth() -> None:
    schema: dict[str, object] = {}
    for _ in range(33):
        schema = {"nested": schema}

    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject=str(uuid4()))
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Deep discovery",
                    endpoint_url="https://mcp.example/tools",
                    secret_binding_id=None,
                    policy_version="v1",
                )
                connection_version_id = connection.pending_version_id
                assert connection_version_id is not None
                generation, control_epoch, _ = await ConnectionService(
                    session
                ).allocate_refresh_generation(context=context, connection_id=connection.id)
                with pytest.raises(ValueError, match="structural limits"):
                    await CapabilityService(session).record_version(
                        context=context,
                        connection_id=connection.id,
                        connection_version_id=connection_version_id,
                        expected_control_epoch=control_epoch,
                        expected_refresh_generation=generation,
                        tool_identity="tools/deep",
                        tool_name="deep",
                        display_name="Deep",
                        description=None,
                        input_schema=schema,
                        output_schema=None,
                        metadata_digest="e" * 64,
                        protocol_revision="2025-06-18",
                    )

    asyncio.run(scenario())


def test_stale_verification_cannot_survive_disable_enable() -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="stale-verification")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Fenced",
                    endpoint_url="https://mcp.example/fenced",
                    secret_binding_id=None,
                    policy_version="v1",
                )
                version_id = connection.pending_version_id
                assert version_id is not None
                generation, stale_epoch, _ = await ConnectionService(
                    session
                ).allocate_refresh_generation(context=context, connection_id=connection.id)
                await ConnectionService(session).disable(
                    context=context, connection_id=connection.id
                )
                await ConnectionService(session).enable(
                    context=context, connection_id=connection.id
                )
                with pytest.raises(InvalidConnectionTransition):
                    await ConnectionService(session).promote_pending(
                        context=context,
                        connection_id=connection.id,
                        expected_version_id=version_id,
                        expected_control_epoch=stale_epoch,
                        expected_refresh_generation=generation,
                    )

    asyncio.run(scenario())


def test_capability_disable_enable_uses_monotonic_epoch() -> None:
    async def scenario() -> None:
        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="capability-lifecycle")
            async with transaction(factory) as session:
                context = await admin_context(session, user_id=admin_id, workspace_id=workspace_id)
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Capability lifecycle",
                    endpoint_url="https://mcp.example/tools",
                    secret_binding_id=None,
                    policy_version="v1",
                )
                version_id = connection.pending_version_id
                assert version_id is not None
                refresh_generation, control_epoch, _ = await ConnectionService(
                    session
                ).allocate_refresh_generation(context=context, connection_id=connection.id)
                service = CapabilityService(session)
                version = await service.record_version(
                    context=context,
                    connection_id=connection.id,
                    connection_version_id=version_id,
                    expected_control_epoch=control_epoch,
                    expected_refresh_generation=refresh_generation,
                    tool_identity="tools/run",
                    tool_name="run",
                    display_name="Run",
                    description=None,
                    input_schema={},
                    output_schema={},
                    metadata_digest="c" * 64,
                    protocol_revision="2025-06-18",
                )
                capability = await service.enable(
                    context=context,
                    capability_id=version.capability_id,
                    expected_version_id=version.id,
                )
                original_enabled_id = capability.enabled_version_id
                await service.disable(context=context, capability_id=capability.id)
                assert capability.status_epoch == 3
                with pytest.raises(InvalidCapabilityTransition):
                    await service.disable(context=context, capability_id=capability.id)
                await service.enable(
                    context=context,
                    capability_id=capability.id,
                    expected_version_id=version.id,
                )
                assert capability.enabled_version_id == original_enabled_id
                assert capability.status_epoch == 4

    asyncio.run(scenario())


def test_registry_services_reject_cross_workspace_ids() -> None:
    async def scenario() -> None:
        async with database() as factory:
            first_user, first_workspace = await bootstrap(factory, subject="scope-first")
            second_user, second_workspace = await bootstrap(factory, subject="scope-second")
            async with transaction(factory) as session:
                second_context = await admin_context(
                    session, user_id=second_user, workspace_id=second_workspace
                )
                connection = await ConnectionService(session).create(
                    context=second_context,
                    name="Private",
                    endpoint_url="https://private.example/mcp",
                    secret_binding_id=None,
                    policy_version="v1",
                )
                connection_id = connection.id
                connection_version_id = connection.pending_version_id
                assert connection_version_id is not None

            async with transaction(factory) as session:
                first_context = await admin_context(
                    session, user_id=first_user, workspace_id=first_workspace
                )
                with pytest.raises(AuthorizationDenied):
                    await ConnectionService(session).append_version(
                        context=first_context,
                        connection_id=connection_id,
                        endpoint_url="https://private.example/mcp-v2",
                        secret_binding_id=None,
                        policy_version="v2",
                    )
                with pytest.raises(AuthorizationDenied):
                    await CapabilityService(session).record_version(
                        context=first_context,
                        connection_id=connection_id,
                        connection_version_id=connection_version_id,
                        expected_control_epoch=0,
                        expected_refresh_generation=0,
                        tool_identity="tools/private",
                        tool_name="private",
                        display_name="Private",
                        description=None,
                        input_schema={},
                        output_schema=None,
                        metadata_digest="d" * 64,
                        protocol_revision="2025-06-18",
                    )

    asyncio.run(scenario())

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modall.audit.types import AuditAction, ResourceType
from modall.identity.repository import (
    AuthorizationDenied,
    AuthorizationService,
    WorkspaceRepository,
)
from modall.identity.service import IdentityService
from modall.identity.types import Permission, Principal, Role
from modall.persistence.database import create_engine, create_session_factory, transaction
from modall.persistence.models import AuditEvent, Base, SecretBinding, User, WorkspaceMembership
from modall.secrets.provider import SecretProviderError, SecretReference
from modall.secrets.service import SecretBindingService


@asynccontextmanager
async def database() -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
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
        yield engine, factory
    finally:
        await engine.dispose()


async def bootstrap_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    subject: str,
    name: str,
) -> tuple[UUID, UUID]:
    async with transaction(factory) as session:
        identity = IdentityService(session)
        user = await identity.resolve_user(
            Principal(issuer="https://issuer.example", subject=subject, display_name=subject)
        )
        workspace = await identity.create_workspace(owner=user, name=name)
        return user.id, workspace.id


def test_workspace_repository_isolation_authorization_and_atomic_audit() -> None:
    async def scenario() -> None:
        async with database() as (_, factory):
            user_one, workspace_one = await bootstrap_workspace(
                factory, subject="one", name="Workspace One"
            )
            user_two, workspace_two = await bootstrap_workspace(
                factory, subject="two", name="Workspace Two"
            )

            async with transaction(factory) as session:
                auth = AuthorizationService(session)
                context_one = await auth.authorize(
                    user_id=user_one,
                    workspace_id=workspace_one,
                    permission=Permission.MANAGE_CONNECTION_CONFIGURATION,
                )
                binding_one = await SecretBindingService(session).create_binding(
                    context=context_one,
                    reference=SecretReference("fixture", "server-one", "v1"),
                    correlation_id=uuid4(),
                )
                binding_one_id = binding_one.id

            async with transaction(factory) as session:
                context_two = await AuthorizationService(session).authorize(
                    user_id=user_two,
                    workspace_id=workspace_two,
                    permission=Permission.MANAGE_CONNECTION_CONFIGURATION,
                )
                binding_two = await SecretBindingService(session).create_binding(
                    context=context_two,
                    reference=SecretReference("fixture", "server-two", "v1"),
                )
                binding_two_id = binding_two.id

            async with transaction(factory) as session:
                context_one = await AuthorizationService(session).authorize(
                    user_id=user_one,
                    workspace_id=workspace_one,
                    permission=Permission.VIEW_RESOURCES,
                )
                repository = WorkspaceRepository(session, context_one)
                found = await repository.get_secret_binding(binding_one_id)
                assert found is not None
                assert found.id == binding_one_id
                assert await repository.get_secret_binding(binding_two_id) is None
                events = await repository.list_audit_events()
                assert {event.workspace_id for event in events} == {workspace_one}
                assert {event.action for event in events} == {
                    AuditAction.WORKSPACE_CREATED.value,
                    AuditAction.SECRET_BINDING_CREATED.value,
                }

            async with transaction(factory) as session:
                with pytest.raises(AuthorizationDenied):
                    await AuthorizationService(session).authorize(
                        user_id=user_one,
                        workspace_id=workspace_two,
                        permission=Permission.VIEW_RESOURCES,
                    )

    asyncio.run(scenario())


def test_current_membership_role_is_rechecked() -> None:
    async def scenario() -> None:
        async with database() as (_, factory):
            user_id, workspace_id = await bootstrap_workspace(
                factory, subject="viewer", name="Role Test"
            )
            async with transaction(factory) as session:
                membership = await session.get(
                    WorkspaceMembership,
                    {"workspace_id": workspace_id, "user_id": user_id},
                )
                assert membership is not None
                membership.role = Role.VIEWER.value

            async with transaction(factory) as session:
                with pytest.raises(AuthorizationDenied):
                    await AuthorizationService(session).authorize(
                        user_id=user_id,
                        workspace_id=workspace_id,
                        permission=Permission.VIEW_AUDIT,
                    )
                viewer_context = await AuthorizationService(session).authorize(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    permission=Permission.VIEW_RESOURCES,
                )
                with pytest.raises(AuthorizationDenied):
                    await WorkspaceRepository(session, viewer_context).list_audit_events()

    asyncio.run(scenario())


def test_domain_write_rolls_back_when_audit_evidence_fails() -> None:
    async def scenario() -> None:
        async with database() as (_, factory):
            user_id, workspace_id = await bootstrap_workspace(
                factory, subject="atomic", name="Atomic Test"
            )
            binding_id = uuid4()

            with pytest.raises(IntegrityError):
                async with transaction(factory) as session:
                    session.add(
                        SecretBinding(
                            id=binding_id,
                            workspace_id=workspace_id,
                            provider="fixture",
                            external_reference="atomic",
                            version="v1",
                            created_by_user_id=user_id,
                        )
                    )
                    await session.flush()
                    session.add(
                        AuditEvent(
                            workspace_id=workspace_id,
                            actor_user_id=user_id,
                            action=AuditAction.SECRET_BINDING_CREATED.value,
                            resource_type=ResourceType.SECRET_BINDING.value,
                            resource_id=binding_id,
                            outcome="not-allowlisted",
                            correlation_id=uuid4(),
                        )
                    )

            async with factory() as session:
                assert await session.get(SecretBinding, binding_id) is None

    asyncio.run(scenario())


def test_user_resolution_is_idempotent_and_workspace_name_is_required() -> None:
    async def scenario() -> None:
        async with database() as (_, factory):
            principal = Principal("issuer", "subject", "Name")
            async with transaction(factory) as session:
                service = IdentityService(session)
                first = await service.resolve_user(principal)
                second = await service.resolve_user(principal)
                assert first.id == second.id
                with pytest.raises(ValueError, match="between 1 and 128"):
                    await service.create_workspace(owner=first, name="   ")
                with pytest.raises(ValueError, match="between 1 and 128"):
                    await service.create_workspace(owner=first, name="x" * 129)

            async with factory() as session:
                assert await session.scalar(select(func.count()).select_from(User)) == 1

    asyncio.run(scenario())


def test_database_rejects_missing_actor_reference() -> None:
    async def scenario() -> None:
        async with database() as (_, factory):
            _, workspace_two = await bootstrap_workspace(factory, subject="db-two", name="DB Two")

            with pytest.raises(IntegrityError):
                async with transaction(factory) as session:
                    session.add(
                        SecretBinding(
                            workspace_id=workspace_two,
                            provider="fixture",
                            external_reference="missing-actor",
                            version="v1",
                            created_by_user_id=uuid4(),
                        )
                    )

    asyncio.run(scenario())


def test_membership_mutation_rejects_a_stale_admin_context() -> None:
    async def scenario() -> None:
        async with database() as (_, factory):
            original_admin_id, workspace_id = await bootstrap_workspace(
                factory, subject="original-admin", name="Stale Context Test"
            )
            async with transaction(factory) as session:
                replacement = await IdentityService(session).resolve_user(
                    Principal("issuer", "replacement-admin", "Replacement Admin")
                )
                replacement_id = replacement.id
                original_context = await AuthorizationService(session).authorize(
                    user_id=original_admin_id,
                    workspace_id=workspace_id,
                    permission=Permission.MANAGE_CONNECTION_CONFIGURATION,
                )
                await IdentityService(session).set_membership_role(
                    context=original_context,
                    user_id=replacement_id,
                    role=Role.ADMIN,
                )

            async with transaction(factory) as session:
                replacement_context = await AuthorizationService(session).authorize(
                    user_id=replacement_id,
                    workspace_id=workspace_id,
                    permission=Permission.MANAGE_CONNECTION_CONFIGURATION,
                )
                await IdentityService(session).set_membership_role(
                    context=replacement_context,
                    user_id=original_admin_id,
                    role=Role.VIEWER,
                )

            async with transaction(factory) as session:
                with pytest.raises(AuthorizationDenied):
                    await IdentityService(session).set_membership_role(
                        context=original_context,
                        user_id=original_admin_id,
                        role=Role.ADMIN,
                    )
                with pytest.raises(AuthorizationDenied):
                    await SecretBindingService(session).create_binding(
                        context=original_context,
                        reference=SecretReference("fixture", "stale", "v1"),
                    )

    asyncio.run(scenario())


def test_secret_binding_rejects_unusable_metadata_before_persistence() -> None:
    async def scenario() -> None:
        async with database() as (_, factory):
            admin_id, workspace_id = await bootstrap_workspace(
                factory, subject="binding-validation", name="Binding Validation"
            )
            async with transaction(factory) as session:
                context = await AuthorizationService(session).authorize(
                    user_id=admin_id,
                    workspace_id=workspace_id,
                    permission=Permission.MANAGE_CONNECTION_CONFIGURATION,
                )
                with pytest.raises(SecretProviderError, match="invalid"):
                    await SecretBindingService(session).create_binding(
                        context=context,
                        reference=SecretReference("mounted_file", "invalid/ref", "v1"),
                    )

            async with factory() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(SecretBinding)
                    .where(SecretBinding.workspace_id == workspace_id)
                )
                assert count == 0

    asyncio.run(scenario())


def test_admin_manages_roles_without_removing_last_admin() -> None:
    async def scenario() -> None:
        async with database() as (_, factory):
            admin_id, workspace_id = await bootstrap_workspace(
                factory, subject="admin", name="Membership Test"
            )
            async with transaction(factory) as session:
                member = await IdentityService(session).resolve_user(
                    Principal("issuer", "new-member", "New Member")
                )
                member_id = member.id

            async with transaction(factory) as session:
                admin_context = await AuthorizationService(session).authorize(
                    user_id=admin_id,
                    workspace_id=workspace_id,
                    permission=Permission.MANAGE_CONNECTION_CONFIGURATION,
                )
                membership = await IdentityService(session).set_membership_role(
                    context=admin_context,
                    user_id=member_id,
                    role=Role.OPERATOR,
                )
                assert membership.typed_role == Role.OPERATOR

            async with transaction(factory) as session:
                operator_context = await AuthorizationService(session).authorize(
                    user_id=member_id,
                    workspace_id=workspace_id,
                    permission=Permission.VIEW_AUDIT,
                )
                with pytest.raises(AuthorizationDenied):
                    await IdentityService(session).set_membership_role(
                        context=operator_context,
                        user_id=admin_id,
                        role=Role.VIEWER,
                    )

            async with transaction(factory) as session:
                admin_context = await AuthorizationService(session).authorize(
                    user_id=admin_id,
                    workspace_id=workspace_id,
                    permission=Permission.MANAGE_CONNECTION_CONFIGURATION,
                )
                with pytest.raises(AuthorizationDenied, match="retain an admin"):
                    await IdentityService(session).set_membership_role(
                        context=admin_context,
                        user_id=admin_id,
                        role=Role.VIEWER,
                    )

                await IdentityService(session).set_membership_role(
                    context=admin_context,
                    user_id=member_id,
                    role=Role.ADMIN,
                )
                demoted = await IdentityService(session).set_membership_role(
                    context=admin_context,
                    user_id=admin_id,
                    role=Role.VIEWER,
                )
                assert demoted.typed_role == Role.VIEWER

    asyncio.run(scenario())

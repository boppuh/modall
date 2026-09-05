import asyncio
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from modall.identity.repository import AuthorizationDenied, AuthorizationService
from modall.identity.service import IdentityService
from modall.identity.types import Permission, Principal, Role
from modall.persistence.database import (
    async_database_url,
    create_engine,
    create_session_factory,
    transaction,
)
from modall.persistence.models import ServerConnectionVersion, User, WorkspaceMembership
from modall.registry.service import ConnectionService

pytestmark = pytest.mark.skipif(
    "MODALL_DATABASE_URL" not in os.environ,
    reason="requires the migrated PostgreSQL integration database",
)


def test_concurrent_first_login_resolves_one_user() -> None:
    async def scenario() -> None:
        engine = create_engine(async_database_url(os.environ["MODALL_DATABASE_URL"]))
        factory = create_session_factory(engine)
        subject = f"concurrent-{uuid4()}"
        principal = Principal("https://issuer.example", subject, "Concurrent User")

        async def resolve() -> UUID:
            async with transaction(factory) as session:
                return (await IdentityService(session).resolve_user(principal)).id

        try:
            user_ids = await asyncio.gather(*(resolve() for _ in range(8)))
            assert len(set(user_ids)) == 1
            async with factory() as session:
                count = await session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.oidc_issuer == principal.issuer, User.oidc_subject == subject)
                )
                assert count == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_admin_demotions_preserve_an_admin() -> None:
    async def scenario() -> None:
        engine = create_engine(async_database_url(os.environ["MODALL_DATABASE_URL"]))
        factory = create_session_factory(engine)
        suffix = str(uuid4())
        try:
            async with transaction(factory) as session:
                identity = IdentityService(session)
                first = await identity.resolve_user(Principal("issuer", f"first-{suffix}", None))
                second = await identity.resolve_user(Principal("issuer", f"second-{suffix}", None))
                workspace = await identity.create_workspace(
                    owner=first, name=f"Concurrent {suffix}"
                )
                first_context = await AuthorizationService(session).authorize(
                    user_id=first.id,
                    workspace_id=workspace.id,
                    permission=Permission.MANAGE_CONNECTION_CONFIGURATION,
                )
                await identity.set_membership_role(
                    context=first_context,
                    user_id=second.id,
                    role=Role.ADMIN,
                )
                first_id, second_id, workspace_id = first.id, second.id, workspace.id

            both_ready = asyncio.Event()
            ready_count = 0
            ready_lock = asyncio.Lock()

            async def demote_self(user_id: UUID) -> bool:
                nonlocal ready_count
                async with transaction(factory) as session:
                    context = await AuthorizationService(session).authorize(
                        user_id=user_id,
                        workspace_id=workspace_id,
                        permission=Permission.MANAGE_CONNECTION_CONFIGURATION,
                    )
                    async with ready_lock:
                        ready_count += 1
                        if ready_count == 2:
                            both_ready.set()
                    await both_ready.wait()
                    try:
                        await IdentityService(session).set_membership_role(
                            context=context,
                            user_id=user_id,
                            role=Role.VIEWER,
                        )
                    except AuthorizationDenied:
                        return False
                    return True

            results = await asyncio.gather(demote_self(first_id), demote_self(second_id))
            assert sorted(results) == [False, True]
            async with factory() as session:
                admin_count = await session.scalar(
                    select(func.count())
                    .select_from(WorkspaceMembership)
                    .where(
                        WorkspaceMembership.workspace_id == workspace_id,
                        WorkspaceMembership.role == Role.ADMIN.value,
                    )
                )
                assert admin_count == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_connection_versions_have_unique_sequences() -> None:
    async def scenario() -> None:
        engine = create_engine(async_database_url(os.environ["MODALL_DATABASE_URL"]))
        factory = create_session_factory(engine)
        suffix = str(uuid4())
        try:
            async with transaction(factory) as session:
                identity = IdentityService(session)
                admin = await identity.resolve_user(Principal("issuer", f"registry-{suffix}", None))
                workspace = await identity.create_workspace(
                    owner=admin, name=f"Registry concurrency {suffix}"
                )
                context = await AuthorizationService(session).authorize(
                    user_id=admin.id,
                    workspace_id=workspace.id,
                    permission=Permission.MANAGE_CONNECTION_CONFIGURATION,
                )
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Concurrent versions",
                    endpoint_url="https://mcp.example/v1",
                    secret_binding_id=None,
                    policy_version="v1",
                )
                admin_id, workspace_id, connection_id = admin.id, workspace.id, connection.id

            both_ready = asyncio.Event()
            ready_count = 0
            ready_lock = asyncio.Lock()

            async def append(suffix: str) -> UUID:
                nonlocal ready_count
                async with transaction(factory) as session:
                    context = await AuthorizationService(session).authorize(
                        user_id=admin_id,
                        workspace_id=workspace_id,
                        permission=Permission.MANAGE_CONNECTION_CONFIGURATION,
                    )
                    async with ready_lock:
                        ready_count += 1
                        if ready_count == 2:
                            both_ready.set()
                    await both_ready.wait()
                    version = await ConnectionService(session).append_version(
                        context=context,
                        connection_id=connection_id,
                        endpoint_url=f"https://mcp.example/{suffix}",
                        secret_binding_id=None,
                        policy_version=suffix,
                    )
                    return version.id

            version_ids = await asyncio.gather(append("v2"), append("v3"))
            async with factory() as session:
                sequences = list(
                    (
                        await session.scalars(
                            select(ServerConnectionVersion.sequence)
                            .where(ServerConnectionVersion.connection_id == connection_id)
                            .order_by(ServerConnectionVersion.sequence)
                        )
                    ).all()
                )
                assert sequences == [1, 2, 3]
                assert len(set(version_ids)) == 2
        finally:
            await engine.dispose()

    asyncio.run(scenario())

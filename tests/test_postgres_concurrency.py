import asyncio
import os
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import DBAPIError

from modall.identity.repository import AuthorizationDenied, AuthorizationService
from modall.identity.service import IdentityService
from modall.identity.types import Permission, Principal, Role
from modall.persistence.database import (
    async_database_url,
    create_engine,
    create_session_factory,
    transaction,
)
from modall.persistence.models import (
    Capability,
    CapabilityStatusEvent,
    McpToolBinding,
    RegistryEntry,
    RegistrySearchCache,
    ServerConnectionVersion,
    User,
    WorkspaceMembership,
)
from modall.registry.official import OfficialRegistryAdapter, OfficialRegistryService
from modall.registry.service import CapabilityService, ConnectionService

pytestmark = pytest.mark.skipif(
    "MODALL_DATABASE_URL" not in os.environ,
    reason="requires the migrated PostgreSQL integration database",
)


def scanner_allows(value: object) -> bool:
    del value
    return False


def test_concurrent_registry_search_misses_share_one_cache_row() -> None:
    async def scenario() -> None:
        engine = create_engine(async_database_url(os.environ["MODALL_DATABASE_URL"]))
        factory = create_session_factory(engine)
        suffix = str(uuid4())
        request_count = 0
        both_requested = asyncio.Event()
        request_lock = asyncio.Lock()
        try:
            async with transaction(factory) as session:
                identity = IdentityService(session)
                operator = await identity.resolve_user(
                    Principal("issuer", f"registry-search-{suffix}", None)
                )
                workspace = await identity.create_workspace(
                    owner=operator, name=f"Registry search {suffix}"
                )
                user_id, workspace_id = operator.id, workspace.id

            async def handler(request: httpx.Request) -> httpx.Response:
                nonlocal request_count
                async with request_lock:
                    request_count += 1
                    if request_count == 2:
                        both_requested.set()
                await both_requested.wait()
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    json={"servers": [], "metadata": {"count": 0}},
                    request=request,
                )

            async def search() -> tuple[UUID, bool]:
                async with transaction(factory) as session:
                    context = await AuthorizationService(session).authorize(
                        user_id=user_id,
                        workspace_id=workspace_id,
                        permission=Permission.SEARCH_REGISTRY,
                    )
                    result = await OfficialRegistryService(
                        session,
                        OfficialRegistryAdapter(transport=httpx.MockTransport(handler)),
                    ).search(context=context, query="shared-query")
                    return result.cache_id, result.from_cache

            results = await asyncio.gather(search(), search())
            assert len({result[0] for result in results}) == 1
            assert sorted(result[1] for result in results) == [False, True]
            assert request_count == 2
            async with factory() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(RegistrySearchCache)
                        .where(RegistrySearchCache.workspace_id == workspace_id)
                    )
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_rejected_cache_reads_share_one_replacement() -> None:
    class BarrierAdapter(OfficialRegistryAdapter):
        def __init__(self, *, transport: httpx.AsyncBaseTransport) -> None:
            super().__init__(transport=transport)
            self._initial_reads = 0
            self._both_initial_reads = asyncio.Event()
            self._read_lock = asyncio.Lock()

        async def parse_cached_items(self, values: object):  # type: ignore[no-untyped-def]
            async with self._read_lock:
                self._initial_reads += 1
                if self._initial_reads == 2:
                    self._both_initial_reads.set()
                wait_for_peer = self._initial_reads <= 2
            if wait_for_peer:
                await self._both_initial_reads.wait()
            return await super().parse_cached_items(values)

    async def scenario() -> None:
        engine = create_engine(async_database_url(os.environ["MODALL_DATABASE_URL"]))
        factory = create_session_factory(engine)
        suffix = str(uuid4())
        request_count = 0
        unsafe_payload = {
            "servers": [
                {
                    "server": {
                        "name": "com.example/unsafe",
                        "version": "1.0.0",
                        "description": "token%2525253DAbCdEfGhIjKlMnOpQrStUvWx",
                    }
                }
            ],
            "metadata": {"count": 1},
        }

        async def seed_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json=unsafe_payload,
                request=request,
            )

        async def strict_handler(request: httpx.Request) -> httpx.Response:
            nonlocal request_count
            request_count += 1
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"servers": [], "metadata": {"count": 0}},
                request=request,
            )

        try:
            async with transaction(factory) as session:
                identity = IdentityService(session)
                operator = await identity.resolve_user(
                    Principal("issuer", f"registry-rejection-{suffix}", None)
                )
                workspace = await identity.create_workspace(
                    owner=operator, name=f"Registry rejection {suffix}"
                )
                context = await AuthorizationService(session).authorize(
                    user_id=operator.id,
                    workspace_id=workspace.id,
                    permission=Permission.SEARCH_REGISTRY,
                )
                seeded = await OfficialRegistryService(
                    session,
                    OfficialRegistryAdapter(
                        transport=httpx.MockTransport(seed_handler),
                        query_scanner=scanner_allows,
                        metadata_scanner=scanner_allows,
                    ),
                ).search(context=context, query="shared-rejected-query")
                user_id, workspace_id = operator.id, workspace.id
                assert seeded.from_cache is False

            adapter = BarrierAdapter(transport=httpx.MockTransport(strict_handler))

            async def search() -> tuple[UUID, bool]:
                async with transaction(factory) as session:
                    context = await AuthorizationService(session).authorize(
                        user_id=user_id,
                        workspace_id=workspace_id,
                        permission=Permission.SEARCH_REGISTRY,
                    )
                    result = await OfficialRegistryService(session, adapter).search(
                        context=context, query="shared-rejected-query"
                    )
                    return result.cache_id, result.from_cache

            results = await asyncio.gather(search(), search())
            assert len({result[0] for result in results}) == 1
            assert sorted(result[1] for result in results) == [False, True]
            assert request_count == 1
            async with factory() as session:
                assert (
                    await session.scalar(
                        select(func.count())
                        .select_from(RegistrySearchCache)
                        .where(RegistrySearchCache.workspace_id == workspace_id)
                    )
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


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


def test_database_guards_stable_capability_identity_and_exact_binding() -> None:
    async def scenario() -> None:
        engine = create_engine(async_database_url(os.environ["MODALL_DATABASE_URL"]))
        factory = create_session_factory(engine)
        suffix = str(uuid4())
        try:
            async with transaction(factory) as session:
                identity = IdentityService(session)
                admin = await identity.resolve_user(Principal("issuer", f"guard-{suffix}", None))
                workspace = await identity.create_workspace(
                    owner=admin, name=f"Registry guards {suffix}"
                )
                context = await AuthorizationService(session).authorize(
                    user_id=admin.id,
                    workspace_id=workspace.id,
                    permission=Permission.MANAGE_CONNECTION_CONFIGURATION,
                )
                connection = await ConnectionService(session).create(
                    context=context,
                    name="Guarded",
                    endpoint_url="https://mcp.example/tools",
                    secret_binding_id=None,
                    policy_version="v1",
                )
                connection_version_id = connection.pending_version_id
                assert connection_version_id is not None
                generation, control_epoch, _ = await ConnectionService(
                    session
                ).allocate_refresh_generation(context=context, connection_id=connection.id)
                version = await CapabilityService(session).record_version(
                    context=context,
                    connection_id=connection.id,
                    connection_version_id=connection_version_id,
                    expected_control_epoch=control_epoch,
                    expected_refresh_generation=generation,
                    tool_identity="tools/guarded",
                    tool_name="guarded",
                    display_name="Guarded",
                    description=None,
                    input_schema={},
                    output_schema=None,
                    metadata_digest="a" * 64,
                    protocol_revision="2025-06-18",
                )
                capability_id, version_id = version.capability_id, version.id
                entry = RegistryEntry(
                    workspace_id=workspace.id,
                    source="official",
                    external_id=f"io.example/{suffix}",
                    current_version_id=None,
                )
                session.add(entry)
                await session.flush()
                entry_id, workspace_id = entry.id, workspace.id

            with pytest.raises(DBAPIError):
                async with transaction(factory) as session:
                    await session.execute(
                        delete(McpToolBinding).where(
                            McpToolBinding.capability_version_id == version_id
                        )
                    )

            with pytest.raises(DBAPIError):
                async with transaction(factory) as session:
                    await session.execute(
                        update(Capability)
                        .where(Capability.id == capability_id)
                        .values(tool_identity="tools/retargeted")
                    )

            with pytest.raises(DBAPIError):
                async with transaction(factory) as session:
                    await session.execute(
                        delete(CapabilityStatusEvent).where(
                            CapabilityStatusEvent.capability_id == capability_id
                        )
                    )

            with pytest.raises(DBAPIError):
                async with transaction(factory) as session:
                    await session.execute(
                        update(RegistryEntry)
                        .where(RegistryEntry.id == entry_id)
                        .values(external_id="io.example/retargeted")
                    )

            with pytest.raises(DBAPIError):
                async with transaction(factory) as session:
                    await session.execute(
                        insert(RegistryEntry).values(
                            workspace_id=workspace_id,
                            source="official",
                            external_id="\t\n",
                            current_version_id=None,
                        )
                    )

            async with transaction(factory) as session:
                await session.execute(delete(Capability).where(Capability.id == capability_id))

            async with factory() as session:
                assert await session.get(Capability, capability_id) is None
                assert await session.get(McpToolBinding, version_id) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())

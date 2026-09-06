import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from modall.audit.types import AuditAction
from modall.identity.repository import AuthorizationDenied, AuthorizationService
from modall.identity.service import IdentityService
from modall.identity.types import Permission, Principal, Role, WorkspaceContext
from modall.persistence.database import create_engine, create_session_factory, transaction
from modall.persistence.models import (
    AuditEvent,
    Base,
    RegistryEntry,
    RegistryEntryVersion,
    RegistrySearchCache,
    ServerConnection,
)
from modall.registry.official import (
    OFFICIAL_REGISTRY_SERVERS_URL,
    OfficialRegistryAdapter,
    OfficialRegistryError,
    OfficialRegistryFailureCode,
    OfficialRegistryLimits,
    OfficialRegistryService,
    purge_expired_registry_cache,
)

FIXTURES = Path(__file__).parent / "fixtures" / "registry"


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
        permission=Permission.SEARCH_REGISTRY,
    )


def fixture_response(name: str, request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "application/json"},
        content=(FIXTURES / name).read_bytes(),
        request=request,
    )


def test_official_adapter_uses_recorded_contract_and_bounded_pagination() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert str(request.url).startswith(OFFICIAL_REGISTRY_SERVERS_URL)
            assert request.url.params["search"] == "fixture"
            assert request.url.params["version"] == "latest"
            if len(requests) == 1:
                assert "cursor" not in request.url.params
                return fixture_response("search_page_1.json", request)
            assert request.url.params["cursor"] == "opaque-page-2"
            return fixture_response("search_page_2.json", request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            items = await OfficialRegistryAdapter(client).search("fixture")
        assert [item.external_id for item in items] == [
            "io.modall.fixture/weather",
            "io.modall.fixture/status",
        ]
        assert items[0].advertised_urls == ("https://weather.example/mcp",)
        assert len(items[0].provenance_digest) == 64
        assert len(requests) == 2

    asyncio.run(scenario())


def test_search_cache_and_import_preserve_provenance_without_connection_trust() -> None:
    async def scenario() -> None:
        calls = 0
        clock = datetime(2026, 9, 6, tzinfo=UTC)

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if "cursor" in request.url.params:
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    json={"servers": [], "metadata": {"count": 0}},
                    request=request,
                )
            return fixture_response("official_remote_sample.json", request)

        async with database() as factory:
            admin_id, workspace_id = await bootstrap(factory, subject="registry-admin")
            async with transaction(factory) as session:
                identity = IdentityService(session)
                operator = await identity.resolve_user(
                    Principal("issuer", "registry-operator", "Registry Operator")
                )
                admin = await context_for(session, user_id=admin_id, workspace_id=workspace_id)
                await identity.set_membership_role(
                    context=admin, user_id=operator.id, role=Role.OPERATOR
                )
                user_id = operator.id
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                adapter = OfficialRegistryAdapter(client)
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    service = OfficialRegistryService(session, adapter, now=lambda: clock)
                    searched = await service.search(context=context, query="inference")
                    cached = await service.search(context=context, query=" inference ")
                    assert searched.from_cache is False
                    assert cached.from_cache is True
                    assert cached.cache_id == searched.cache_id
                    assert calls == 2
                    item = searched.items[0]
                    imported = await service.import_cached(
                        context=context,
                        cache_id=searched.cache_id,
                        provenance_digest=item.provenance_digest,
                    )
                    replayed = await service.import_cached(
                        context=context,
                        cache_id=searched.cache_id,
                        provenance_digest=item.provenance_digest,
                    )
                    assert replayed.id == imported.id
                    assert imported.source_version == "1.0.0"
                    assert imported.source_uri == OFFICIAL_REGISTRY_SERVERS_URL
                    assert imported.imported_by_user_id == user_id
                    assert imported.normalized_metadata == item.normalized_metadata

                async with transaction(factory) as session:
                    assert (
                        await session.scalar(select(func.count()).select_from(ServerConnection))
                        == 0
                    )
                    assert (
                        await session.scalar(select(func.count()).select_from(RegistryEntry)) == 1
                    )
                    assert (
                        await session.scalar(select(func.count()).select_from(RegistryEntryVersion))
                        == 1
                    )
                    events = (
                        await session.scalars(
                            select(AuditEvent).where(
                                AuditEvent.action == AuditAction.REGISTRY_ENTRY_IMPORTED.value
                            )
                        )
                    ).all()
                    assert len(events) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("query", "expected_code"),
    [
        ("token=AbCdEfGhIjKlMnOpQrStUvWx", OfficialRegistryFailureCode.UNSAFE_QUERY),
        ("\x00weather", OfficialRegistryFailureCode.INVALID_QUERY),
        ("x" * 257, OfficialRegistryFailureCode.INVALID_QUERY),
    ],
)
def test_unsafe_query_sends_and_persists_nothing(
    query: str, expected_code: OfficialRegistryFailureCode
) -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return fixture_response("search_page_1.json", request)

        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="unsafe-query")
            async with (
                httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
                transaction(factory) as session,
            ):
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                service = OfficialRegistryService(session, OfficialRegistryAdapter(client))
                with pytest.raises(OfficialRegistryError) as raised:
                    await service.search(context=context, query=query)
                assert raised.value.code == expected_code
                assert (
                    await session.scalar(select(func.count()).select_from(RegistrySearchCache)) == 0
                )
        assert calls == 0

    asyncio.run(scenario())


def test_scanner_failure_and_unsafe_metadata_write_no_cache() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            payload = json.loads((FIXTURES / "search_page_1.json").read_text())
            payload["servers"][0]["server"]["description"] = "token=AbCdEfGhIjKlMnOpQrStUvWx"
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json=payload,
                request=request,
            )

        def failed_scanner(value: object) -> bool:
            del value
            raise TimeoutError

        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="scanner")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    service = OfficialRegistryService(
                        session,
                        OfficialRegistryAdapter(client),
                        query_scanner=failed_scanner,
                    )
                    with pytest.raises(OfficialRegistryError) as failed:
                        await service.search(context=context, query="weather")
                    assert failed.value.code == OfficialRegistryFailureCode.SCANNER_FAILED
                assert calls == 0

                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    service = OfficialRegistryService(session, OfficialRegistryAdapter(client))
                    with pytest.raises(OfficialRegistryError) as unsafe:
                        await service.search(context=context, query="weather")
                    assert unsafe.value.code == OfficialRegistryFailureCode.UNSAFE_METADATA
                    assert (
                        await session.scalar(select(func.count()).select_from(RegistrySearchCache))
                        == 0
                    )
                assert calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("response", "limits", "expected_code"),
    [
        (
            lambda request: httpx.Response(
                503,
                headers={"Content-Type": "application/json"},
                content=(FIXTURES / "upstream_error.json").read_bytes(),
                request=request,
            ),
            OfficialRegistryLimits(),
            OfficialRegistryFailureCode.UPSTREAM_UNAVAILABLE,
        ),
        (
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b'{"servers":',
                request=request,
            ),
            OfficialRegistryLimits(),
            OfficialRegistryFailureCode.INVALID_RESPONSE,
        ),
        (
            lambda request: fixture_response("search_page_1.json", request),
            OfficialRegistryLimits(max_response_bytes=32),
            OfficialRegistryFailureCode.RESPONSE_LIMIT,
        ),
    ],
)
def test_upstream_outage_invalid_payload_and_limits_are_isolated(
    response: object,
    limits: OfficialRegistryLimits,
    expected_code: OfficialRegistryFailureCode,
) -> None:
    async def scenario() -> None:
        responder = response
        assert callable(responder)
        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
            with pytest.raises(OfficialRegistryError) as raised:
                await OfficialRegistryAdapter(client, limits=limits).search("weather")
        assert raised.value.code == expected_code

    asyncio.run(scenario())


def test_cache_is_workspace_scoped_expires_within_one_hour_and_roles_are_current() -> None:
    async def scenario() -> None:
        calls = 0
        now = datetime(2026, 9, 6, tzinfo=UTC)

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if "cursor" in request.url.params:
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    json={"servers": [], "metadata": {"count": 0}},
                    request=request,
                )
            return fixture_response("official_remote_sample.json", request)

        async with database() as factory:
            user_a, workspace_a = await bootstrap(factory, subject="workspace-a")
            user_b, workspace_b = await bootstrap(factory, subject="workspace-b")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                adapter = OfficialRegistryAdapter(client)
                async with transaction(factory) as session:
                    context_a = await context_for(session, user_id=user_a, workspace_id=workspace_a)
                    result_a = await OfficialRegistryService(
                        session, adapter, now=lambda: now
                    ).search(context=context_a, query="inference")
                    assert result_a.expires_at - result_a.fetched_at <= timedelta(hours=1)

                async with transaction(factory) as session:
                    context_b = await context_for(session, user_id=user_b, workspace_id=workspace_b)
                    service_b = OfficialRegistryService(session, adapter, now=lambda: now)
                    await service_b.search(context=context_b, query="inference")
                    with pytest.raises(OfficialRegistryError) as missing:
                        await service_b.import_cached(
                            context=context_b,
                            cache_id=result_a.cache_id,
                            provenance_digest=result_a.items[0].provenance_digest,
                        )
                    assert missing.value.code == OfficialRegistryFailureCode.CACHE_MISS

                now += timedelta(hours=1, microseconds=1)
                async with transaction(factory) as session:
                    context_a = await context_for(session, user_id=user_a, workspace_id=workspace_a)
                    refreshed = await OfficialRegistryService(
                        session, adapter, now=lambda: now
                    ).search(context=context_a, query="inference")
                    assert refreshed.cache_id != result_a.cache_id
                    assert await session.get(RegistrySearchCache, result_a.cache_id) is None
                    await purge_expired_registry_cache(session, now=now)
                    assert (
                        await session.scalar(select(func.count()).select_from(RegistrySearchCache))
                        == 1
                    )
                assert calls == 6

                async with transaction(factory) as session:
                    viewer_context = WorkspaceContext(workspace_a, user_a, Role.VIEWER)
                    with pytest.raises(AuthorizationDenied):
                        await OfficialRegistryService(session, adapter).search(
                            context=viewer_context, query="inference"
                        )
                assert calls == 6

    asyncio.run(scenario())


def test_limit_configuration_rejects_nonpositive_and_overlong_cache_ttl() -> None:
    with pytest.raises(ValueError):
        OfficialRegistryLimits(max_pages=0)
    with pytest.raises(ValueError):
        OfficialRegistryLimits(cache_ttl=timedelta(hours=1, microseconds=1))


@pytest.mark.parametrize(
    ("responder", "expected_code"),
    [
        (
            lambda request: httpx.Response(
                302, headers={"Location": "https://example.test"}, request=request
            ),
            OfficialRegistryFailureCode.UPSTREAM_UNAVAILABLE,
        ),
        (
            lambda request: httpx.Response(
                200, headers={"Content-Type": "text/html"}, content=b"{}", request=request
            ),
            OfficialRegistryFailureCode.INVALID_RESPONSE,
        ),
        (
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
                content=b"{}",
                request=request,
            ),
            OfficialRegistryFailureCode.INVALID_RESPONSE,
        ),
        (
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/json", "Content-Length": "invalid"},
                content=b"{}",
                request=request,
            ),
            OfficialRegistryFailureCode.INVALID_RESPONSE,
        ),
        (
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b'{"servers":[],"metadata":{"count":0,"count":0}}',
                request=request,
            ),
            OfficialRegistryFailureCode.INVALID_RESPONSE,
        ),
        (
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b'{"servers":[],"metadata":{"count":NaN}}',
                request=request,
            ),
            OfficialRegistryFailureCode.INVALID_RESPONSE,
        ),
    ],
)
def test_adapter_rejects_redirect_header_and_json_envelope_faults(
    responder: Callable[[httpx.Request], httpx.Response],
    expected_code: OfficialRegistryFailureCode,
) -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
            with pytest.raises(OfficialRegistryError) as raised:
                await OfficialRegistryAdapter(client).search("weather")
        assert raised.value.code == expected_code

    asyncio.run(scenario())


def test_adapter_bounds_streams_timeouts_transport_errors_and_direct_queries() -> None:
    class ChunkedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"x" * 20
            yield b"x" * 20

    async def scenario() -> None:
        calls = 0

        async def counted(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return fixture_response("search_page_1.json", request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(counted)) as client:
            with pytest.raises(OfficialRegistryError) as unsafe:
                await OfficialRegistryAdapter(client).search("token=AbCdEfGhIjKlMnOpQrStUvWx")
            assert unsafe.value.code == OfficialRegistryFailureCode.UNSAFE_QUERY
        assert calls == 0

        async def streamed(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                stream=ChunkedStream(),
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(streamed)) as client:
            with pytest.raises(OfficialRegistryError) as limited:
                await OfficialRegistryAdapter(
                    client, limits=OfficialRegistryLimits(max_response_bytes=32)
                ).search("weather")
            assert limited.value.code == OfficialRegistryFailureCode.RESPONSE_LIMIT

        async def hanging(request: httpx.Request) -> httpx.Response:
            del request
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async with httpx.AsyncClient(transport=httpx.MockTransport(hanging)) as client:
            with pytest.raises(OfficialRegistryError) as timed_out:
                await OfficialRegistryAdapter(
                    client, limits=OfficialRegistryLimits(total_timeout_seconds=0.01)
                ).search("weather")
            assert timed_out.value.code == OfficialRegistryFailureCode.TIMEOUT

        async def disconnected(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("fixture disconnect", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(disconnected)) as client:
            with pytest.raises(OfficialRegistryError) as unavailable:
                await OfficialRegistryAdapter(client).search("weather")
            assert unavailable.value.code == OfficialRegistryFailureCode.UPSTREAM_UNAVAILABLE

    asyncio.run(scenario())


def test_metadata_scanner_failure_is_payload_free() -> None:
    def failed_scanner(value: object) -> bool:
        del value
        raise RuntimeError("scanner internals must not escape")

    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return fixture_response("search_page_1.json", request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OfficialRegistryError) as raised:
                await OfficialRegistryAdapter(client, metadata_scanner=failed_scanner).search(
                    "weather"
                )
        assert raised.value.code == OfficialRegistryFailureCode.SCANNER_FAILED
        assert "scanner internals" not in str(raised.value)

    asyncio.run(scenario())


def test_changed_official_metadata_appends_an_immutable_entry_version() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads((FIXTURES / "search_page_1.json").read_text())
            server = payload["servers"][0]["server"]
            if request.url.params["search"] == "weather-v2":
                server["version"] = "1.3.0"
                server["description"] = "Read public weather forecasts"
            payload["metadata"].pop("nextCursor", None)
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json=payload,
                request=request,
            )

        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="registry-versioning")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                adapter = OfficialRegistryAdapter(client)
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    service = OfficialRegistryService(session, adapter)
                    first_search = await service.search(context=context, query="weather-v1")
                    first = await service.import_cached(
                        context=context,
                        cache_id=first_search.cache_id,
                        provenance_digest=first_search.items[0].provenance_digest,
                    )
                    second_search = await service.search(context=context, query="weather-v2")
                    second = await service.import_cached(
                        context=context,
                        cache_id=second_search.cache_id,
                        provenance_digest=second_search.items[0].provenance_digest,
                    )
                    replayed_first = await service.import_cached(
                        context=context,
                        cache_id=first_search.cache_id,
                        provenance_digest=first_search.items[0].provenance_digest,
                    )
                    assert first.registry_entry_id == second.registry_entry_id
                    assert (first.sequence, second.sequence) == (1, 2)
                    assert replayed_first.id == first.id
                    assert (first.source_version, second.source_version) == ("1.2.0", "1.3.0")
                    entry = await session.get(RegistryEntry, first.registry_entry_id)
                    assert entry is not None
                    assert entry.current_version_id == second.id
                    assert first.description == "Read public weather observations"
                    assert (
                        await session.scalar(
                            select(func.count())
                            .select_from(RegistryEntryVersion)
                            .where(
                                RegistryEntryVersion.registry_entry_id == first.registry_entry_id
                            )
                        )
                        == 2
                    )

    asyncio.run(scenario())


def test_registry_request_strips_ambient_credentials_and_suppresses_query_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert "Authorization" not in request.headers
            assert "Cookie" not in request.headers
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"servers": [], "metadata": {"count": 0}},
                request=request,
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer ambient-secret"},
            cookies={"session": "ambient-cookie"},
        ) as client:
            with caplog.at_level(logging.INFO, logger="httpx"):
                await OfficialRegistryAdapter(client).search("private operator search")

    asyncio.run(scenario())
    assert "private operator search" not in caplog.text
    assert "ambient-secret" not in caplog.text


def test_scanning_runs_off_loop_under_the_configured_deadline() -> None:
    def blocked_scanner(value: object) -> bool:
        del value
        time.sleep(0.1)
        return False

    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return fixture_response("search_page_1.json", request)

        limits = OfficialRegistryLimits(total_timeout_seconds=0.01)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OfficialRegistryAdapter(
                client,
                limits=limits,
                query_scanner=lambda value: False,
                metadata_scanner=blocked_scanner,
            )
            with pytest.raises(OfficialRegistryError) as raised:
                await adapter.search("weather")
        assert raised.value.code == OfficialRegistryFailureCode.TIMEOUT

    asyncio.run(scenario())


def test_service_and_adapter_cannot_diverge_on_limits() -> None:
    async def scenario() -> None:
        async with (
            database() as factory,
            httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request))
            ) as client,
            transaction(factory) as session,
        ):
            adapter = OfficialRegistryAdapter(client, limits=OfficialRegistryLimits(max_items=2))
            with pytest.raises(ValueError, match="one official Registry limits policy"):
                OfficialRegistryService(
                    session,
                    adapter,
                    limits=OfficialRegistryLimits(max_items=1),
                )

    asyncio.run(scenario())

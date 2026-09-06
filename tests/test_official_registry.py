import asyncio
import gc
import json
import logging
import multiprocessing
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID
from weakref import ReferenceType, ref

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import StatementError
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
from modall.registry import official as official_registry
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


def scanner_allows(value: object) -> bool:
    del value
    return False


def scanner_times_out(value: object) -> bool:
    del value
    raise TimeoutError


def scanner_fails_with_sensitive_detail(value: object) -> bool:
    del value
    raise RuntimeError("scanner internals must not escape")


def scanner_blocks_briefly(value: object) -> bool:
    del value
    time.sleep(0.1)
    return False


def scanner_hangs(value: object) -> bool:
    del value
    while True:
        time.sleep(1)


class ReusableTrackingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.calls = 0
        self.closes = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        assert self.closes == 0
        self.calls += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={"servers": [], "metadata": {"count": 0}},
            request=request,
        )

    async def aclose(self) -> None:
        self.closes += 1


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
            items = await OfficialRegistryAdapter(transport=client._transport).search("fixture")
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
                adapter = OfficialRegistryAdapter(transport=client._transport)
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    service = OfficialRegistryService(session, adapter, now=lambda: clock)
                    searched = await service.search(context=context, query="inference")
                    cached = await service.search(context=context, query=" inference ")
                    assert searched.from_cache is False
                    assert cached.from_cache is True
                    assert cached.cache_id == searched.cache_id
                    assert calls == 2

                    strict_service = OfficialRegistryService(
                        session,
                        OfficialRegistryAdapter(
                            transport=client._transport, query_scanner=scanner_times_out
                        ),
                        now=lambda: clock,
                    )
                    with pytest.raises(OfficialRegistryError) as blocked_cache_hit:
                        await strict_service.search(context=context, query="inference")
                    assert (
                        blocked_cache_hit.value.code == OfficialRegistryFailureCode.SCANNER_FAILED
                    )
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
        (
            "token%2525253DAbCdEfGhIjKlMnOpQrStUvWx",
            OfficialRegistryFailureCode.UNSAFE_QUERY,
        ),
        (
            "token%" + ("25" * 30) + "3DAbCdEfGhIjKlMnOpQrStUvWx",
            OfficialRegistryFailureCode.SCANNER_FAILED,
        ),
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
                service = OfficialRegistryService(
                    session, OfficialRegistryAdapter(transport=client._transport)
                )
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
            payload["servers"][0]["server"]["description"] = (
                "token%2525253DAbCdEfGhIjKlMnOpQrStUvWx"
            )
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json=payload,
                request=request,
            )

        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="scanner")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    service = OfficialRegistryService(
                        session,
                        OfficialRegistryAdapter(
                            transport=client._transport, query_scanner=scanner_times_out
                        ),
                    )
                    with pytest.raises(OfficialRegistryError) as failed:
                        await service.search(context=context, query="weather")
                    assert failed.value.code == OfficialRegistryFailureCode.SCANNER_FAILED
                assert calls == 0

                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    service = OfficialRegistryService(
                        session, OfficialRegistryAdapter(transport=client._transport)
                    )
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
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b'{"servers":[],"metadata":{"count":0},"secret":"\xff"}',
                request=request,
            ),
            OfficialRegistryLimits(),
            OfficialRegistryFailureCode.INVALID_RESPONSE,
        ),
        (
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b'{"servers":[],"metadata":{"count":0,"nextCursor":"\\ud800"}}',
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
                await OfficialRegistryAdapter(transport=client._transport, limits=limits).search(
                    "weather"
                )
        assert raised.value.code == expected_code
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None

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
                adapter = OfficialRegistryAdapter(transport=client._transport)
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
    with pytest.raises(ValueError):
        OfficialRegistryLimits(max_workspace_cache_rows=0)
    with pytest.raises(ValueError):
        OfficialRegistryLimits(max_response_bytes=1024, max_workspace_cache_bytes=512)
    for timeout in (float("inf"), float("nan")):
        with pytest.raises(ValueError):
            OfficialRegistryLimits(total_timeout_seconds=timeout)


def test_adapter_rejects_non_picklable_scanners_at_construction() -> None:
    scanner = lambda value: bool(value)  # noqa: E731
    with pytest.raises(ValueError, match="importable and picklable"):
        OfficialRegistryAdapter(query_scanner=scanner)
    with pytest.raises(ValueError, match="importable and picklable"):
        OfficialRegistryAdapter(metadata_scanner=scanner)


def test_global_cache_cleanup_deletes_bounded_batches() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 6, tzinfo=UTC)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"servers": [], "metadata": {"count": 0}},
                request=request,
            )

        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="bounded-cleanup")
            adapter = OfficialRegistryAdapter(transport=httpx.MockTransport(handler))
            for query in ("first", "second", "third"):
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    await OfficialRegistryService(session, adapter, now=lambda: now).search(
                        context=context, query=query
                    )

            async with transaction(factory) as session:
                await purge_expired_registry_cache(
                    session, now=now + timedelta(hours=1), batch_size=2
                )
                assert (
                    await session.scalar(select(func.count()).select_from(RegistrySearchCache)) == 1
                )
            async with transaction(factory) as session:
                await purge_expired_registry_cache(
                    session, now=now + timedelta(hours=1), batch_size=2
                )
                assert (
                    await session.scalar(select(func.count()).select_from(RegistrySearchCache)) == 0
                )
                with pytest.raises(ValueError, match="batch size"):
                    await purge_expired_registry_cache(session, batch_size=0)

    asyncio.run(scenario())


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
        (
            lambda request: httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=b'{"servers":[],"metadata":{"count":0,"weight":1e1000000}}',
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
                await OfficialRegistryAdapter(transport=client._transport).search("weather")
        assert raised.value.code == expected_code
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None

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
                await OfficialRegistryAdapter(transport=client._transport).search(
                    "token=AbCdEfGhIjKlMnOpQrStUvWx"
                )
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
                    transport=client._transport,
                    limits=OfficialRegistryLimits(max_response_bytes=32),
                ).search("weather")
            assert limited.value.code == OfficialRegistryFailureCode.RESPONSE_LIMIT

        async def hanging(request: httpx.Request) -> httpx.Response:
            del request
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async with httpx.AsyncClient(transport=httpx.MockTransport(hanging)) as client:
            with pytest.raises(OfficialRegistryError) as timed_out:
                await OfficialRegistryAdapter(
                    transport=client._transport,
                    limits=OfficialRegistryLimits(total_timeout_seconds=0.01),
                ).search("weather")
            assert timed_out.value.code == OfficialRegistryFailureCode.TIMEOUT
            assert timed_out.value.__cause__ is None
            assert timed_out.value.__context__ is None

        async def disconnected(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("fixture disconnect", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(disconnected)) as client:
            with pytest.raises(OfficialRegistryError) as unavailable:
                await OfficialRegistryAdapter(transport=client._transport).search("weather")
            assert unavailable.value.code == OfficialRegistryFailureCode.UPSTREAM_UNAVAILABLE
            assert unavailable.value.__cause__ is None
            assert unavailable.value.__context__ is None

        async def client_timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("fixture timeout", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(client_timeout)) as client:
            with pytest.raises(OfficialRegistryError) as timed_out_by_client:
                await OfficialRegistryAdapter(transport=client._transport).search("weather")
            assert timed_out_by_client.value.code == OfficialRegistryFailureCode.TIMEOUT
            assert timed_out_by_client.value.__cause__ is None
            assert timed_out_by_client.value.__context__ is None

    asyncio.run(scenario())


def test_canonicalization_failure_detaches_payload_bearing_exception() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=(
                    b'{"servers":[{"server":{"name":"io.modall.fixture.surrogate",'
                    b'"version":"1.0.0","annotations":{"value":"\\ud800"}}}],'
                    b'"metadata":{"count":1}}'
                ),
                request=request,
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OfficialRegistryError) as raised:
                await OfficialRegistryAdapter(transport=client._transport).search("weather")
        assert raised.value.code == OfficialRegistryFailureCode.INVALID_RESPONSE
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None

    asyncio.run(scenario())


def test_metadata_scanner_failure_is_payload_free() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return fixture_response("search_page_1.json", request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(OfficialRegistryError) as raised:
                await OfficialRegistryAdapter(
                    transport=client._transport,
                    metadata_scanner=scanner_fails_with_sensitive_detail,
                ).search("weather")
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
                adapter = OfficialRegistryAdapter(transport=client._transport)
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
        hooked_requests: list[httpx.Request] = []

        async def capture_request(request: httpx.Request) -> None:
            hooked_requests.append(request)

        async def handler(request: httpx.Request) -> httpx.Response:
            assert "Authorization" not in request.headers
            assert "Cookie" not in request.headers
            logging.getLogger("httpx").info("unrelated outbound request")
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"servers": [], "metadata": {"count": 0}},
                request=request,
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            auth=httpx.BasicAuth("ambient-user", "ambient-password"),
            headers={"Authorization": "Bearer ambient-secret"},
            cookies={"session": "ambient-cookie"},
            event_hooks={"request": [capture_request]},
        ) as client:
            with caplog.at_level(logging.INFO, logger="httpx"):
                await OfficialRegistryAdapter(transport=client._transport).search(
                    "private operator search"
                )
        assert hooked_requests == []

    asyncio.run(scenario())
    assert "private operator search" not in caplog.text
    assert "ambient-secret" not in caplog.text
    assert "unrelated outbound request" in caplog.text


def test_adapter_does_not_close_borrowed_transport_between_searches() -> None:
    async def scenario() -> None:
        transport = ReusableTrackingTransport()
        adapter = OfficialRegistryAdapter(transport=transport)
        assert await adapter.search("first") == ()
        assert await adapter.search("second") == ()
        assert (transport.calls, transport.closes) == (2, 0)
        await transport.aclose()
        assert transport.closes == 1

    asyncio.run(scenario())


def test_adapter_disables_the_independent_httpx_timeout() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert set(request.extensions["timeout"].values()) == {None}
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"servers": [], "metadata": {"count": 0}},
                request=request,
            )

        assert (
            await OfficialRegistryAdapter(
                transport=httpx.MockTransport(handler),
                limits=OfficialRegistryLimits(total_timeout_seconds=10),
            ).search("weather")
            == ()
        )

    asyncio.run(scenario())


def test_scanner_semaphore_supports_uvloop() -> None:
    uvloop = pytest.importorskip("uvloop")
    loop = uvloop.new_event_loop()

    async def scenario() -> None:
        first = official_registry._scanner_process_semaphore()
        assert official_registry._scanner_process_semaphore() is first

    try:
        loop.run_until_complete(scenario())
    finally:
        loop.close()


def test_contended_scanner_semaphore_does_not_retain_closed_loop() -> None:
    def exercise_loop() -> ReferenceType[asyncio.AbstractEventLoop]:
        loop = asyncio.new_event_loop()

        async def scenario() -> None:
            semaphore = official_registry._scanner_process_semaphore()
            for _ in range(4):
                await semaphore.acquire()
            waiter = asyncio.create_task(semaphore.acquire())
            await asyncio.sleep(0)
            assert not waiter.done()
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter
            for _ in range(4):
                semaphore.release()

        loop.run_until_complete(scenario())
        loop_reference = ref(loop)
        loop.close()
        return loop_reference

    loop_reference = exercise_loop()
    gc.collect()
    assert loop_reference() is None


def test_scanning_runs_off_loop_under_the_configured_deadline() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return fixture_response("search_page_1.json", request)

        limits = OfficialRegistryLimits(total_timeout_seconds=0.01)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = OfficialRegistryAdapter(
                transport=client._transport,
                limits=limits,
                query_scanner=scanner_allows,
                metadata_scanner=scanner_blocks_briefly,
            )
            with pytest.raises(OfficialRegistryError) as raised:
                await adapter.search("weather")
        assert raised.value.code == OfficialRegistryFailureCode.TIMEOUT
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None

    asyncio.run(scenario())


def test_service_deadline_is_detached_from_scanned_metadata() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return fixture_response("search_page_1.json", request)

        limits = OfficialRegistryLimits(total_timeout_seconds=0.01)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="service-timeout-context")
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                with pytest.raises(OfficialRegistryError) as raised:
                    await OfficialRegistryService(
                        session,
                        OfficialRegistryAdapter(
                            transport=httpx.MockTransport(handler),
                            limits=limits,
                            query_scanner=scanner_allows,
                            metadata_scanner=scanner_blocks_briefly,
                        ),
                    ).search(context=context, query="private operator query")
        assert raised.value.code == OfficialRegistryFailureCode.TIMEOUT
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None

    asyncio.run(scenario())


def test_service_screens_each_search_only_once() -> None:
    class CountingAdapter(OfficialRegistryAdapter):
        def __init__(self, *, transport: httpx.AsyncBaseTransport) -> None:
            super().__init__(transport=transport)
            self.screen_calls = 0

        async def screen_query(self, query: str) -> str:
            self.screen_calls += 1
            return await super().screen_query(query)

    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"servers": [], "metadata": {"count": 0}},
                request=request,
            )

        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="single-query-screen")
            adapter = CountingAdapter(transport=httpx.MockTransport(handler))
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                await OfficialRegistryService(session, adapter).search(
                    context=context, query="weather"
                )
            assert adapter.screen_calls == 1

    asyncio.run(scenario())


def test_repeated_hanging_scanners_are_killed_without_leaking_workers() -> None:
    async def scenario() -> None:
        limits = OfficialRegistryLimits(total_timeout_seconds=0.02)
        async with httpx.AsyncClient() as client:
            adapter = OfficialRegistryAdapter(
                transport=client._transport,
                limits=limits,
                query_scanner=scanner_hangs,
            )
            existing_children = {child.pid for child in multiprocessing.active_children()}
            for _ in range(3):
                with pytest.raises(OfficialRegistryError) as raised:
                    await adapter.screen_query("weather")
                assert raised.value.code == OfficialRegistryFailureCode.SCANNER_FAILED
            assert {child.pid for child in multiprocessing.active_children()} == existing_children

    asyncio.run(scenario())


def test_scanner_process_concurrency_is_globally_bounded() -> None:
    async def scenario() -> None:
        adapter = OfficialRegistryAdapter(
            limits=OfficialRegistryLimits(total_timeout_seconds=3),
            query_scanner=scanner_blocks_briefly,
        )
        existing_children = {child.pid for child in multiprocessing.active_children()}
        tasks = [
            asyncio.create_task(adapter.screen_query(f"weather-{index}")) for index in range(8)
        ]
        maximum_new_children = 0
        while not all(task.done() for task in tasks):
            maximum_new_children = max(
                maximum_new_children,
                len(
                    {
                        child.pid
                        for child in multiprocessing.active_children()
                        if child.pid not in existing_children
                    }
                ),
            )
            await asyncio.sleep(0.005)
        assert await asyncio.gather(*tasks) == [f"weather-{index}" for index in range(8)]
        assert 1 <= maximum_new_children <= 4
        assert {child.pid for child in multiprocessing.active_children()} == existing_children

    asyncio.run(scenario())
    asyncio.run(scenario())


def test_scanner_policy_and_child_protocol_are_covered_in_process() -> None:
    safe_metadata = {
        "server": {
            "description": "Public weather observations",
            "tags": ["weather", 1],
        }
    }
    assert official_registry._default_metadata_scanner(safe_metadata) is False
    assert (
        official_registry._default_metadata_scanner(
            {"description": "token%2525253DAbCdEfGhIjKlMnOpQrStUvWx"}
        )
        is True
    )
    with pytest.raises(OfficialRegistryError) as duplicate:
        official_registry._default_metadata_scanner({"name": "one", "%6eame": "two"})
    assert duplicate.value.code == OfficialRegistryFailureCode.UNSAFE_METADATA

    receiver, sender = multiprocessing.Pipe(duplex=False)
    official_registry._scanner_process_main(scanner_allows, safe_metadata, sender)
    assert receiver.recv() == (True, False)
    receiver.close()

    receiver, sender = multiprocessing.Pipe(duplex=False)
    official_registry._scanner_process_main(scanner_fails_with_sensitive_detail, {}, sender)
    assert receiver.recv() == (False, False)
    receiver.close()


def test_normalized_cache_payload_must_fit_the_active_byte_limit() -> None:
    async def scenario() -> None:
        payload = {
            "servers": [
                {
                    "server": {
                        "name": "io.modall.fixture.expansion",
                        "version": "1.0.0",
                        "values": [10.0] * 256,
                    }
                }
            ],
            "metadata": {"count": 1},
        }
        raw = json.dumps(payload, separators=(",", ":")).replace("10.0", "1e1").encode()
        normalized = json.dumps(payload["servers"], separators=(",", ":"), sort_keys=True).encode()
        assert len(raw) < len(normalized)
        byte_limit = (len(raw) + len(normalized)) // 2

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=raw,
                request=request,
            )

        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="normalized-byte-limit")
            async with (
                httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
                transaction(factory) as session,
            ):
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                service = OfficialRegistryService(
                    session,
                    OfficialRegistryAdapter(
                        transport=client._transport,
                        limits=OfficialRegistryLimits(max_response_bytes=byte_limit),
                    ),
                )
                with pytest.raises(OfficialRegistryError) as raised:
                    await service.search(context=context, query="expansion")
                assert raised.value.code == OfficialRegistryFailureCode.RESPONSE_LIMIT
                assert (
                    await session.scalar(select(func.count()).select_from(RegistrySearchCache)) == 0
                )

    asyncio.run(scenario())


def test_combined_paginated_metadata_must_fit_structural_limits() -> None:
    async def scenario() -> None:
        calls = 0

        def server(page: int) -> dict[str, object]:
            return {
                "server": {
                    "name": f"io.modall.fixture.structural-{page}",
                    "version": "1.0.0",
                    "values": [page] * 900,
                }
            }

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            page = calls
            calls += 1
            metadata: dict[str, object] = {"count": 1}
            if page < 4:
                metadata["nextCursor"] = f"page-{page + 1}"
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={
                    "servers": [server(page)],
                    "metadata": metadata,
                },
                request=request,
            )

        limits = OfficialRegistryLimits(total_timeout_seconds=10)
        with pytest.raises(OfficialRegistryError) as cached_revalidation:
            await OfficialRegistryAdapter(limits=limits).parse_cached_items(
                [server(page) for page in range(5)]
            )
        assert cached_revalidation.value.code == OfficialRegistryFailureCode.RESPONSE_LIMIT

        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="combined-structure")
            async with (
                httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client,
                transaction(factory) as session,
            ):
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                service = OfficialRegistryService(
                    session,
                    OfficialRegistryAdapter(
                        transport=client._transport,
                        limits=limits,
                    ),
                )
                with pytest.raises(OfficialRegistryError) as raised:
                    await service.search(context=context, query="structural")
                assert raised.value.code == OfficialRegistryFailureCode.RESPONSE_LIMIT
                assert (
                    await session.scalar(select(func.count()).select_from(RegistrySearchCache)) == 0
                )
        assert calls == 5

    asyncio.run(scenario())


def test_workspace_cache_row_budget_rejects_additional_queries() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"servers": [], "metadata": {"count": 0}},
                request=request,
            )

        limits = OfficialRegistryLimits(max_workspace_cache_rows=1)
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="cache-row-budget")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                adapter = OfficialRegistryAdapter(transport=client._transport, limits=limits)
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    await OfficialRegistryService(session, adapter).search(
                        context=context, query="first"
                    )
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    with pytest.raises(OfficialRegistryError) as raised:
                        await OfficialRegistryService(session, adapter).search(
                            context=context, query="second"
                        )
                    assert raised.value.code == OfficialRegistryFailureCode.RESPONSE_LIMIT
                    assert (
                        await session.scalar(select(func.count()).select_from(RegistrySearchCache))
                        == 1
                    )
        assert calls == 2

    asyncio.run(scenario())


def test_workspace_cache_byte_budget_rejects_additional_queries() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={
                    "servers": [
                        {
                            "server": {
                                "name": "io.modall.fixture.cache-budget",
                                "version": "1.0.0",
                                "description": "x" * 700,
                            }
                        }
                    ],
                    "metadata": {"count": 1},
                },
                request=request,
            )

        limits = OfficialRegistryLimits(
            max_response_bytes=1024,
            max_workspace_cache_bytes=1024,
        )
        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="cache-byte-budget")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                adapter = OfficialRegistryAdapter(transport=client._transport, limits=limits)
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    first = await OfficialRegistryService(session, adapter).search(
                        context=context, query="first"
                    )
                    assert first.from_cache is False
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    with pytest.raises(OfficialRegistryError) as raised:
                        await OfficialRegistryService(session, adapter).search(
                            context=context, query="second"
                        )
                    assert raised.value.code == OfficialRegistryFailureCode.RESPONSE_LIMIT
                    assert (
                        await session.scalar(select(func.count()).select_from(RegistrySearchCache))
                        == 1
                    )

    asyncio.run(scenario())


def test_registry_payload_flush_failure_is_context_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"servers": [], "metadata": {"count": 0}},
                request=request,
            )

        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="payload-flush-failure")
            with pytest.raises(OfficialRegistryError) as raised:
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)

                    async def failed_flush() -> None:
                        raise StatementError(
                            "insert failed",
                            "INSERT INTO registry_search_cache",
                            {"normalized_results": {"secret": "must-not-escape"}},
                            RuntimeError("database detail"),
                        )

                    monkeypatch.setattr(session, "flush", failed_flush)
                    await OfficialRegistryService(
                        session,
                        OfficialRegistryAdapter(transport=httpx.MockTransport(handler)),
                    ).search(context=context, query="weather")
            assert raised.value.code == OfficialRegistryFailureCode.PERSISTENCE_FAILURE
            assert raised.value.__cause__ is None
            assert raised.value.__context__ is None

    asyncio.run(scenario())


def test_import_purges_cache_rejected_by_the_active_scanner() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads((FIXTURES / "search_page_1.json").read_text())
            payload["servers"][0]["server"]["description"] = (
                "token%2525253DAbCdEfGhIjKlMnOpQrStUvWx"
            )
            payload["metadata"].pop("nextCursor", None)
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json=payload,
                request=request,
            )

        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="import-rescan")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    searched = await OfficialRegistryService(
                        session,
                        OfficialRegistryAdapter(
                            transport=client._transport,
                            metadata_scanner=scanner_allows,
                        ),
                    ).search(context=context, query="weather")

                with pytest.raises(OfficialRegistryError) as raised:
                    async with transaction(factory) as session:
                        context = await context_for(
                            session, user_id=user_id, workspace_id=workspace_id
                        )
                        service = OfficialRegistryService(
                            session, OfficialRegistryAdapter(transport=client._transport)
                        )
                        await service.import_cached(
                            context=context,
                            cache_id=searched.cache_id,
                            provenance_digest=searched.items[0].provenance_digest,
                        )
                assert raised.value.code == OfficialRegistryFailureCode.UNSAFE_METADATA

                async with transaction(factory) as session:
                    assert await session.get(RegistrySearchCache, searched.cache_id) is None

    asyncio.run(scenario())


def test_search_rejected_cache_flushes_for_quota_and_survives_rollback() -> None:
    async def scenario() -> None:
        calls: dict[str, int] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            query = request.url.params["search"]
            calls[query] = calls.get(query, 0) + 1
            if calls[query] == 1:
                payload = json.loads((FIXTURES / "search_page_1.json").read_text())
                payload["servers"][0]["server"]["description"] = (
                    "token%2525253DAbCdEfGhIjKlMnOpQrStUvWx"
                )
                payload["metadata"].pop("nextCursor", None)
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                    request=request,
                )
            if query == "rollback":
                raise httpx.ConnectError("upstream unavailable", request=request)
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                json={"servers": [], "metadata": {"count": 0}},
                request=request,
            )

        limits = OfficialRegistryLimits(max_workspace_cache_rows=1)
        async with database() as factory:
            replace_user, replace_workspace = await bootstrap(factory, subject="search-replace")
            rollback_user, rollback_workspace = await bootstrap(factory, subject="search-rollback")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                permissive = OfficialRegistryAdapter(
                    transport=client._transport,
                    limits=limits,
                    metadata_scanner=scanner_allows,
                )
                strict = OfficialRegistryAdapter(transport=client._transport, limits=limits)
                async with transaction(factory) as session:
                    context = await context_for(
                        session, user_id=replace_user, workspace_id=replace_workspace
                    )
                    rejected = await OfficialRegistryService(session, permissive).search(
                        context=context, query="replace"
                    )
                async with transaction(factory) as session:
                    context = await context_for(
                        session, user_id=replace_user, workspace_id=replace_workspace
                    )
                    replacement = await OfficialRegistryService(session, strict).search(
                        context=context, query="replace"
                    )
                    assert replacement.cache_id != rejected.cache_id
                    assert (
                        await session.scalar(
                            select(func.count())
                            .select_from(RegistrySearchCache)
                            .where(RegistrySearchCache.workspace_id == replace_workspace)
                        )
                        == 1
                    )

                async with transaction(factory) as session:
                    context = await context_for(
                        session, user_id=rollback_user, workspace_id=rollback_workspace
                    )
                    rollback_cache = await OfficialRegistryService(session, permissive).search(
                        context=context, query="rollback"
                    )
                with pytest.raises(OfficialRegistryError) as unavailable:
                    async with transaction(factory) as session:
                        context = await context_for(
                            session, user_id=rollback_user, workspace_id=rollback_workspace
                        )
                        await OfficialRegistryService(session, strict).search(
                            context=context, query="rollback"
                        )
                assert unavailable.value.code == OfficialRegistryFailureCode.UPSTREAM_UNAVAILABLE
                async with transaction(factory) as session:
                    assert await session.get(RegistrySearchCache, rollback_cache.cache_id) is None

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
            adapter = OfficialRegistryAdapter(
                transport=client._transport, limits=OfficialRegistryLimits(max_items=2)
            )
            with pytest.raises(ValueError, match="one official Registry limits policy"):
                OfficialRegistryService(
                    session,
                    adapter,
                    limits=OfficialRegistryLimits(max_items=1),
                )

    asyncio.run(scenario())


def test_cache_reuse_and_import_respect_a_stricter_rolling_byte_policy() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls >= 3:
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    json={"servers": [], "metadata": {"count": 0}},
                    request=request,
                )
            if "cursor" in request.url.params:
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    json={"servers": [], "metadata": {"count": 0}},
                    request=request,
                )
            return fixture_response("official_remote_sample.json", request)

        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="rolling-limits")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    original = await OfficialRegistryService(
                        session, OfficialRegistryAdapter(transport=client._transport)
                    ).search(context=context, query="inference")
                    cache = await session.get(RegistrySearchCache, original.cache_id)
                    assert cache is not None
                    assert cache.byte_count > 100

                strict = OfficialRegistryLimits(max_response_bytes=100, max_workspace_cache_rows=1)
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    service = OfficialRegistryService(
                        session, OfficialRegistryAdapter(transport=client._transport, limits=strict)
                    )
                    replacement = await service.search(context=context, query="inference")
                    assert replacement.from_cache is False
                    assert replacement.cache_id != original.cache_id
                    with pytest.raises(OfficialRegistryError) as import_miss:
                        await service.import_cached(
                            context=context,
                            cache_id=original.cache_id,
                            provenance_digest=original.items[0].provenance_digest,
                        )
                    assert import_miss.value.code == OfficialRegistryFailureCode.CACHE_MISS
                    assert await session.get(RegistrySearchCache, original.cache_id) is None
                assert calls == 3

    asyncio.run(scenario())


def test_cache_quota_excludes_rows_above_a_stricter_rolling_item_policy() -> None:
    async def scenario() -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls >= 3:
                return httpx.Response(
                    200,
                    headers={"Content-Type": "application/json"},
                    json={"servers": [], "metadata": {"count": 0}},
                    request=request,
                )
            if "cursor" in request.url.params:
                return fixture_response("search_page_2.json", request)
            return fixture_response("search_page_1.json", request)

        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="rolling-item-limit")
            transport = httpx.MockTransport(handler)
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                original = await OfficialRegistryService(
                    session, OfficialRegistryAdapter(transport=transport)
                ).search(context=context, query="fixture")
                assert len(original.items) == 2

            limits = OfficialRegistryLimits(max_items=1, max_workspace_cache_rows=1)
            async with transaction(factory) as session:
                context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                replacement = await OfficialRegistryService(
                    session, OfficialRegistryAdapter(transport=transport, limits=limits)
                ).search(context=context, query="fixture")
                assert replacement.from_cache is False
                assert replacement.cache_id != original.cache_id
                assert await session.get(RegistrySearchCache, original.cache_id) is None
            assert calls == 3

    asyncio.run(scenario())


def test_cache_reuse_and_import_respect_a_stricter_rolling_ttl() -> None:
    async def scenario() -> None:
        calls = 0
        now = datetime(2026, 9, 6, tzinfo=UTC)

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if "cursor" in request.url.params:
                return fixture_response("search_page_2.json", request)
            return fixture_response("search_page_1.json", request)

        async with database() as factory:
            user_id, workspace_id = await bootstrap(factory, subject="rolling-ttl")
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    original = await OfficialRegistryService(
                        session,
                        OfficialRegistryAdapter(transport=client._transport),
                        now=lambda: now,
                    ).search(context=context, query="fixture")

                now += timedelta(minutes=10)
                strict_limits = OfficialRegistryLimits(
                    cache_ttl=timedelta(minutes=5), max_workspace_cache_rows=1
                )
                async with transaction(factory) as session:
                    context = await context_for(session, user_id=user_id, workspace_id=workspace_id)
                    service = OfficialRegistryService(
                        session,
                        OfficialRegistryAdapter(transport=client._transport, limits=strict_limits),
                        now=lambda: now,
                    )
                    refreshed = await service.search(context=context, query="fixture")
                    assert refreshed.from_cache is False
                    assert refreshed.cache_id != original.cache_id
                    with pytest.raises(OfficialRegistryError) as expired_import:
                        await service.import_cached(
                            context=context,
                            cache_id=original.cache_id,
                            provenance_digest=original.items[0].provenance_digest,
                        )
                    assert expired_import.value.code == OfficialRegistryFailureCode.CACHE_MISS
                    assert await session.get(RegistrySearchCache, original.cache_id) is None
                assert calls == 4

    asyncio.run(scenario())

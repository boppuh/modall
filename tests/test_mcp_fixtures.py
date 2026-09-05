import asyncio
import hashlib
import json
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, McpError, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

from tests.support.mcp_fixture_server import (
    FIXTURE_TOKEN,
    PROTOCOL_REVISION,
    create_mcp_fixture_app,
)

FIXTURES = Path(__file__).parent / "fixtures" / "registry"
ACCEPT_HEADERS = {"Accept": "application/json, text/event-stream"}


def _request(request_id: int, method: str, params: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


async def _handshake(
    client: httpx.AsyncClient,
    profile: str,
    *,
    authorization: str | None = None,
) -> dict[str, str]:
    headers = dict(ACCEPT_HEADERS)
    if authorization is not None:
        headers["Authorization"] = authorization
    response = await client.post(
        f"/mcp/{profile}",
        headers=headers,
        json=_request(
            1,
            "initialize",
            {
                "protocolVersion": PROTOCOL_REVISION,
                "capabilities": {},
                "clientInfo": {"name": "modall-tests", "version": "1"},
            },
        ),
    )
    response.raise_for_status()
    assert response.json()["result"]["protocolVersion"] == PROTOCOL_REVISION
    headers.update(
        {
            "Mcp-Protocol-Version": PROTOCOL_REVISION,
            "Mcp-Session-Id": response.headers["Mcp-Session-Id"],
        }
    )
    notification = await client.post(
        f"/mcp/{profile}",
        headers=headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert notification.status_code == 202
    return headers


async def _sdk_initialize(session: ClientSession) -> types.InitializeResult:
    initialized = await session.send_request(
        types.ClientRequest(
            types.InitializeRequest(
                params=types.InitializeRequestParams(
                    protocolVersion=PROTOCOL_REVISION,
                    capabilities=types.ClientCapabilities(),
                    clientInfo=types.Implementation(name="modall-tests", version="1"),
                )
            )
        ),
        types.InitializeResult,
    )
    await session.send_notification(types.ClientNotification(types.InitializedNotification()))
    return initialized


def _leaf_exceptions(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in _leaf_exceptions(child)]
    return [error]


def test_reference_server_initialization_pagination_drift_and_results() -> None:
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_mcp_fixture_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
            default_headers = await _handshake(client, "default")
            schema_v1_headers = await _handshake(client, "schema-drift")
            page_1 = (
                await client.post(
                    "/mcp/schema-drift",
                    headers=schema_v1_headers,
                    json=_request(2, "tools/list", {}),
                )
            ).json()["result"]
            page_2 = (
                await client.post(
                    "/mcp/schema-drift",
                    headers=schema_v1_headers,
                    json=_request(3, "tools/list", {"cursor": page_1["nextCursor"]}),
                )
            ).json()["result"]
            assert [tool["name"] for tool in page_1["tools"] + page_2["tools"]] == [
                "echo",
                "status",
                "unsupported-content",
                "fail",
            ]
            schema_v2_headers = await _handshake(client, "schema-drift")
            schema_drift = (
                await client.post(
                    "/mcp/schema-drift",
                    headers=schema_v2_headers,
                    json=_request(4, "tools/list", {}),
                )
            ).json()["result"]["tools"][0]
            assert schema_drift["inputSchema"] != page_1["tools"][0]["inputSchema"]
            metadata_v1_headers = await _handshake(client, "metadata-drift")
            metadata_v1 = (
                await client.post(
                    "/mcp/metadata-drift",
                    headers=metadata_v1_headers,
                    json=_request(5, "tools/list", {}),
                )
            ).json()["result"]["tools"][0]
            metadata_headers = await _handshake(client, "metadata-drift")
            metadata_drift = (
                await client.post(
                    "/mcp/metadata-drift",
                    headers=metadata_headers,
                    json=_request(6, "tools/list", {}),
                )
            ).json()["result"]["tools"][0]
            assert metadata_drift["inputSchema"] == metadata_v1["inputSchema"]
            assert metadata_drift["description"] != metadata_v1["description"]
            called = await client.post(
                "/mcp/default",
                headers=default_headers,
                json=_request(6, "tools/call", {"name": "echo", "arguments": {"message": "hello"}}),
            )
            assert called.json()["result"]["structuredContent"] == {"message": "hello"}
            status = await client.post(
                "/mcp/default",
                headers=default_headers,
                json=_request(7, "tools/call", {"name": "status", "arguments": {}}),
            )
            assert "structuredContent" not in status.json()["result"]
            assert status.json()["result"]["isError"] is False
            unsupported = await client.post(
                "/mcp/default",
                headers=default_headers,
                json=_request(8, "tools/call", {"name": "unsupported-content", "arguments": {}}),
            )
            assert unsupported.json()["result"]["content"][0]["type"] == "image"
            failed = await client.post(
                "/mcp/default",
                headers=default_headers,
                json=_request(9, "tools/call", {"name": "fail", "arguments": {}}),
            )
            assert failed.json()["result"]["isError"] is True
            missing = await client.post(
                "/mcp/default",
                headers=default_headers,
                json=_request(10, "tools/call", {"name": "missing", "arguments": {}}),
            )
            assert missing.json()["error"]["code"] == -32602

    asyncio.run(scenario())


def test_reference_server_auth_protocol_and_transport_fault_profiles() -> None:
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_mcp_fixture_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
            request = _request(1, "tools/list", {})
            assert (
                await client.post("/mcp/authenticated", headers=ACCEPT_HEADERS, json=request)
            ).status_code == 401
            authenticated_headers = await _handshake(
                client, "authenticated", authorization=f"Bearer {FIXTURE_TOKEN}"
            )
            authenticated = await client.post(
                "/mcp/authenticated", headers=authenticated_headers, json=request
            )
            assert authenticated.status_code == 200
            mismatch = await client.post(
                "/mcp/protocol-mismatch",
                headers=ACCEPT_HEADERS,
                json=_request(
                    2,
                    "initialize",
                    {"protocolVersion": PROTOCOL_REVISION},
                ),
            )
            assert mismatch.json()["result"]["protocolVersion"] == "2025-11-25"
            mismatch_headers = {
                **ACCEPT_HEADERS,
                "Mcp-Protocol-Version": "2025-11-25",
                "Mcp-Session-Id": mismatch.headers["Mcp-Session-Id"],
            }
            mismatch_initialized = await client.post(
                "/mcp/protocol-mismatch",
                headers=mismatch_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert mismatch_initialized.status_code == 202
            assert (
                await client.post("/mcp/protocol-mismatch", headers=mismatch_headers, json=request)
            ).status_code == 200
            for profile in ("malformed", "oversized", "timeout", "disconnect"):
                headers = await _handshake(client, profile)
                if profile == "malformed":
                    malformed_headers = headers
                elif profile == "oversized":
                    oversized_headers = headers
                elif profile == "timeout":
                    timeout_headers = headers
                else:
                    disconnect_headers = headers
            malformed = await client.post("/mcp/malformed", headers=malformed_headers, json=request)
            assert malformed.content == b'{"jsonrpc":'
            oversized = await client.post("/mcp/oversized", headers=oversized_headers, json=request)
            assert len(oversized.content) > 262_144
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.01):
                    await client.post("/mcp/timeout", headers=timeout_headers, json=request)
            with pytest.raises(ConnectionError, match="fixture disconnect"):
                await client.post("/mcp/disconnect", headers=disconnect_headers, json=request)

    asyncio.run(scenario())


def test_reference_server_rejects_missing_transport_and_lifecycle_headers() -> None:
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_mcp_fixture_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
            initialize = _request(1, "initialize", {"protocolVersion": PROTOCOL_REVISION})
            assert (await client.post("/mcp/headers", json=initialize)).status_code == 406
            invalid_initializations: tuple[dict[str, object], ...] = (
                {},
                {"protocolVersion": "2025-11-25"},
            )
            for invalid_params in invalid_initializations:
                assert (
                    await client.post(
                        "/mcp/headers",
                        headers=ACCEPT_HEADERS,
                        json=_request(1, "initialize", invalid_params),
                    )
                ).status_code == 400
            response = await client.post("/mcp/headers", headers=ACCEPT_HEADERS, json=initialize)
            assert response.status_code == 200
            assert (
                await client.post(
                    "/mcp/headers", headers=ACCEPT_HEADERS, json=_request(2, "tools/list", {})
                )
            ).status_code == 400

            session_headers = {
                **ACCEPT_HEADERS,
                "Mcp-Protocol-Version": PROTOCOL_REVISION,
                "Mcp-Session-Id": response.headers["Mcp-Session-Id"],
            }
            assert (
                await client.post(
                    "/mcp/headers", headers=session_headers, json=_request(3, "tools/list", {})
                )
            ).status_code == 409
            assert (
                await client.post(
                    "/mcp/headers",
                    headers=session_headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
            ).status_code == 202
            invalid_protocol_headers = {
                **session_headers,
                "Mcp-Protocol-Version": "2025-11-25",
            }
            assert (
                await client.post(
                    "/mcp/headers",
                    headers=invalid_protocol_headers,
                    json=_request(4, "tools/list", {}),
                )
            ).status_code == 400
            second = await client.post("/mcp/headers", headers=ACCEPT_HEADERS, json=initialize)
            assert second.headers["Mcp-Session-Id"] != response.headers["Mcp-Session-Id"]
            assert (
                await client.post(
                    "/mcp/headers", headers=session_headers, json=_request(5, "tools/list", {})
                )
            ).status_code == 200
            second_headers = {
                **ACCEPT_HEADERS,
                "Mcp-Protocol-Version": PROTOCOL_REVISION,
                "Mcp-Session-Id": second.headers["Mcp-Session-Id"],
            }
            assert (
                await client.post(
                    "/mcp/headers", headers=second_headers, json=_request(6, "tools/list", {})
                )
            ).status_code == 409

    asyncio.run(scenario())


def test_pinned_sdk_negotiates_and_parses_reference_server() -> None:
    async def scenario() -> None:
        app = create_mcp_fixture_app()
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fixture",
        )
        async with (
            http_client,
            streamable_http_client("http://fixture/mcp/sdk", http_client=http_client) as (
                read_stream,
                write_stream,
                _,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await _sdk_initialize(session)
            assert initialized.protocolVersion == PROTOCOL_REVISION
            tools = await session.list_tools()
            assert [tool.name for tool in tools.tools] == ["echo", "status"]
            remaining = await session.list_tools(cursor=tools.nextCursor)
            assert [tool.name for tool in remaining.tools] == ["unsupported-content", "fail"]
            echo = await session.call_tool("echo", {"message": "hello"})
            assert echo.structuredContent == {"message": "hello"}
            assert isinstance(echo.content[0], types.TextContent)
            assert echo.content[0].text == "hello"
            status = await session.call_tool("status", {})
            assert status.structuredContent is None
            assert isinstance(status.content[0], types.TextContent)
            assert status.isError is False
            unsupported = await session.call_tool("unsupported-content", {})
            assert isinstance(unsupported.content[0], types.ImageContent)
            failed = await session.call_tool("fail", {})
            assert failed.isError is True
            with pytest.raises(McpError) as unknown:
                await session.call_tool("missing", {})
            assert unknown.value.error.code == -32602

        authenticated_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fixture",
            headers={"Authorization": f"Bearer {FIXTURE_TOKEN}"},
        )
        async with (
            authenticated_client,
            streamable_http_client(
                "http://fixture/mcp/authenticated", http_client=authenticated_client
            ) as (read_stream, write_stream, _),
            ClientSession(read_stream, write_stream) as authenticated_session,
        ):
            await _sdk_initialize(authenticated_session)
            authenticated_tools = await authenticated_session.list_tools()
            assert authenticated_tools.tools[0].name == "echo"

        mismatch_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fixture",
        )
        async with (
            mismatch_client,
            streamable_http_client(
                "http://fixture/mcp/protocol-mismatch", http_client=mismatch_client
            ) as (read_stream, write_stream, _),
            ClientSession(read_stream, write_stream) as mismatch_session,
        ):
            mismatch = await _sdk_initialize(mismatch_session)
            assert mismatch.protocolVersion == "2025-11-25"
            mismatch_tools = await mismatch_session.list_tools()
            assert mismatch_tools.tools[0].name == "echo"

    asyncio.run(scenario())


def test_pinned_sdk_surfaces_response_and_transport_faults() -> None:
    async def exercise(profile: str) -> types.ListToolsResult:
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_mcp_fixture_app()),
            base_url="http://fixture",
        )
        async with (
            asyncio.timeout(1),
            http_client,
            streamable_http_client(f"http://fixture/mcp/{profile}", http_client=http_client) as (
                read_stream,
                write_stream,
                _,
            ),
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(milliseconds=10),
            ) as session,
        ):
            await _sdk_initialize(session)
            return await session.list_tools()

    async def scenario() -> None:
        with pytest.raises(ExceptionGroup) as timeout_error:
            await exercise("timeout")
        timeout_leaves = _leaf_exceptions(timeout_error.value)
        assert any(
            isinstance(error, McpError) and "Timed out" in str(error) for error in timeout_leaves
        )

        with pytest.raises(ExceptionGroup) as disconnect_error:
            await exercise("disconnect")
        assert any(
            isinstance(error, ConnectionError) for error in _leaf_exceptions(disconnect_error.value)
        )

        with pytest.raises(ExceptionGroup) as malformed_error:
            await exercise("malformed")
        assert any(
            isinstance(error, McpError) and "Timed out" in str(error)
            for error in _leaf_exceptions(malformed_error.value)
        )

        oversized = await exercise("oversized")
        padding = (oversized.model_extra or {}).get("padding")
        assert isinstance(padding, str)
        assert len(padding) > 262_144

    asyncio.run(scenario())


def test_pinned_sdk_does_not_follow_redirects_with_or_without_credentials() -> None:
    async def exercise(profile: str, *, authenticated: bool) -> None:
        headers = {"Authorization": f"Bearer {FIXTURE_TOKEN}"} if authenticated else {}
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_mcp_fixture_app()),
            base_url="http://fixture",
            headers=headers,
        )
        async with (
            asyncio.timeout(1),
            http_client,
            streamable_http_client(f"http://fixture/mcp/{profile}", http_client=http_client) as (
                read_stream,
                write_stream,
                _,
            ),
            ClientSession(read_stream, write_stream) as session,
        ):
            await _sdk_initialize(session)

    async def scenario() -> None:
        for profile, authenticated in (
            ("redirect", False),
            ("authenticated-redirect", True),
        ):
            with pytest.raises(ExceptionGroup) as redirect_error:
                await exercise(profile, authenticated=authenticated)
            assert any(
                isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 307
                for error in _leaf_exceptions(redirect_error.value)
            )

    asyncio.run(scenario())


def test_recorded_registry_pages_are_offline_and_cursor_exact() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    first = json.loads((FIXTURES / "search_page_1.json").read_text())
    second = json.loads((FIXTURES / "search_page_2.json").read_text())
    recorded = json.loads((FIXTURES / "official_search_sample.json").read_text())
    assert manifest["apiRevision"] == "v0.1"
    assert "not captured upstream responses" in manifest["generated"]["purpose"]
    for fixture_kind in ("generated", "recorded"):
        for filename, digest in manifest[fixture_kind]["files"].items():
            assert hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest() == digest
    assert first["metadata"]["nextCursor"] == "opaque-page-2"
    assert "nextCursor" not in second["metadata"]
    assert all(item["server"]["remotes"] for item in first["servers"] + second["servers"])
    assert recorded["servers"][0]["server"]["name"] == ("io.github.domdomegg/airtable-mcp-server")
    assert "io.modelcontextprotocol.registry/official" in recorded["servers"][0]["_meta"]


def test_selected_sdk_supports_qualified_protocol_revision() -> None:
    assert version("mcp") == "1.29.1"
    assert PROTOCOL_REVISION in SUPPORTED_PROTOCOL_VERSIONS

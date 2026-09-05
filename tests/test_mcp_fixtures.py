import asyncio
import hashlib
import json
from importlib.metadata import version
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
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


def test_reference_server_initialization_pagination_drift_and_results() -> None:
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_mcp_fixture_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
            default_headers = await _handshake(client, "default")
            schema_v1_headers = await _handshake(client, "schema-drift-v1")
            page_1 = (
                await client.post(
                    "/mcp/schema-drift-v1",
                    headers=schema_v1_headers,
                    json=_request(2, "tools/list", {}),
                )
            ).json()["result"]
            page_2 = (
                await client.post(
                    "/mcp/schema-drift-v1",
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
            schema_v2_headers = await _handshake(client, "schema-drift-v2")
            schema_drift = (
                await client.post(
                    "/mcp/schema-drift-v2",
                    headers=schema_v2_headers,
                    json=_request(4, "tools/list", {}),
                )
            ).json()["result"]["tools"][0]
            assert schema_drift["inputSchema"] != page_1["tools"][0]["inputSchema"]
            metadata_headers = await _handshake(client, "metadata-drift-v2")
            metadata_drift = (
                await client.post(
                    "/mcp/metadata-drift-v2",
                    headers=metadata_headers,
                    json=_request(5, "tools/list", {}),
                )
            ).json()["result"]["tools"][0]
            assert metadata_drift["inputSchema"] == page_1["tools"][0]["inputSchema"]
            assert metadata_drift["description"] != page_1["tools"][0]["description"]
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

    asyncio.run(scenario())


def test_pinned_sdk_negotiates_and_parses_reference_server() -> None:
    async def scenario() -> None:
        http_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_mcp_fixture_app()),
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
            initialized = await session.initialize()
            assert initialized.protocolVersion == PROTOCOL_REVISION
            tools = await session.list_tools()
            assert [tool.name for tool in tools.tools] == ["echo", "status"]

    asyncio.run(scenario())


def test_recorded_registry_pages_are_offline_and_cursor_exact() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    first = json.loads((FIXTURES / "search_page_1.json").read_text())
    second = json.loads((FIXTURES / "search_page_2.json").read_text())
    assert manifest["apiRevision"] == "v0.1"
    for filename, digest in manifest["files"].items():
        assert hashlib.sha256((FIXTURES / filename).read_bytes()).hexdigest() == digest
    assert first["metadata"]["nextCursor"] == "opaque-page-2"
    assert "nextCursor" not in second["metadata"]
    assert all(item["server"]["remotes"] for item in first["servers"] + second["servers"])


def test_selected_sdk_supports_qualified_protocol_revision() -> None:
    assert version("mcp") == "1.29.1"
    assert PROTOCOL_REVISION in SUPPORTED_PROTOCOL_VERSIONS

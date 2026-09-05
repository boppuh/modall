import asyncio
import hashlib
import json
from importlib.metadata import version
from pathlib import Path

import httpx
import pytest
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

from tests.support.mcp_fixture_server import (
    FIXTURE_TOKEN,
    PROTOCOL_REVISION,
    create_mcp_fixture_app,
)

FIXTURES = Path(__file__).parent / "fixtures" / "registry"


def _request(request_id: int, method: str, params: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def test_reference_server_initialization_pagination_drift_and_results() -> None:
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_mcp_fixture_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
            initialized = await client.post(
                "/mcp/default",
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
            assert initialized.json()["result"]["protocolVersion"] == PROTOCOL_REVISION
            notification = await client.post(
                "/mcp/default",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert notification.status_code == 202
            page_1 = (
                await client.post("/mcp/drift-v1", json=_request(2, "tools/list", {}))
            ).json()["result"]
            page_2 = (
                await client.post(
                    "/mcp/drift-v1",
                    json=_request(3, "tools/list", {"cursor": page_1["nextCursor"]}),
                )
            ).json()["result"]
            assert [tool["name"] for tool in page_1["tools"] + page_2["tools"]] == [
                "echo",
                "status",
                "unsupported-content",
            ]
            drift = (await client.post("/mcp/drift-v2", json=_request(4, "tools/list", {}))).json()[
                "result"
            ]["tools"][0]
            assert drift["inputSchema"] != page_1["tools"][0]["inputSchema"]
            called = await client.post(
                "/mcp/default",
                json=_request(5, "tools/call", {"name": "echo", "arguments": {"message": "hello"}}),
            )
            assert called.json()["result"]["structuredContent"] == {"message": "hello"}
            unsupported = await client.post(
                "/mcp/default",
                json=_request(6, "tools/call", {"name": "unsupported-content", "arguments": {}}),
            )
            assert unsupported.json()["result"]["content"][0]["type"] == "image"
            failed = await client.post(
                "/mcp/default",
                json=_request(7, "tools/call", {"name": "missing", "arguments": {}}),
            )
            assert failed.json()["result"]["isError"] is True

    asyncio.run(scenario())


def test_reference_server_auth_protocol_and_transport_fault_profiles() -> None:
    async def scenario() -> None:
        transport = httpx.ASGITransport(app=create_mcp_fixture_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
            request = _request(1, "tools/list", {})
            assert (await client.post("/mcp/authenticated", json=request)).status_code == 401
            authenticated = await client.post(
                "/mcp/authenticated",
                json=request,
                headers={"Authorization": f"Bearer {FIXTURE_TOKEN}"},
            )
            assert authenticated.status_code == 200
            mismatch = await client.post(
                "/mcp/protocol-mismatch",
                json=_request(
                    2,
                    "initialize",
                    {"protocolVersion": PROTOCOL_REVISION},
                ),
            )
            assert mismatch.json()["result"]["protocolVersion"] != PROTOCOL_REVISION
            malformed = await client.post("/mcp/malformed", json=request)
            assert malformed.content == b'{"jsonrpc":'
            oversized = await client.post("/mcp/oversized", json=request)
            assert len(oversized.content) > 262_144
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.01):
                    await client.post("/mcp/timeout", json=request)
            with pytest.raises(ConnectionError, match="fixture disconnect"):
                await client.post("/mcp/disconnect", json=request)

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
    assert version("mcp") == "1.27.2"
    assert PROTOCOL_REVISION in SUPPORTED_PROTOCOL_VERSIONS

"""Deterministic legacy MCP Streamable HTTP fixture server."""

import asyncio
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response

PROTOCOL_REVISION = "2025-06-18"
FIXTURE_TOKEN = "fixture-token-not-a-real-secret"


def _tools(profile: str) -> list[dict[str, Any]]:
    description = "Echo bounded text" if profile != "drift-v2" else "Echo bounded text safely"
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"message": {"type": "string", "maxLength": 256}},
        "required": ["message"],
        "additionalProperties": False,
    }
    if profile == "drift-v2":
        schema["properties"]["uppercase"] = {"type": "boolean", "default": False}
    return [
        {
            "name": "echo",
            "title": "Echo",
            "description": description,
            "inputSchema": schema,
            "outputSchema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        },
        {
            "name": "status",
            "title": "Status",
            "description": "Return fixture status",
            "inputSchema": {"type": "object", "additionalProperties": False},
        },
        {
            "name": "unsupported-content",
            "description": "Return an unsupported image block",
            "inputSchema": {"type": "object"},
        },
    ]


def create_mcp_fixture_app() -> FastAPI:
    app = FastAPI()

    @app.post("/mcp/{profile}")
    async def mcp(
        profile: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Response:
        if profile == "authenticated" and authorization != f"Bearer {FIXTURE_TOKEN}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if profile == "disconnect":
            raise ConnectionError("fixture disconnect")
        if profile == "timeout":
            await asyncio.sleep(0.05)
        if profile == "malformed":
            return Response(b'{"jsonrpc":', media_type="application/json")
        if profile == "oversized":
            return JSONResponse({"padding": "x" * 262_145})

        payload = await request.json()
        method = payload.get("method")
        request_id = payload.get("id")
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "initialize":
            requested = payload.get("params", {}).get("protocolVersion")
            revision = "2026-experimental" if profile == "protocol-mismatch" else requested
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": revision,
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {"name": "modall-reference", "version": "1.0.0"},
                    },
                }
            )
        if method == "tools/list":
            tools = _tools(profile)
            cursor = payload.get("params", {}).get("cursor")
            result: dict[str, Any]
            if cursor is None:
                result = {"tools": tools[:2], "nextCursor": "page-2"}
            elif cursor == "page-2":
                result = {"tools": tools[2:]}
            else:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": "invalid cursor"},
                    }
                )
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})
        if method == "tools/call":
            params = payload.get("params", {})
            name = params.get("name")
            arguments = params.get("arguments", {})
            if name == "echo":
                message = arguments.get("message", "")
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "content": [{"type": "text", "text": message}],
                            "structuredContent": {"message": message},
                            "isError": False,
                        },
                    }
                )
            if name == "unsupported-content":
                result = {
                    "content": [{"type": "image", "data": "AA==", "mimeType": "image/png"}],
                    "isError": False,
                }
            else:
                result = {
                    "content": [{"type": "text", "text": "fixture failure detail"}],
                    "isError": True,
                }
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "method not found"},
            }
        )

    return app

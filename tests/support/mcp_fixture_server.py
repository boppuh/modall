"""Deterministic legacy MCP Streamable HTTP fixture server."""

import asyncio
from itertools import count
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

PROTOCOL_REVISION = "2025-06-18"
FIXTURE_TOKEN = "fixture-token-not-a-real-secret"


def _tools(profile: str) -> list[dict[str, Any]]:
    description = (
        "Echo bounded text safely" if profile == "metadata-drift-v2" else "Echo bounded text"
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"message": {"type": "string", "maxLength": 256}},
        "required": ["message"],
        "additionalProperties": False,
    }
    if profile == "schema-drift-v2":
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
        {
            "name": "fail",
            "description": "Return a declared tool execution error",
            "inputSchema": {"type": "object"},
        },
    ]


def create_mcp_fixture_app() -> FastAPI:
    app = FastAPI()
    next_session = count(1)
    drift_generations: dict[str, int] = {}
    sessions: dict[str, tuple[str, str, bool, str]] = {}

    @app.post("/mcp/{profile}")
    async def mcp(
        profile: str,
        request: Request,
        authorization: str | None = Header(default=None),
        accept: str | None = Header(default=None),
        mcp_protocol_version: str | None = Header(default=None),
        mcp_session_id: str | None = Header(default=None),
    ) -> Response:
        if profile == "authenticated" and authorization != f"Bearer {FIXTURE_TOKEN}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        accepted_types = {item.strip() for item in (accept or "").split(",")}
        if not {"application/json", "text/event-stream"}.issubset(accepted_types):
            return JSONResponse({"error": "invalid accept header"}, status_code=406)

        payload = await request.json()
        method = payload.get("method")
        request_id = payload.get("id")
        if method == "initialize":
            requested_revision = payload.get("params", {}).get("protocolVersion")
            if requested_revision != PROTOCOL_REVISION:
                return JSONResponse({"error": "unsupported requested protocol"}, status_code=400)
            revision = "2025-11-25" if profile == "protocol-mismatch" else PROTOCOL_REVISION
            session_id = f"{profile}-session-{next(next_session)}"
            tool_profile = profile
            if profile in {"schema-drift", "metadata-drift"}:
                generation = drift_generations.get(profile, 0) + 1
                drift_generations[profile] = generation
                tool_profile = f"{profile}-v{min(generation, 2)}"
            sessions[session_id] = (profile, revision, False, tool_profile)
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": revision,
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {"name": "modall-reference", "version": "1.0.0"},
                    },
                },
                headers={"Mcp-Session-Id": session_id},
            )
        session = sessions.get(mcp_session_id or "")
        if session is None or session[:2] != (profile, mcp_protocol_version):
            return JSONResponse({"error": "invalid session or protocol header"}, status_code=400)
        if method == "notifications/initialized":
            sessions[mcp_session_id or ""] = (profile, session[1], True, session[3])
            return Response(status_code=202)
        if not session[2]:
            return JSONResponse({"error": "session is not initialized"}, status_code=409)
        if profile == "timeout":
            await asyncio.sleep(0.05)
        if profile == "malformed":
            return Response(b'{"jsonrpc":', media_type="application/json")
        if profile == "oversized":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": [], "padding": "x" * 262_145},
                }
            )
        if profile == "disconnect":

            async def abort_body() -> Any:
                yield b'{"jsonrpc":"2.0","id":'
                raise ConnectionError("fixture disconnect")

            return StreamingResponse(abort_body(), media_type="application/json")
        if method == "tools/list":
            tools = _tools(session[3])
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
            if name == "status":
                result = {
                    "content": [{"type": "text", "text": "fixture healthy"}],
                    "isError": False,
                }
            elif name == "unsupported-content":
                result = {
                    "content": [{"type": "image", "data": "AA==", "mimeType": "image/png"}],
                    "isError": False,
                }
            elif name == "fail":
                result = {
                    "content": [{"type": "text", "text": "fixture failure detail"}],
                    "isError": True,
                }
            else:
                return JSONResponse(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": "unknown tool"},
                    }
                )
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "method not found"},
            }
        )

    return app

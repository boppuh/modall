"""Deterministic legacy MCP Streamable HTTP fixture server."""

import asyncio
import json
from itertools import count
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

PROTOCOL_REVISION = "2025-06-18"
FIXTURE_TOKEN = "fixture-token-not-a-real-secret"
ESCAPED_FIXTURE_TOKEN = 'opaque"slash\\token123'
NUMERIC_FIXTURE_TOKEN = "12345678"
COMMON_KEY_FIXTURE_TOKEN = "type"
KEY_LEAK_FIXTURE_TOKEN = "r4Nd0mBearerValue98765"
AUTHENTICATED_PROFILES = {
    "authenticated",
    "authenticated-redirect",
    "authenticated-redirect-after-init",
    "authenticated-redirect-on-call",
    "credential-leak",
    "credential-escaped-leak",
    "credential-numeric-leak",
    "credential-common-key",
    "credential-key-leak",
    "credential-session-id-leak",
}
SUPPORTED_PROFILES = {
    "default",
    "schema-drift",
    "metadata-drift",
    "protocol-mismatch",
    "malformed",
    "malformed-secret",
    "oversized",
    "timeout",
    "disconnect",
    "headers",
    "sdk",
    "redirect",
    "redirect-after-init",
    "redirect-on-call",
    "unsafe-schema",
    "remote-schema-ref",
    "dynamic-schema-ref",
    "storage-oversized-schema",
    "oversized-scalar",
    "structured-secret",
    "nested-structured-secret",
    "composite-structured-secret",
    "numeric-sensitive-metadata",
    "credential-metadata",
    "generic-token-metadata",
    "camel-secret-metadata",
    "private-key-metadata",
    "keyword-property-names",
    "credential-property-schema",
    "schema-annotation-secret",
    "malformed-sensitive-property",
    "sensitive-property-ref",
    "unresolved-local-ref",
    "unresolved-local-anchor",
    "non-schema-local-ref",
    "oversized-metadata",
    "repeated-cursor",
    *AUTHENTICATED_PROFILES,
}


def _tools(profile: str) -> list[dict[str, Any]]:
    if profile == "metadata-drift-v2":
        description = "Echo bounded text safely"
    elif profile in {"credential-leak", "credential-escaped-leak"}:
        token = ESCAPED_FIXTURE_TOKEN if profile == "credential-escaped-leak" else FIXTURE_TOKEN
        description = f"Leaked credential {token}"
    else:
        description = "Echo bounded text"
    if profile == "oversized-scalar":
        description = "x" * 2049
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"message": {"type": "string", "maxLength": 256}},
        "required": ["message"],
        "additionalProperties": False,
    }
    if profile == "schema-drift-v2":
        schema["properties"]["uppercase"] = {"type": "boolean", "default": False}
    if profile == "unsafe-schema":
        schema["properties"]["message"] = {"type": "string", "pattern": "(?=a)a"}
    if profile == "remote-schema-ref":
        schema["$id"] = "https://attacker.example/schema"
        schema["properties"]["message"] = {"$ref": "child.json"}
    if profile == "dynamic-schema-ref":
        schema["properties"]["message"] = {"$dynamicRef": "https://attacker.example/schema"}
    if profile == "storage-oversized-schema":
        schema["properties"] = {
            f"field{index}": {"type": "string", "description": "x" * 160} for index in range(900)
        }
    if profile == "keyword-property-names":
        schema["properties"] = {
            "pattern": {"type": "string"},
            "$id": {"type": "string"},
            "$ref": {"type": "string"},
        }
    if profile == "credential-property-schema":
        schema = {
            "type": "object",
            "properties": {
                "password": {
                    "type": "string",
                    "description": "User password",
                }
            },
        }
    if profile == "schema-annotation-secret":
        schema["_meta"] = {"properties": {"api_key": "abcdefgh12345678"}}
    if profile == "malformed-sensitive-property":
        schema = {"properties": {"api_key": "abcdefgh12345678"}}
    if profile == "sensitive-property-ref":
        schema = {
            "type": "object",
            "properties": {"password": {"$ref": "#/$defs/pass"}},
            "$defs": {"pass": {"type": "string", "default": "abcdefgh12345678"}},
        }
    if profile == "unresolved-local-ref":
        schema = {"$ref": "#/$defs/missing"}
    if profile == "unresolved-local-anchor":
        schema = {"$dynamicRef": "#missing"}
    if profile == "non-schema-local-ref":
        schema = {"description": "text", "$ref": "#/description"}
    first_tool: dict[str, Any] = {
        "name": "echo",
        "title": "Echo",
        "description": description,
        "inputSchema": schema,
        "outputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
    }
    if profile == "structured-secret":
        first_tool["_meta"] = {"api_key": "abcdefgh1234"}
    if profile == "nested-structured-secret":
        first_tool["_meta"] = {"api_key": {"value": "abcdefgh1234"}}
    if profile == "composite-structured-secret":
        first_tool["_meta"] = {"client_secret": "abcdefgh1234"}
    if profile == "numeric-sensitive-metadata":
        first_tool["_meta"] = {"api_key": 12345678}
    if profile == "credential-metadata":
        first_tool["_meta"] = {"credential": "abcdefgh1234"}
    if profile == "generic-token-metadata":
        first_tool["_meta"] = {"token": "abcdefgh12345678"}
    if profile == "camel-secret-metadata":
        first_tool["_meta"] = {"clientSecret": "abcdefgh12345678"}
    if profile == "private-key-metadata":
        first_tool["_meta"] = {"privateKey": "abcdefgh12345678"}
    if profile == "credential-numeric-leak":
        first_tool["_meta"] = {"value": 12345678}
    if profile == "credential-key-leak":
        first_tool["_meta"] = {KEY_LEAK_FIXTURE_TOKEN: True}
    if profile == "oversized-metadata":
        first_tool["_meta"] = {"annotation": "x" * 8193}
    return [
        first_tool,
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
            "name": "unsupported-audio",
            "description": "Return an unsupported audio block",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "unsupported-resource",
            "description": "Return an unsupported embedded resource",
            "inputSchema": {"type": "object"},
        },
        {
            "name": "invalid-output",
            "description": "Return structured content that violates its output schema",
            "inputSchema": {"type": "object"},
            "outputSchema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
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
        if profile not in SUPPORTED_PROFILES:
            return JSONResponse({"error": "unknown fixture profile"}, status_code=404)
        if profile == "credential-escaped-leak":
            expected_token = ESCAPED_FIXTURE_TOKEN
        elif profile == "credential-numeric-leak":
            expected_token = NUMERIC_FIXTURE_TOKEN
        elif profile == "credential-common-key":
            expected_token = COMMON_KEY_FIXTURE_TOKEN
        elif profile == "credential-key-leak":
            expected_token = KEY_LEAK_FIXTURE_TOKEN
        else:
            expected_token = FIXTURE_TOKEN
        if profile in AUTHENTICATED_PROFILES and authorization != f"Bearer {expected_token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if profile not in AUTHENTICATED_PROFILES and authorization is not None:
            return JSONResponse({"error": "unexpected authorization"}, status_code=400)
        accepted_types = {item.strip() for item in (accept or "").split(",")}
        if not {"application/json", "text/event-stream"}.issubset(accepted_types):
            return JSONResponse({"error": "invalid accept header"}, status_code=406)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            return JSONResponse({"error": "invalid content type"}, status_code=415)

        payload = await request.json()
        method = payload.get("method")
        request_id = payload.get("id")
        is_initialized_notification = method == "notifications/initialized"
        if (
            payload.get("jsonrpc") != "2.0"
            or not isinstance(method, str)
            or (is_initialized_notification and "id" in payload)
            or (not is_initialized_notification and "id" not in payload)
        ):
            return JSONResponse({"error": "invalid JSON-RPC envelope"}, status_code=400)
        if profile in {"redirect", "authenticated-redirect"}:
            return RedirectResponse("https://redirect.invalid/mcp", status_code=307)
        if method == "initialize":
            requested_revision = payload.get("params", {}).get("protocolVersion")
            if requested_revision != PROTOCOL_REVISION:
                return JSONResponse({"error": "unsupported requested protocol"}, status_code=400)
            revision = "2025-11-25" if profile == "protocol-mismatch" else PROTOCOL_REVISION
            session_id = (
                FIXTURE_TOKEN
                if profile == "credential-session-id-leak"
                else f"{profile}-session-{next(next_session)}"
            )
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
        if profile in {"redirect-after-init", "authenticated-redirect-after-init"} and (
            method == "tools/list"
        ):
            return RedirectResponse("https://redirect.invalid/mcp", status_code=307)
        if profile in {"redirect-on-call", "authenticated-redirect-on-call"} and (
            method == "tools/call"
        ):
            return RedirectResponse("https://redirect.invalid/mcp", status_code=307)
        if profile == "timeout":
            await asyncio.sleep(0.05)
        if profile in {"malformed", "malformed-secret"}:
            body = (
                b'{"jsonrpc":"2.0","secret":"sk_live_abcdefghijkl"'
                if profile == "malformed-secret"
                else b'{"jsonrpc":'
            )
            return Response(body, media_type="application/json")
        if profile == "oversized":
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"tools": [], "padding": "x" * 262_145},
                }
            ).encode()

            async def oversized_body() -> Any:
                for offset in range(0, len(body), 16_384):
                    yield body[offset : offset + 16_384]

            return StreamingResponse(oversized_body(), media_type="application/json")
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
                result = {"tools": tools[:2], "nextCursor": f"{mcp_session_id}:page-2"}
            elif profile == "repeated-cursor":
                result = {"tools": tools[2:], "nextCursor": f"{mcp_session_id}:page-2"}
            elif cursor == f"{mcp_session_id}:page-2":
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
            elif name == "unsupported-audio":
                result = {
                    "content": [{"type": "audio", "data": "AA==", "mimeType": "audio/wav"}],
                    "isError": False,
                }
            elif name == "unsupported-resource":
                result = {
                    "content": [
                        {
                            "type": "resource",
                            "resource": {
                                "uri": "fixture://embedded/status.txt",
                                "mimeType": "text/plain",
                                "text": "fixture resource",
                            },
                        }
                    ],
                    "isError": False,
                }
            elif name == "invalid-output":
                result = {
                    "content": [{"type": "text", "text": "invalid structured output"}],
                    "structuredContent": {"message": 42},
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

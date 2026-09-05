"""Pinned MCP SDK wrapper that exposes only Modall-owned discovery types."""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import re2  # type: ignore[import-untyped]
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from modall.mcp_adapter.policy import (
    EndpointPolicy,
    LimitedTransport,
    PinnedHTTPTransport,
    TransportLimits,
)

QUALIFIED_PROTOCOL_REVISION = "2025-06-18"


class DiscoveryError(Exception):
    """Safe discovery failure without upstream content."""


class ProtocolMismatch(DiscoveryError):
    """The endpoint negotiated a protocol revision not qualified by Modall."""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    identity: str
    name: str
    display_name: str
    description: str | None
    input_schema: dict[str, object]
    output_schema: dict[str, object] | None
    metadata_digest: str
    schema_supported: bool
    normalized: dict[str, object]


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    protocol_revision: str
    tools: tuple[ToolDefinition, ...]
    normalized_payload: dict[str, object]
    canonical_bytes: bytes
    canonical_digest: str


class McpClientAdapter:
    def __init__(
        self,
        *,
        endpoint_policy: EndpointPolicy,
        limits: TransportLimits | None = None,
        max_pages: int = 16,
        max_tools: int = 512,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if max_pages < 1 or max_tools < 1:
            raise ValueError("discovery limits must be positive")
        self._endpoint_policy = endpoint_policy
        self._limits = limits or TransportLimits()
        self._max_pages = max_pages
        self._max_tools = max_tools
        self._transport = transport

    async def discover(
        self, endpoint: str, *, bearer_token: bytes | bytearray | None = None
    ) -> DiscoveryResult:
        headers: dict[str, str] = {"Accept-Encoding": "identity"}
        credential_text: str | None = None
        if bearer_token is not None:
            try:
                credential_text = bytes(bearer_token).decode("ascii")
            except UnicodeDecodeError as exc:
                raise DiscoveryError("credential encoding rejected") from exc
            if not credential_text or any(ord(character) < 33 for character in credential_text):
                raise DiscoveryError("credential encoding rejected")
            headers["Authorization"] = f"Bearer {credential_text}"
        try:
            async with asyncio.timeout(self._limits.total_seconds):
                resolution = await self._endpoint_policy.validate(endpoint)
                inner = self._transport or PinnedHTTPTransport(resolution)
                transport = LimitedTransport(inner, self._limits.response_bytes)
                timeout = httpx.Timeout(
                    self._limits.read_seconds,
                    connect=self._limits.connect_seconds,
                )
                client = httpx.AsyncClient(
                    transport=transport,
                    headers=headers,
                    timeout=timeout,
                    follow_redirects=False,
                )
                return await self._discover(client, endpoint, credential_text)
        except (ProtocolMismatch, DiscoveryError):
            raise
        except Exception as exc:
            mismatch = _find_exception(exc, ProtocolMismatch)
            if mismatch is not None:
                raise mismatch from exc
            discovery_error = _find_exception(exc, DiscoveryError)
            if discovery_error is not None:
                raise discovery_error from exc
            raise DiscoveryError("MCP discovery failed") from exc

    async def _discover(
        self, client: httpx.AsyncClient, endpoint: str, credential_text: str | None
    ) -> DiscoveryResult:
        async with (
            client,
            streamable_http_client(endpoint, http_client=client) as (
                read_stream,
                write_stream,
                _,
            ),
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=self._limits.read_seconds),
            ) as session,
        ):
            initialized = await session.send_request(
                types.ClientRequest(
                    types.InitializeRequest(
                        params=types.InitializeRequestParams(
                            protocolVersion=QUALIFIED_PROTOCOL_REVISION,
                            capabilities=types.ClientCapabilities(),
                            clientInfo=types.Implementation(name="modall", version="0.1.0"),
                        )
                    )
                ),
                types.InitializeResult,
            )
            if initialized.protocolVersion != QUALIFIED_PROTOCOL_REVISION:
                raise ProtocolMismatch("MCP protocol revision is not qualified")
            await session.send_notification(
                types.ClientNotification(types.InitializedNotification())
            )
            raw_tools: list[types.Tool] = []
            cursor: str | None = None
            seen_cursors: set[str] = set()
            for _page in range(self._max_pages):
                page = await session.list_tools(cursor=cursor)
                raw_tools.extend(page.tools)
                if len(raw_tools) > self._max_tools:
                    raise DiscoveryError("MCP discovery exceeded tool limit")
                cursor = page.nextCursor
                if cursor is None:
                    break
                if cursor in seen_cursors:
                    raise DiscoveryError("MCP discovery repeated a cursor")
                seen_cursors.add(cursor)
            else:
                raise DiscoveryError("MCP discovery exceeded page limit")

        normalized_tools: list[dict[str, object]] = []
        definitions: list[ToolDefinition] = []
        identities: set[str] = set()
        for tool in raw_tools:
            normalized = _normalize_tool(tool)
            identity = tool.name
            if identity in identities:
                raise DiscoveryError("MCP discovery returned duplicate tool identity")
            identities.add(identity)
            encoded = _canonical_json(normalized)
            metadata_digest = hashlib.sha256(encoded).hexdigest()
            schema_supported = await _schema_support_with_deadline(
                tool.inputSchema, tool.outputSchema
            )
            definitions.append(
                ToolDefinition(
                    identity=identity,
                    name=tool.name,
                    display_name=tool.title or tool.name,
                    description=tool.description,
                    input_schema=dict(tool.inputSchema),
                    output_schema=(
                        dict(tool.outputSchema) if tool.outputSchema is not None else None
                    ),
                    metadata_digest=metadata_digest,
                    schema_supported=schema_supported,
                    normalized=normalized,
                )
            )
            normalized_tools.append(normalized)
        payload: dict[str, object] = {
            "protocolRevision": QUALIFIED_PROTOCOL_REVISION,
            "tools": normalized_tools,
        }
        canonical = _canonical_json(payload)
        if len(canonical) > self._limits.response_bytes:
            raise DiscoveryError("normalized discovery exceeded byte limit")
        text = canonical.decode("utf-8")
        if _contains_obvious_secret(text) or (
            credential_text is not None and _contains_decoded_credential(payload, credential_text)
        ):
            raise DiscoveryError("discovery metadata failed secret screening")
        return DiscoveryResult(
            protocol_revision=QUALIFIED_PROTOCOL_REVISION,
            tools=tuple(definitions),
            normalized_payload=payload,
            canonical_bytes=canonical,
            canonical_digest=hashlib.sha256(canonical).hexdigest(),
        )


def _normalize_tool(tool: types.Tool) -> dict[str, object]:
    value = tool.model_dump(mode="json", by_alias=True, exclude_none=True)
    if not isinstance(value, dict):
        raise DiscoveryError("invalid MCP tool metadata")
    return value


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise DiscoveryError("invalid discovery metadata") from exc


_OBVIOUS_SECRET = re.compile(
    r"(?:sk_live_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:api[_-]?key|access[_-]?token|secret|password)[=:/_-][A-Za-z0-9._~+/=\-]{8,})",
    re.IGNORECASE,
)


def _schema_is_supported(
    input_schema: dict[str, Any], output_schema: dict[str, Any] | None
) -> bool:
    stack: list[tuple[object, int]] = [(input_schema, 1)]
    if output_schema is not None:
        stack.append((output_schema, 1))
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if depth > 32 or nodes > 4096:
            return False
        if isinstance(value, dict):
            if len(value) > 1024:
                return False
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 8192:
                    return False
                if key == "pattern" and (not isinstance(child, str) or len(child) > 512):
                    return False
                if key == "pattern" and isinstance(child, str):
                    try:
                        re2.compile(child)
                    except re2.error:
                        return False
                if key == "patternProperties" and isinstance(child, dict):
                    for pattern in child:
                        if len(pattern) > 512:
                            return False
                        try:
                            re2.compile(pattern)
                        except re2.error:
                            return False
                if key == "$id":
                    return False
                if key == "$schema" and child != "https://json-schema.org/draft/2020-12/schema":
                    return False
                if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (
                    not isinstance(child, str) or not child.startswith("#")
                ):
                    return False
                stack.append((child, depth + 1))
        elif isinstance(value, list):
            if len(value) > 1024:
                return False
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            if len(value) > 8192:
                return False
        elif value is not None and not isinstance(value, (bool, int, float)):
            return False
    try:
        Draft202012Validator.check_schema(input_schema)
        if output_schema is not None:
            Draft202012Validator.check_schema(output_schema)
    except SchemaError:
        return False
    return True


async def _schema_support_with_deadline(
    input_schema: dict[str, Any], output_schema: dict[str, Any] | None
) -> bool:
    try:
        async with asyncio.timeout(0.25):
            return await asyncio.to_thread(_schema_is_supported, input_schema, output_schema)
    except TimeoutError:
        return False


def _contains_obvious_secret(value: str) -> bool:
    return _OBVIOUS_SECRET.search(value) is not None


def _contains_decoded_credential(value: object, credential: str) -> bool:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, child in current.items():
                if credential in key:
                    return True
                stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str) and credential in current:
            return True
    return False


def _find_exception[T: BaseException](error: BaseException, kind: type[T]) -> T | None:
    if isinstance(error, kind):
        return error
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            found = _find_exception(child, kind)
            if found is not None:
                return found
    return None

"""Fail-closed endpoint validation and bounded HTTP response streaming."""

import asyncio
import json
import math
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from ipaddress import ip_address
from socket import AF_UNSPEC, SOCK_STREAM
from typing import ClassVar, cast
from urllib.parse import urlsplit

import httpcore
import httpx

from modall.security.endpoints import normalize_endpoint_host
from modall.security.metadata import (
    contains_obvious_secret,
    contains_sensitive_hostname,
    contains_sensitive_json,
    contains_sensitive_url_path,
    decode_safe_url_path,
)

_SSE_EVENT_BOUNDARY = r"(?:\r\n|\r(?!\n)|(?<!\r)\n){2}"
_SSE_EVENT_BOUNDARY_BYTES = re.compile(rb"(?:\r\n|\r(?!\n)|(?<!\r)\n){2}")


class EndpointPolicyError(Exception):
    """The endpoint cannot be contacted under the active network policy."""


class EndpointResolutionError(Exception):
    """DNS resolution failed before an endpoint-policy decision was possible."""


class ResponseLimitExceeded(Exception):
    """An upstream response exceeded its incremental raw-byte allowance."""


Resolver = Callable[[str, int], Awaitable[set[str]]]


async def system_resolver(host: str, port: int) -> set[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, port, family=AF_UNSPEC, type=SOCK_STREAM)
    return {str(record[4][0]) for record in records}


@dataclass(frozen=True, slots=True)
class TransportLimits:
    response_bytes: int = 262_144
    connect_seconds: float = 3.0
    read_seconds: float = 5.0
    total_seconds: float = 10.0

    def __post_init__(self) -> None:
        if (
            self.response_bytes < 1
            or min(self.connect_seconds, self.read_seconds, self.total_seconds) <= 0
            or not all(
                math.isfinite(value)
                for value in (self.connect_seconds, self.read_seconds, self.total_seconds)
            )
        ):
            raise ValueError("transport limits must be positive")


@dataclass(frozen=True, slots=True)
class EndpointResolution:
    host: str
    port: int
    addresses: tuple[str, ...]


class EndpointPolicy:
    """Validate persisted endpoints before a worker opens a network session."""

    def __init__(
        self,
        *,
        environment: str,
        allow_loopback_http: bool = False,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._environment = environment
        self._allow_loopback_http = allow_loopback_http
        self._resolver = resolver

    async def validate(self, endpoint: str) -> EndpointResolution:
        try:
            parsed = urlsplit(endpoint)
            parsed_port = parsed.port
            port = (
                parsed_port
                if parsed_port is not None
                else (443 if parsed.scheme == "https" else 80)
            )
        except ValueError as exc:
            raise EndpointPolicyError("endpoint rejected") from exc
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.scheme not in {"http", "https"}
            or port == 0
        ):
            raise EndpointPolicyError("endpoint rejected")
        try:
            host = normalize_endpoint_host(parsed.hostname).value
        except ValueError as exc:
            raise EndpointPolicyError("endpoint rejected") from exc
        try:
            decoded_path = decode_safe_url_path(parsed.path)
        except ValueError as exc:
            raise EndpointPolicyError("endpoint rejected") from exc
        canonical_endpoint = f"{parsed.scheme}://{host}:{port}{decoded_path}"
        if (
            contains_obvious_secret(canonical_endpoint)
            or contains_sensitive_hostname(host)
            or contains_sensitive_url_path(decoded_path)
        ):
            raise EndpointPolicyError("endpoint rejected")
        try:
            addresses = await self._resolver(host, port)
        except Exception as exc:
            raise EndpointResolutionError("endpoint resolution failed") from exc
        if not addresses:
            raise EndpointPolicyError("endpoint resolution failed")
        parsed_addresses = []
        try:
            parsed_addresses = [ip_address(address) for address in addresses]
        except ValueError as exc:
            raise EndpointPolicyError("endpoint resolution failed") from exc
        local_fixture = (
            parsed.scheme == "http"
            and self._environment in {"local", "test"}
            and self._allow_loopback_http
            and all(address.is_loopback for address in parsed_addresses)
        )
        if local_fixture:
            return EndpointResolution(host, port, tuple(sorted(addresses)))
        forbidden = any(
            not address.is_global
            or address.is_multicast
            or address.is_loopback
            or address.is_link_local
            or address.is_private
            or address.is_reserved
            or address.is_unspecified
            for address in parsed_addresses
        )
        if parsed.scheme != "https" or forbidden:
            raise EndpointPolicyError("endpoint rejected")
        return EndpointResolution(host, port, tuple(sorted(addresses)))


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect only to IP addresses approved by the endpoint policy."""

    def __init__(
        self,
        resolution: EndpointResolution,
        *,
        inner: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolution = resolution
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host.lower().removesuffix(".") != self._resolution.host or port != self._resolution.port:
            raise EndpointPolicyError("connection target changed after validation")
        last_error: Exception | None = None
        for address in self._resolution.addresses:
            try:
                return await self._inner.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise EndpointPolicyError("endpoint resolution failed")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        del path, timeout, socket_options
        raise EndpointPolicyError("unix sockets are not permitted")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport whose TCP backend cannot re-resolve the approved host."""

    def __init__(
        self,
        resolution: EndpointResolution,
        *,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpcore.default_ssl_context(),
            retries=0,
            network_backend=network_backend or PinnedNetworkBackend(resolution),
        )


class LimitedByteStream(httpx.AsyncByteStream):
    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        budget: "RawByteBudget",
        forbidden_values: tuple[bytes, ...],
        mark_sensitive_response: Callable[[], None],
        media_type: str,
    ) -> None:
        self._stream = stream
        self._budget = budget
        self._mark_sensitive_response = mark_sensitive_response
        self._forbidden_matcher = _IncrementalByteMatcher(forbidden_values)
        self._decoded_forbidden_matcher = _IncrementalByteMatcher(forbidden_values)
        self._escape_decoder = _IncrementalJsonEscapeDecoder()
        self._generic_tail = b""
        self._structured_buffer = bytearray()
        self._buffer_json_document = media_type != "text/event-stream"
        self._sse_scan_from = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            self._budget.consume(len(chunk))
            decoded_chunk = self._escape_decoder.feed(chunk)
            generic_window = self._generic_tail + decoded_chunk
            if (
                self._forbidden_matcher.feed(chunk)
                or self._decoded_forbidden_matcher.feed(decoded_chunk)
                or contains_obvious_secret(generic_window.decode("utf-8", errors="ignore"))
            ):
                self._reject_sensitive_body()
            self._generic_tail = generic_window[-256:]
            self._structured_buffer.extend(chunk)
            if self._buffer_json_document:
                continue
            self._screen_completed_sse_events()
            yield chunk
        decoded_tail = self._escape_decoder.finish()
        if self._decoded_forbidden_matcher.feed(decoded_tail) or contains_obvious_secret(
            (self._generic_tail + decoded_tail).decode("utf-8", errors="ignore")
        ):
            self._reject_sensitive_body()
        if self._buffer_json_document:
            body = bytes(self._structured_buffer)
            if _contains_sensitive_json_document(body):
                self._reject_sensitive_body()
            if body:
                yield body
        elif self._structured_buffer and _contains_sensitive_sse_event(
            bytes(self._structured_buffer)
        ):
            self._reject_sensitive_body()

    def _screen_completed_sse_events(self) -> None:
        consumed = 0
        while match := _SSE_EVENT_BOUNDARY_BYTES.search(
            self._structured_buffer, self._sse_scan_from
        ):
            event = bytes(self._structured_buffer[consumed : match.start()])
            consumed = match.end()
            self._sse_scan_from = consumed
            if _contains_sensitive_sse_event(event):
                self._reject_sensitive_body()
        if consumed:
            del self._structured_buffer[:consumed]
        self._sse_scan_from = max(0, len(self._structured_buffer) - 3)

    def _reject_sensitive_body(self) -> None:
        self._mark_sensitive_response()
        raise EndpointPolicyError("sensitive upstream response body")

    async def aclose(self) -> None:
        buffered = bytes(self._structured_buffer)
        sensitive = (
            _contains_sensitive_json_document(buffered)
            if self._buffer_json_document
            else _contains_sensitive_sse_event(buffered)
        )
        await self._stream.aclose()
        if sensitive:
            self._reject_sensitive_body()


class _IncrementalByteMatcher:
    """Match fixed byte strings across chunks in linear time."""

    def __init__(self, patterns: tuple[bytes, ...]) -> None:
        self._patterns = tuple(pattern for pattern in patterns if pattern)
        self._prefixes = tuple(self._prefix_table(pattern) for pattern in self._patterns)
        self._states = [0] * len(self._patterns)

    @staticmethod
    def _prefix_table(pattern: bytes) -> tuple[int, ...]:
        prefix = [0] * len(pattern)
        matched = 0
        for index in range(1, len(pattern)):
            while matched and pattern[index] != pattern[matched]:
                matched = prefix[matched - 1]
            if pattern[index] == pattern[matched]:
                matched += 1
            prefix[index] = matched
        return tuple(prefix)

    def feed(self, value: bytes) -> bool:
        for byte in value:
            for index, pattern in enumerate(self._patterns):
                matched = self._states[index]
                while matched and byte != pattern[matched]:
                    matched = self._prefixes[index][matched - 1]
                if byte == pattern[matched]:
                    matched += 1
                if matched == len(pattern):
                    return True
                self._states[index] = matched
        return False


class _IncrementalJsonEscapeDecoder:
    """Decode visible ASCII JSON escapes without rescanning prior chunks."""

    _SHORT_ESCAPES: ClassVar[dict[int, int]] = {
        ord('"'): ord('"'),
        ord("\\"): ord("\\"),
        ord("/"): ord("/"),
        ord("b"): 8,
        ord("f"): 12,
        ord("n"): 10,
        ord("r"): 13,
        ord("t"): 9,
    }

    def __init__(self) -> None:
        self._pending = bytearray()

    def feed(self, value: bytes) -> bytes:
        decoded = bytearray()
        for byte in value:
            if not self._pending:
                if byte == ord("\\"):
                    self._pending.append(byte)
                else:
                    decoded.append(byte)
                continue
            self._pending.append(byte)
            if len(self._pending) == 2:
                escaped = self._pending[1]
                if escaped in self._SHORT_ESCAPES:
                    decoded.append(self._SHORT_ESCAPES[escaped])
                    self._pending.clear()
                elif escaped != ord("u"):
                    decoded.extend(self._pending)
                    self._pending.clear()
                continue
            if len(self._pending) == 6:
                escape = bytes(self._pending)
                try:
                    character = int(escape[4:6], 16)
                except ValueError:
                    character = 256
                if escape[2:4] == b"00" and 0 <= character <= 127:
                    decoded.append(character)
                else:
                    decoded.extend(escape)
                self._pending.clear()
        return bytes(decoded)

    def finish(self) -> bytes:
        pending = bytes(self._pending)
        self._pending.clear()
        return pending


def _contains_sensitive_structured_response(value: bytes) -> bool:
    """Screen complete JSON documents and completed SSE JSON events."""

    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if _contains_sensitive_json_text(text):
        return True
    completed_events = re.split(_SSE_EVENT_BOUNDARY, text)
    if not re.search(f"{_SSE_EVENT_BOUNDARY}\\Z", text):
        completed_events.pop()
    return any(_contains_sensitive_sse_event(event.encode()) for event in completed_events)


class _DuplicateJsonMember(ValueError):
    pass


def _reject_duplicate_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, child in pairs:
        if key in value:
            raise _DuplicateJsonMember
        value[key] = child
    return value


def _contains_sensitive_json_text(value: str) -> bool:
    decoder = json.JSONDecoder(object_pairs_hook=_reject_duplicate_members)
    index = 0
    while True:
        while index < len(value) and value[index].isspace():
            index += 1
        if index == len(value):
            return False
        try:
            parsed, index = decoder.raw_decode(value, index)
            if contains_sensitive_json(parsed):
                return True
        except _DuplicateJsonMember:
            return True
        except RecursionError:
            return False
        except json.JSONDecodeError as exc:
            next_object = value.find("{", max(index + 1, exc.pos + 1))
            next_array = value.find("[", max(index + 1, exc.pos + 1))
            candidates = [position for position in (next_object, next_array) if position >= 0]
            if not candidates:
                return False
            index = min(candidates)


def _contains_sensitive_json_document(value: bytes) -> bool:
    try:
        return _contains_sensitive_json_text(value.decode("utf-8"))
    except UnicodeDecodeError:
        return False


def _contains_sensitive_sse_event(event: bytes) -> bool:
    try:
        text = event.decode("utf-8")
    except UnicodeDecodeError:
        return False
    lines = text.splitlines()
    field_values = [line.partition(":")[2].lstrip() for line in lines if ":" in line]
    complete_field_members = [f"{{{line}}}" for line in lines if ":" in line]
    data_lines = [line.partition(":")[2].lstrip() for line in lines if line.startswith("data:")]
    return (
        contains_obvious_secret(text)
        or any(_contains_sensitive_json_text(line) for line in lines)
        or any(_contains_sensitive_json_text(value) for value in field_values)
        or any(_contains_sensitive_json_text(value) for value in complete_field_members)
        or (bool(data_lines) and _contains_sensitive_json_text("\n".join(data_lines)))
    )


class RawByteBudget:
    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def consume(self, size: int) -> None:
        self.remaining -= size
        if self.remaining < 0:
            raise ResponseLimitExceeded("upstream response exceeded byte limit")


class LimitedTransport(httpx.AsyncBaseTransport):
    """Apply one raw byte budget across every response in a client session."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        response_bytes: int,
        *,
        forbidden_response_values: tuple[str, ...] = (),
    ) -> None:
        self._inner = inner
        self._budget = RawByteBudget(response_bytes)
        self._forbidden_response_values = forbidden_response_values
        self._forbidden_response_bytes = tuple(
            value.encode("utf-8") for value in forbidden_response_values
        )
        self._sensitive_response_detected = False

    @property
    def sensitive_response_detected(self) -> bool:
        return self._sensitive_response_detected

    def _mark_sensitive_response(self) -> None:
        self._sensitive_response_detected = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        content_encoding = response.headers.get("content-encoding", "identity").lower()
        if content_encoding != "identity":
            await response.aclose()
            raise ResponseLimitExceeded("encoded upstream responses are not accepted")
        if any(
            forbidden in header_name or forbidden in header_value
            for header_name, header_value in response.headers.multi_items()
            for forbidden in self._forbidden_response_values
        ) or any(
            contains_obvious_secret(header_name)
            or contains_obvious_secret(header_value)
            or _contains_sensitive_json_text(header_name)
            or _contains_sensitive_json_text(header_value)
            for header_name, header_value in response.headers.multi_items()
        ):
            self._mark_sensitive_response()
            await response.aclose()
            raise EndpointPolicyError("sensitive upstream response header")
        reason_phrase = response.reason_phrase
        if (
            contains_obvious_secret(reason_phrase)
            or any(forbidden in reason_phrase for forbidden in self._forbidden_response_values)
            or _contains_sensitive_json_text(reason_phrase)
        ):
            self._mark_sensitive_response()
            await response.aclose()
            raise EndpointPolicyError("sensitive upstream response status")
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
                if declared_size < 0 or declared_size > self._budget.remaining:
                    await response.aclose()
                    raise ResponseLimitExceeded("upstream response exceeded byte limit")
            except ValueError as exc:
                await response.aclose()
                raise EndpointPolicyError("invalid upstream content length") from exc
        media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        screened_response = httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=LimitedByteStream(
                cast(httpx.AsyncByteStream, response.stream),
                self._budget,
                self._forbidden_response_bytes,
                self._mark_sensitive_response,
                media_type,
            ),
            extensions=response.extensions,
            request=request,
        )
        if media_type != "text/event-stream" or response.status_code != 200:
            try:
                await screened_response.aread()
            except Exception:
                await screened_response.aclose()
                raise
        return screened_response

    async def aclose(self) -> None:
        await self._inner.aclose()

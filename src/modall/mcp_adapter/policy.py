"""Fail-closed endpoint validation and bounded HTTP response streaming."""

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from ipaddress import ip_address
from socket import AF_UNSPEC, SOCK_STREAM
from typing import cast
from urllib.parse import urlsplit

import httpcore
import httpx

from modall.security.endpoints import normalize_endpoint_host
from modall.security.metadata import contains_obvious_secret


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
    ) -> None:
        self._stream = stream
        self._budget = budget
        self._forbidden_values = forbidden_values
        self._mark_sensitive_response = mark_sensitive_response
        self._tail = b""
        self._tail_limit = max((len(value) for value in forbidden_values), default=0) * 6 + 256

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            self._budget.consume(len(chunk))
            window = self._tail + chunk
            decoded_window = _decode_visible_json_escapes(window)
            if any(
                value in decoded_window for value in self._forbidden_values
            ) or contains_obvious_secret(decoded_window.decode("utf-8", errors="ignore")):
                self._mark_sensitive_response()
                raise EndpointPolicyError("sensitive upstream response body")
            self._tail = window[-self._tail_limit :]
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


def _decode_visible_json_escapes(value: bytes) -> bytes:
    """Decode one JSON escape layer for ASCII credential screening."""

    decoded = bytearray()
    index = 0
    short_escapes = {
        ord('"'): ord('"'),
        ord("\\"): ord("\\"),
        ord("/"): ord("/"),
        ord("b"): 8,
        ord("f"): 12,
        ord("n"): 10,
        ord("r"): 13,
        ord("t"): 9,
    }
    while index < len(value):
        if value[index] == ord("\\") and index + 1 < len(value):
            escaped = value[index + 1]
            if escaped in short_escapes:
                decoded.append(short_escapes[escaped])
                index += 2
                continue
            if (
                index + 5 < len(value)
                and escaped == ord("u")
                and value[index + 2 : index + 4] == b"00"
            ):
                try:
                    character = int(value[index + 4 : index + 6], 16)
                except ValueError:
                    pass
                else:
                    if 0 <= character <= 127:
                        decoded.append(character)
                        index += 6
                        continue
        decoded.append(value[index])
        index += 1
    return bytes(decoded)


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
        ):
            self._mark_sensitive_response()
            await response.aclose()
            raise EndpointPolicyError("sensitive upstream response header")
        reason_phrase = response.reason_phrase
        if contains_obvious_secret(reason_phrase) or any(
            forbidden in reason_phrase for forbidden in self._forbidden_response_values
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
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=LimitedByteStream(
                cast(httpx.AsyncByteStream, response.stream),
                self._budget,
                self._forbidden_response_bytes,
                self._mark_sensitive_response,
            ),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()

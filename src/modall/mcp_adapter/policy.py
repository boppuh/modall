"""Fail-closed endpoint validation and bounded HTTP response streaming."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from ipaddress import ip_address
from socket import AF_UNSPEC, SOCK_STREAM
from typing import cast
from urllib.parse import urlsplit

import httpcore
import httpx

from modall.security.endpoints import normalize_endpoint_host


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
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise EndpointPolicyError("endpoint rejected") from exc
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.scheme not in {"http", "https"}
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
    def __init__(self, stream: httpx.AsyncByteStream, limit: int) -> None:
        self._stream = stream
        self._limit = limit

    async def __aiter__(self) -> AsyncIterator[bytes]:
        consumed = 0
        async for chunk in self._stream:
            consumed += len(chunk)
            if consumed > self._limit:
                raise ResponseLimitExceeded("upstream response exceeded byte limit")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class LimitedTransport(httpx.AsyncBaseTransport):
    """Apply a raw byte limit even when the response omits Content-Length."""

    def __init__(self, inner: httpx.AsyncBaseTransport, response_bytes: int) -> None:
        self._inner = inner
        self._response_bytes = response_bytes

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        content_encoding = response.headers.get("content-encoding", "identity").lower()
        if content_encoding != "identity":
            await response.aclose()
            raise ResponseLimitExceeded("encoded upstream responses are not accepted")
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > self._response_bytes:
                    await response.aclose()
                    raise ResponseLimitExceeded("upstream response exceeded byte limit")
            except ValueError as exc:
                await response.aclose()
                raise EndpointPolicyError("invalid upstream content length") from exc
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=LimitedByteStream(
                cast(httpx.AsyncByteStream, response.stream), self._response_bytes
            ),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()

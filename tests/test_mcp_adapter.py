import asyncio
import gzip
from collections.abc import Awaitable, Callable, Iterable
from socket import gaierror

import httpcore
import httpx
import pytest

from modall.mcp_adapter.client import (
    CredentialError,
    DiscoveryError,
    McpClientAdapter,
    ProtocolMismatch,
)
from modall.mcp_adapter.policy import (
    EndpointPolicy,
    EndpointPolicyError,
    EndpointResolution,
    EndpointResolutionError,
    LimitedTransport,
    PinnedHTTPTransport,
    PinnedNetworkBackend,
    ResponseLimitExceeded,
    TransportLimits,
)
from tests.support.mcp_fixture_server import (
    COMMON_KEY_FIXTURE_TOKEN,
    ESCAPED_FIXTURE_TOKEN,
    FIXTURE_TOKEN,
    NUMERIC_FIXTURE_TOKEN,
    create_mcp_fixture_app,
)


async def loopback_resolver(host: str, port: int) -> set[str]:
    del host, port
    return {"8.8.8.8"}


def adapter_for(
    profile: str,
    *,
    app: object | None = None,
    max_pages: int = 16,
    max_tools: int = 512,
    limits: TransportLimits | None = None,
) -> tuple[McpClientAdapter, str]:
    fixture_app = app or create_mcp_fixture_app()
    return (
        McpClientAdapter(
            endpoint_policy=EndpointPolicy(
                environment="test",
                resolver=loopback_resolver,
            ),
            limits=limits,
            max_pages=max_pages,
            max_tools=max_tools,
            transport=httpx.ASGITransport(app=fixture_app),  # type: ignore[arg-type]
        ),
        f"https://fixture/mcp/{profile}",
    )


def test_adapter_discovers_bounded_domain_types_and_drift() -> None:
    async def scenario() -> None:
        client, endpoint = adapter_for("default")
        discovered = await client.discover(endpoint)
        assert discovered.protocol_revision == "2025-06-18"
        assert len(discovered.tools) == 7
        assert discovered.tools[0].name == "echo"
        assert discovered.tools[0].schema_supported is True
        assert len(discovered.canonical_digest) == 64
        assert discovered.canonical_bytes.startswith(b'{"protocolRevision"')

        app = create_mcp_fixture_app()
        first_client, schema_endpoint = adapter_for("schema-drift", app=app)
        second_client, _ = adapter_for("schema-drift", app=app)
        schema_v1 = await first_client.discover(schema_endpoint)
        schema_v2 = await second_client.discover(schema_endpoint)
        assert schema_v1.tools[0].input_schema != schema_v2.tools[0].input_schema

        first_client, metadata_endpoint = adapter_for("metadata-drift", app=app)
        second_client, _ = adapter_for("metadata-drift", app=app)
        metadata_v1 = await first_client.discover(metadata_endpoint)
        metadata_v2 = await second_client.discover(metadata_endpoint)
        assert metadata_v1.tools[0].input_schema == metadata_v2.tools[0].input_schema
        assert metadata_v1.tools[0].metadata_digest != metadata_v2.tools[0].metadata_digest

        for profile in (
            "unsafe-schema",
            "remote-schema-ref",
            "dynamic-schema-ref",
            "unresolved-local-ref",
            "unresolved-local-anchor",
            "non-schema-local-ref",
        ):
            unsafe_client, unsafe_endpoint = adapter_for(profile)
            unsafe = await unsafe_client.discover(unsafe_endpoint)
            assert unsafe.tools[0].schema_supported is False

        keyword_client, keyword_endpoint = adapter_for("keyword-property-names")
        keyword_names = await keyword_client.discover(keyword_endpoint)
        assert keyword_names.tools[0].schema_supported is True

        credential_property, endpoint = adapter_for("credential-property-schema")
        credential_property_result = await credential_property.discover(endpoint)
        assert credential_property_result.tools[0].schema_supported is True

    asyncio.run(scenario())


def test_adapter_fails_closed_on_protocol_limits_faults_and_secret_echo() -> None:
    async def scenario() -> None:
        mismatch, endpoint = adapter_for("protocol-mismatch")
        with pytest.raises(ProtocolMismatch):
            await mismatch.discover(endpoint)

        for profile in ("oversized", "malformed", "timeout", "disconnect"):
            client, endpoint = adapter_for(
                profile,
                limits=TransportLimits(read_seconds=0.02, total_seconds=0.2),
            )
            with pytest.raises(DiscoveryError):
                await client.discover(endpoint)

        repeated, endpoint = adapter_for("repeated-cursor")
        with pytest.raises(DiscoveryError, match="repeated a cursor"):
            await repeated.discover(endpoint)

        page_limited, endpoint = adapter_for("default", max_pages=1)
        with pytest.raises(DiscoveryError, match="page limit"):
            await page_limited.discover(endpoint)

        tool_limited, endpoint = adapter_for("default", max_tools=1)
        with pytest.raises(DiscoveryError, match="tool limit"):
            await tool_limited.discover(endpoint)

        leaking, endpoint = adapter_for("credential-leak")
        with pytest.raises(DiscoveryError, match="secret screening"):
            await leaking.discover(endpoint, bearer_token=FIXTURE_TOKEN.encode())

        escaped_leaking, endpoint = adapter_for("credential-escaped-leak")
        with pytest.raises(DiscoveryError, match="secret screening"):
            await escaped_leaking.discover(endpoint, bearer_token=ESCAPED_FIXTURE_TOKEN.encode())

        numeric_leaking, endpoint = adapter_for("credential-numeric-leak")
        with pytest.raises(CredentialError, match="credential encoding"):
            await numeric_leaking.discover(endpoint, bearer_token=NUMERIC_FIXTURE_TOKEN.encode())

        common_key, endpoint = adapter_for("credential-common-key")
        result = await common_key.discover(endpoint, bearer_token=COMMON_KEY_FIXTURE_TOKEN.encode())
        assert result.tools

        for profile in (
            "structured-secret",
            "nested-structured-secret",
            "composite-structured-secret",
            "numeric-sensitive-metadata",
        ):
            structured, endpoint = adapter_for(profile)
            with pytest.raises(DiscoveryError, match="secret screening"):
                await structured.discover(endpoint)

        oversized_scalar, endpoint = adapter_for("oversized-scalar")
        with pytest.raises(DiscoveryError, match="invalid discovery metadata"):
            await oversized_scalar.discover(endpoint)

        oversized_metadata, endpoint = adapter_for("oversized-metadata")
        with pytest.raises(DiscoveryError, match="invalid discovery metadata"):
            await oversized_metadata.discover(endpoint)

        local_http, _ = adapter_for("authenticated")
        with pytest.raises(DiscoveryError, match="credentials require HTTPS"):
            await local_http.discover(
                "http://127.0.0.1/mcp/authenticated",
                bearer_token=FIXTURE_TOKEN.encode(),
            )

        invalid_credential, endpoint = adapter_for("authenticated")
        with pytest.raises(CredentialError, match="credential encoding"):
            await invalid_credential.discover(endpoint, bearer_token=b"bad token")
        with pytest.raises(CredentialError, match="credential encoding"):
            await invalid_credential.discover(endpoint, bearer_token=b"bad\x7ftoken")

    asyncio.run(scenario())


def test_endpoint_policy_rejects_unsafe_resolution_and_scheme_combinations() -> None:
    ResolverFactory = Callable[[str, int], Awaitable[set[str]]]

    def resolver(addresses: set[str]) -> ResolverFactory:
        async def resolve(host: str, port: int) -> set[str]:
            del host, port
            return addresses

        return resolve

    async def scenario() -> None:
        public = EndpointPolicy(environment="production", resolver=resolver({"8.8.8.8"}))
        await public.validate("https://mcp.example/tools")
        trailing_dot = await public.validate("https://mcp.example./tools")
        assert trailing_dot.host == "mcp.example"
        unicode_host = await public.validate("https://faß.de/tools")
        assert unicode_host.host == "xn--fa-hia.de"
        rejected = (
            (public, "http://mcp.example/tools"),
            (public, "https://user@mcp.example/tools"),
            (public, "https://mcp.example/tools?secret=no"),
            (public, "https://mcp.example:0/tools"),
            (
                EndpointPolicy(
                    environment="production", resolver=resolver({"8.8.8.8", "127.0.0.1"})
                ),
                "https://mcp.example/tools",
            ),
            (
                EndpointPolicy(environment="production", resolver=resolver({"224.0.0.1"})),
                "https://mcp.example/tools",
            ),
            (
                EndpointPolicy(environment="production", resolver=resolver(set())),
                "https://mcp.example/tools",
            ),
        )
        for policy, endpoint in rejected:
            with pytest.raises(EndpointPolicyError):
                await policy.validate(endpoint)

        local = EndpointPolicy(
            environment="test", allow_loopback_http=True, resolver=resolver({"127.0.0.1"})
        )
        await local.validate("http://fixture/mcp")
        ipv6_local = EndpointPolicy(
            environment="test", allow_loopback_http=True, resolver=resolver({"::1"})
        )
        ipv6_resolution = await ipv6_local.validate("http://[::1]:8000/mcp")
        assert ipv6_resolution.host == "::1"
        with pytest.raises(EndpointPolicyError):
            await EndpointPolicy(environment="test", resolver=resolver({"127.0.0.1"})).validate(
                "http://fixture/mcp"
            )

    asyncio.run(scenario())


def test_total_timeout_bounds_resolution() -> None:
    async def stalled_resolver(host: str, port: int) -> set[str]:
        del host, port
        await asyncio.sleep(1)
        return {"8.8.8.8"}

    async def scenario() -> None:
        adapter = McpClientAdapter(
            endpoint_policy=EndpointPolicy(environment="production", resolver=stalled_resolver),
            limits=TransportLimits(total_seconds=0.01),
        )
        with pytest.raises(DiscoveryError):
            await adapter.discover("https://mcp.example")

    asyncio.run(scenario())


def test_endpoint_policy_classifies_dns_failure_as_resolution_error() -> None:
    async def failed_resolver(host: str, port: int) -> set[str]:
        del host, port
        raise gaierror("fixture DNS failure")

    async def scenario() -> None:
        policy = EndpointPolicy(environment="production", resolver=failed_resolver)
        with pytest.raises(EndpointResolutionError):
            await policy.validate("https://mcp.example")

    asyncio.run(scenario())


def test_adapter_rejects_schema_above_the_persistence_bound() -> None:
    async def scenario() -> None:
        adapter, endpoint = adapter_for("storage-oversized-schema")
        with pytest.raises(DiscoveryError, match="invalid discovery metadata"):
            await adapter.discover(endpoint)

    asyncio.run(scenario())


def test_unsupported_schema_patterns_do_not_log_remote_content(
    capfd: pytest.CaptureFixture[str],
) -> None:
    async def scenario() -> None:
        adapter, endpoint = adapter_for("unsafe-schema")
        result = await adapter.discover(endpoint)
        assert result.tools[0].schema_supported is False

    asyncio.run(scenario())
    captured = capfd.readouterr()
    assert "(?=a)a" not in captured.err


def test_pinned_network_backend_connects_only_to_approved_addresses() -> None:
    class RecordingBackend(httpcore.AsyncNetworkBackend):
        def __init__(self) -> None:
            self.hosts: list[str] = []

        async def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
        ) -> httpcore.AsyncNetworkStream:
            del port, timeout, local_address, socket_options
            self.hosts.append(host)
            return httpcore.AsyncMockStream([])

        async def connect_unix_socket(
            self,
            path: str,
            timeout: float | None = None,
            socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
        ) -> httpcore.AsyncNetworkStream:
            raise AssertionError((path, timeout, socket_options))

        async def sleep(self, seconds: float) -> None:
            del seconds

    async def scenario() -> None:
        inner = RecordingBackend()
        resolution = EndpointResolution("mcp.example", 443, ("8.8.8.8",))
        backend = PinnedNetworkBackend(resolution, inner=inner)
        await backend.connect_tcp("mcp.example", 443)
        assert inner.hosts == ["8.8.8.8"]
        with pytest.raises(EndpointPolicyError, match="target changed"):
            await backend.connect_tcp("rebound.example", 443)

        http_inner = RecordingBackend()
        http_resolution = EndpointResolution("mcp.example", 80, ("8.8.4.4",))
        http_inner_response = b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n"

        async def response_connect(
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
        ) -> httpcore.AsyncNetworkStream:
            del port, timeout, local_address, socket_options
            http_inner.hosts.append(host)
            return httpcore.AsyncMockStream([http_inner_response])

        http_inner.connect_tcp = response_connect  # type: ignore[method-assign]
        pinned = PinnedNetworkBackend(http_resolution, inner=http_inner)
        async with httpx.AsyncClient(
            transport=PinnedHTTPTransport(http_resolution, network_backend=pinned)
        ) as client:
            response = await client.get("http://mcp.example/status")
        assert response.status_code == 204
        assert http_inner.hosts == ["8.8.4.4"]

    asyncio.run(scenario())


def test_transport_enforces_declared_and_streamed_byte_limits() -> None:
    async def scenario() -> None:
        async def declared(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"Content-Length": "100"}, request=request)

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(declared), 10)
        ) as client:
            with pytest.raises(ResponseLimitExceeded):
                await client.get("https://example.test")

        async def invalid(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"Content-Length": "invalid"}, request=request)

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(invalid), 10)
        ) as client:
            with pytest.raises(EndpointPolicyError):
                await client.get("https://example.test")

        async def encoded(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Encoding": "gzip"},
                content=gzip.compress(b"encoded"),
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(encoded), 10)
        ) as client:
            with pytest.raises(ResponseLimitExceeded):
                await client.get("https://example.test")

        async def streamed(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 11, request=request)

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(streamed), 10)
        ) as client:
            with pytest.raises(ResponseLimitExceeded):
                await client.get("https://example.test")

    asyncio.run(scenario())

    with pytest.raises(ValueError):
        TransportLimits(response_bytes=0)
    with pytest.raises(ValueError):
        McpClientAdapter(endpoint_policy=EndpointPolicy(environment="test"), max_pages=0)

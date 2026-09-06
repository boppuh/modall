import asyncio
import gzip
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from socket import gaierror

import httpcore
import httpx
import pytest

from modall.execution import validation as schema_validation
from modall.mcp_adapter.client import (
    CredentialError,
    DiscoveryError,
    InvocationError,
    InvocationFailureCode,
    InvocationIndeterminate,
    McpClientAdapter,
    ProtocolMismatch,
    _contains_decoded_credential,
    _contains_sensitive_tool,
    _schema_is_supported,
    _suppress_untrusted_sdk_logs,
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
    _contains_sensitive_structured_response,
)
from modall.security.metadata import (
    contains_obvious_secret,
    contains_sensitive_json,
    contains_sensitive_schema,
    contains_sensitive_url,
    validate_capability_scalars,
)
from tests.support.mcp_fixture_server import (
    COMMON_KEY_FIXTURE_TOKEN,
    ESCAPED_FIXTURE_TOKEN,
    FIXTURE_TOKEN,
    KEY_LEAK_FIXTURE_TOKEN,
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
    schema_validation_timeout_seconds: float = 2.0,
    schema_validation_memory_bytes: int = 256 * 1024 * 1024,
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
            schema_validation_timeout_seconds=schema_validation_timeout_seconds,
            schema_validation_memory_bytes=schema_validation_memory_bytes,
            transport=httpx.ASGITransport(app=fixture_app),  # type: ignore[arg-type]
        ),
        f"https://fixture/mcp/case-{profile}",
    )


@pytest.mark.parametrize(
    "field_name",
    (
        "token",
        "refresh_token",
        "sessionToken",
        "clientSecret",
        "authorization",
        "privateKey",
        "api.key",
        "http.authorization",
    ),
)
def test_structured_secret_screen_recognizes_common_field_spellings(
    field_name: str,
) -> None:
    assert contains_sensitive_json({field_name: "abcdefgh12345678"})


def test_structured_secret_screen_inspects_keys_beneath_sensitive_fields() -> None:
    assert contains_sensitive_json({"token": {"abcdefgh12345678": True}})
    assert contains_sensitive_json({"token": {"value": "01234567890123456789"}})


def test_schema_anchor_screen_ignores_anchors_in_instance_examples() -> None:
    assert contains_sensitive_schema(
        {
            "$defs": {
                "credential": {
                    "$anchor": "target",
                    "default": "AbCdEfGhIjKlMnOpQrStUvWx",
                }
            },
            "examples": [{"$anchor": "target", "default": "safe"}],
            "properties": {"password": {"$ref": "#target"}},
        }
    )
    assert contains_sensitive_schema(
        {
            "$defs": {
                "unsafe": {"$anchor": "dup", "default": "AbCdEfGhIjKlMnOpQrStUvWx"},
                "safe": {"$anchor": "dup", "default": "safe"},
            },
            "properties": {"token": {"$ref": "#dup"}},
        }
    )
    assert contains_sensitive_schema(
        {"patternProperties": {"^token$": {"default": "AbCdEfGhIjKlMnOpQrStUvWx"}}}
    )
    assert contains_sensitive_schema(
        {"patternProperties": {"^t[o]ken$": {"default": "AbCdEfGhIjKlMnOpQrStUvWx"}}}
    )


@pytest.mark.parametrize(
    "value",
    (
        {"authentication": "optional"},
        {"authentication": "oauth2_required"},
        {"authentication": {"required": False}},
        {"authentication": {"type": "oauth2_required"}},
        {"tokenExpiration": 1700000000},
        {"token": {"expires_at": 1700000000}},
        {"authentication": {"type": "authorization_code"}},
        {"authentication": {"type": "device_authorization"}},
    ),
)
def test_structured_secret_screen_allows_authentication_status_metadata(
    value: dict[str, object],
) -> None:
    assert contains_sensitive_json(value) is False


@pytest.mark.parametrize(
    "value",
    (
        "metadata token: abcdefgh12345678",
        "https://mcp.example/token/abcdefgh12345678",
        "credential=abcdefgh12345678",
        "Authorization: Bearer abcdefgh12345678",
        "token:\0abcdefgh12345678",
        "token AbCdEfGhIjKlMnOpQrStUvWx",
        'Use token: "AbCdEfGhIjKlMnOpQrStUvWx"',
        "token: 12345678901234567890",
        "token=" + ("a" * 1000) + "=token=AbCdEfGhIjKlMnOpQrStUvWx",
    ),
)
def test_unstructured_secret_screen_recognizes_generic_markers(value: str) -> None:
    assert contains_obvious_secret(value)


@pytest.mark.parametrize(
    "value",
    (
        "password-protected input",
        "api-key-compatible endpoint",
        "secret-management helper",
        "Uses Bearer authentication for requests",
        "Authentication: authorization_code",
        "Authentication: oauth2_required",
    ),
)
def test_unstructured_secret_screen_allows_hyphenated_prose(value: str) -> None:
    assert contains_obvious_secret(value) is False


@pytest.mark.parametrize("value", ("Password: required", "Password: protected"))
def test_unstructured_secret_screen_allows_status_prose(value: str) -> None:
    assert contains_obvious_secret(value) is False


def test_capability_scalar_screen_does_not_join_independent_fields() -> None:
    validate_capability_scalars(
        tool_identity="token",
        tool_name="token",
        display_name="ConfigurationPanel",
        description=None,
        protocol_revision="2025-06-18",
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


def test_adapter_invokes_once_between_fences_and_normalizes_safe_results() -> None:
    async def scenario() -> None:
        client, endpoint = adapter_for("default")
        boundaries: list[str] = []

        async def session_fence() -> None:
            boundaries.append("session")

        async def dispatch_fence() -> None:
            boundaries.append("dispatch")

        result = await client.invoke(
            endpoint,
            tool_name="echo",
            arguments={"message": "hello"},
            output_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
            before_session=session_fence,
            before_dispatch=dispatch_fence,
        )
        assert boundaries == ["session", "dispatch"]
        assert result.payload["structuredContent"] == {"message": "hello"}
        assert result.byte_count > 0
        assert len(result.canonical_digest) == 64

        authenticated, authenticated_endpoint = adapter_for("authenticated")
        authenticated_result = await authenticated.invoke(
            authenticated_endpoint,
            tool_name="echo",
            arguments={"message": "authenticated"},
            output_schema=None,
            bearer_token=FIXTURE_TOKEN.encode(),
            before_session=session_fence,
            before_dispatch=dispatch_fence,
        )
        assert authenticated_result.payload["structuredContent"] == {"message": "authenticated"}
        for invalid_credential in (b"short", b"non-ascii-\xff"):
            invalid_client, invalid_endpoint = adapter_for("authenticated")
            with pytest.raises(InvocationError) as invalid:
                await invalid_client.invoke(
                    invalid_endpoint,
                    tool_name="echo",
                    arguments={"message": "never sent"},
                    output_schema=None,
                    bearer_token=invalid_credential,
                    before_session=session_fence,
                    before_dispatch=dispatch_fence,
                )
            assert invalid.value.code == InvocationFailureCode.PREPARATION_FAILED

        for tool, expected in (
            ("unsupported-content", InvocationFailureCode.UNSUPPORTED_RESULT_CONTENT),
            ("invalid-output", InvocationFailureCode.INVALID_UPSTREAM_OUTPUT),
            ("fail", InvocationFailureCode.TOOL_CALL_FAILED),
            ("rpc-error", InvocationFailureCode.TOOL_CALL_FAILED),
        ):
            rejected, rejected_endpoint = adapter_for("default")
            with pytest.raises(InvocationError) as failure:
                await rejected.invoke(
                    rejected_endpoint,
                    tool_name=tool,
                    arguments={},
                    output_schema=(
                        {
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                            "required": ["message"],
                        }
                        if tool == "invalid-output"
                        else None
                    ),
                    before_session=session_fence,
                    before_dispatch=dispatch_fence,
                )
            assert failure.value.code == expected
            assert failure.value.dispatched is True

        timed, timed_endpoint = adapter_for(
            "timeout-on-call", limits=TransportLimits(read_seconds=0.02, total_seconds=0.2)
        )
        with pytest.raises(InvocationIndeterminate):
            await timed.invoke(
                timed_endpoint,
                tool_name="status",
                arguments={},
                output_schema=None,
                before_session=session_fence,
                before_dispatch=dispatch_fence,
            )

        sensitive_incomplete, sensitive_incomplete_endpoint = adapter_for(
            "sensitive-incomplete-call"
        )
        with pytest.raises(InvocationIndeterminate) as incomplete:
            await sensitive_incomplete.invoke(
                sensitive_incomplete_endpoint,
                tool_name="status",
                arguments={},
                output_schema=None,
                before_session=session_fence,
                before_dispatch=dispatch_fence,
            )
        assert incomplete.value.dispatched is True

        for profile in ("sensitive-complete-call", "sensitive-complete-sse-call"):
            sensitive_complete, sensitive_complete_endpoint = adapter_for(profile)
            with pytest.raises(InvocationError) as complete:
                await sensitive_complete.invoke(
                    sensitive_complete_endpoint,
                    tool_name="status",
                    arguments={},
                    output_schema=None,
                    before_session=session_fence,
                    before_dispatch=dispatch_fence,
                )
            assert type(complete.value) is InvocationError
            assert complete.value.code == InvocationFailureCode.SENSITIVE_RESULT

        teardown, teardown_endpoint = adapter_for(
            "teardown-timeout", limits=TransportLimits(read_seconds=0.2, total_seconds=0.05)
        )
        teardown_result = await teardown.invoke(
            teardown_endpoint,
            tool_name="status",
            arguments={},
            output_schema=None,
            before_session=session_fence,
            before_dispatch=dispatch_fence,
        )
        assert teardown_result.payload["content"] == [{"type": "text", "text": "fixture healthy"}]

        initializing, initializing_endpoint = adapter_for("protocol-mismatch")
        with pytest.raises(InvocationError) as initialization_failure:
            await initializing.invoke(
                initializing_endpoint,
                tool_name="status",
                arguments={},
                output_schema=None,
                before_session=session_fence,
                before_dispatch=dispatch_fence,
            )
        assert (
            initialization_failure.value.code == InvocationFailureCode.SESSION_INITIALIZATION_FAILED
        )

        for profile in ("invalid-call-result", "malformed"):
            malformed, malformed_endpoint = adapter_for(profile)
            with pytest.raises(InvocationError) as malformed_failure:
                await malformed.invoke(
                    malformed_endpoint,
                    tool_name="status",
                    arguments={},
                    output_schema=None,
                    before_session=session_fence,
                    before_dispatch=dispatch_fence,
                )
            assert type(malformed_failure.value) is InvocationError
            assert malformed_failure.value.code == InvocationFailureCode.INVALID_UPSTREAM_OUTPUT

    asyncio.run(scenario())


def test_invocation_uses_configured_output_validation_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[float, int]] = []

    async def validate(
        arguments: object,
        schema: object,
        *,
        timeout_seconds: float,
        memory_limit_bytes: int,
    ) -> schema_validation.SchemaValidationResult:
        del arguments, schema
        observed.append((timeout_seconds, memory_limit_bytes))
        return schema_validation.SchemaValidationResult.VALID

    monkeypatch.setattr(schema_validation, "validate_schema_arguments", validate)

    async def scenario() -> None:
        client, endpoint = adapter_for(
            "default",
            schema_validation_timeout_seconds=0.25,
            schema_validation_memory_bytes=64 * 1024 * 1024,
        )

        async def fence() -> None:
            return None

        await client.invoke(
            endpoint,
            tool_name="echo",
            arguments={"message": "bounded"},
            output_schema={"type": "object"},
            before_session=fence,
            before_dispatch=fence,
        )

    asyncio.run(scenario())
    assert observed == [(0.25, 64 * 1024 * 1024)]


def test_post_response_validation_timeout_is_a_definitive_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def validate(*args: object, **kwargs: object) -> schema_validation.SchemaValidationResult:
        del args, kwargs
        await asyncio.sleep(1.1)
        raise AssertionError("unreachable")

    monkeypatch.setattr(schema_validation, "validate_schema_arguments", validate)

    async def scenario() -> None:
        client, endpoint = adapter_for(
            "default", limits=TransportLimits(read_seconds=1.0, total_seconds=1.0)
        )

        async def fence() -> None:
            return None

        with pytest.raises(InvocationError) as failure:
            await client.invoke(
                endpoint,
                tool_name="echo",
                arguments={"message": "bounded"},
                output_schema={"type": "object"},
                before_session=fence,
                before_dispatch=fence,
            )
        assert type(failure.value) is InvocationError
        assert failure.value.code == InvocationFailureCode.INVALID_UPSTREAM_OUTPUT

    asyncio.run(scenario())


def test_schema_qualification_honors_an_expired_cooperative_deadline() -> None:
    assert not _schema_is_supported(
        {"type": "object"},
        None,
        deadline=time.monotonic() - 1,
    )


def test_adapter_fails_closed_on_protocol_limits_faults_and_secret_echo(
    caplog: pytest.LogCaptureFixture,
) -> None:
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

        malformed_secret, endpoint = adapter_for("malformed-secret")
        with pytest.raises(DiscoveryError):
            await malformed_secret.discover(endpoint)

        repeated, endpoint = adapter_for("repeated-cursor")
        with pytest.raises(DiscoveryError, match="repeated a cursor"):
            await repeated.discover(endpoint)

        page_limited, endpoint = adapter_for("default", max_pages=1)
        with pytest.raises(DiscoveryError, match="page limit"):
            await page_limited.discover(endpoint)

        tool_limited, endpoint = adapter_for("default", max_tools=1)
        with pytest.raises(DiscoveryError, match="tool limit"):
            await tool_limited.discover(endpoint)

        fast_failure_limits = TransportLimits(read_seconds=0.02, total_seconds=0.2)
        leaking, endpoint = adapter_for("credential-leak", limits=fast_failure_limits)
        with pytest.raises(DiscoveryError):
            await leaking.discover(endpoint, bearer_token=FIXTURE_TOKEN.encode())

        escaped_leaking, endpoint = adapter_for(
            "credential-escaped-leak", limits=fast_failure_limits
        )
        with pytest.raises(DiscoveryError):
            await escaped_leaking.discover(endpoint, bearer_token=ESCAPED_FIXTURE_TOKEN.encode())

        numeric_leaking, endpoint = adapter_for("credential-numeric-leak")
        with pytest.raises(DiscoveryError, match="secret screening"):
            await numeric_leaking.discover(endpoint, bearer_token=NUMERIC_FIXTURE_TOKEN.encode())

        common_key, endpoint = adapter_for("credential-common-key")
        with pytest.raises(CredentialError, match="credential encoding"):
            await common_key.discover(endpoint, bearer_token=COMMON_KEY_FIXTURE_TOKEN.encode())

        key_leaking, endpoint = adapter_for("credential-key-leak", limits=fast_failure_limits)
        with pytest.raises(DiscoveryError, match="secret screening"):
            await key_leaking.discover(endpoint, bearer_token=KEY_LEAK_FIXTURE_TOKEN.encode())

        session_leaking, endpoint = adapter_for(
            "credential-session-id-leak", limits=fast_failure_limits
        )
        with pytest.raises(DiscoveryError, match="secret screening rejected metadata"):
            await session_leaking.discover(endpoint, bearer_token=FIXTURE_TOKEN.encode())

        raw_leaking, endpoint = adapter_for("credential-raw-extension", limits=fast_failure_limits)
        with pytest.raises(DiscoveryError, match="secret screening rejected metadata"):
            await raw_leaking.discover(endpoint, bearer_token=FIXTURE_TOKEN.encode())

        unicode_leaking, endpoint = adapter_for(
            "credential-raw-unicode-extension", limits=fast_failure_limits
        )
        with pytest.raises(DiscoveryError, match="secret screening rejected metadata"):
            await unicode_leaking.discover(endpoint, bearer_token=FIXTURE_TOKEN.encode())

        raw_obvious, endpoint = adapter_for("raw-obvious-extension", limits=fast_failure_limits)
        with pytest.raises(DiscoveryError, match="secret screening rejected metadata"):
            await raw_obvious.discover(endpoint)

        raw_whitespace, endpoint = adapter_for(
            "raw-whitespace-extension", limits=fast_failure_limits
        )
        with pytest.raises(DiscoveryError, match="secret screening rejected metadata"):
            await raw_whitespace.discover(endpoint)

        raw_control, endpoint = adapter_for("raw-control-extension", limits=fast_failure_limits)
        with pytest.raises(DiscoveryError, match="secret screening rejected metadata"):
            await raw_control.discover(endpoint)

        raw_structured, endpoint = adapter_for(
            "raw-structured-extension", limits=fast_failure_limits
        )
        with pytest.raises(DiscoveryError, match="secret screening rejected metadata"):
            await raw_structured.discover(endpoint)

        for profile in (
            "structured-secret",
            "nested-structured-secret",
            "composite-structured-secret",
            "numeric-sensitive-metadata",
            "credential-metadata",
            "generic-token-metadata",
            "camel-secret-metadata",
            "private-key-metadata",
        ):
            structured, endpoint = adapter_for(profile)
            with pytest.raises(DiscoveryError, match="secret screening"):
                await structured.discover(endpoint)

        for profile in (
            "schema-annotation-secret",
            "malformed-sensitive-property",
            "sensitive-property-ref",
            "sensitive-property-recursive-ref",
            "sensitive-property-annotation",
        ):
            unsafe_schema, endpoint = adapter_for(profile, limits=fast_failure_limits)
            with pytest.raises(
                DiscoveryError, match=r"secret screening|invalid discovery metadata"
            ):
                await unsafe_schema.discover(endpoint)

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
        for rejected_token in (b"bad token", b"string", b"application/json"):
            with pytest.raises(CredentialError, match="credential encoding"):
                await invalid_credential.discover(endpoint, bearer_token=rejected_token)
        with pytest.raises(CredentialError, match="credential encoding"):
            await invalid_credential.discover(endpoint, bearer_token=b"bad\x7ftoken")

    asyncio.run(scenario())
    assert FIXTURE_TOKEN not in caplog.text
    assert "sk_live_abcdefghijkl" not in caplog.text


def test_adapter_suppresses_untrusted_transport_debug_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    with _suppress_untrusted_sdk_logs():
        for logger_name in ("client", "httpcore.connection", "httpcore.http11", "httpx"):
            logging.getLogger(logger_name).debug("remote header %s", FIXTURE_TOKEN)
    assert FIXTURE_TOKEN not in caplog.text


def test_adapter_revalidates_lease_after_resolution_before_transport_contact() -> None:
    events: list[str] = []

    async def resolver(host: str, port: int) -> set[str]:
        del host, port
        events.append("resolved")
        return {"8.8.8.8"}

    async def contact(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"transport contacted: {request.url}")

    async def reject_stale_lease() -> None:
        events.append("lease-revalidated")
        raise RuntimeError("lease revoked")

    async def scenario() -> None:
        adapter = McpClientAdapter(
            endpoint_policy=EndpointPolicy(environment="test", resolver=resolver),
            transport=httpx.MockTransport(contact),
        )
        with pytest.raises(DiscoveryError, match="MCP discovery failed"):
            await adapter.discover(
                "https://mcp.example/tools",
                before_connect=reject_stale_lease,
            )

    asyncio.run(scenario())
    assert events == ["resolved", "lease-revalidated"]


def test_limited_transport_revalidates_before_every_request() -> None:
    validations = 0
    contacts = 0

    async def validate() -> None:
        nonlocal validations
        validations += 1
        if validations == 2:
            raise RuntimeError("lease revoked")

    async def contact(request: httpx.Request) -> httpx.Response:
        nonlocal contacts
        contacts += 1
        return httpx.Response(204, request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=LimitedTransport(
                httpx.MockTransport(contact),
                100,
                before_request=validate,
            )
        ) as client:
            assert (await client.get("https://example.test/one")).status_code == 204
            with pytest.raises(RuntimeError, match="lease revoked"):
                await client.get("https://example.test/two")

    asyncio.run(scenario())
    assert validations == 2
    assert contacts == 1


@pytest.mark.parametrize(
    ("body", "completed", "failed"),
    (
        (b'{"jsonrpc":"2.0","id":1,"result":{}}', True, False),
        (b'{"jsonrpc":"2.0","id":1,"error":{"code":-1}}', True, True),
        (b'{"jsonrpc":"2.0","id":2,"result":{}}', False, False),
        (b'{"jsonrpc":"2.0","id":2,"error":{"code":-1}}', False, False),
        (b'{"jsonrpc":"2.0","id":true,"result":{}}', False, False),
        (b'{"jsonrpc":', True, False),
        (b"", True, False),
    ),
)
def test_tool_call_response_completion_requires_an_exact_response_id(
    body: bytes, completed: bool, failed: bool
) -> None:
    async def scenario() -> None:
        async def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=body,
                request=request,
            )

        transport = LimitedTransport(httpx.MockTransport(respond), 1024)
        async with httpx.AsyncClient(transport=transport) as client:
            await client.post(
                "https://example.test",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
            )
        assert transport.tool_call_response_completed is completed
        assert transport.tool_call_jsonrpc_error_completed is failed

    asyncio.run(scenario())


def test_decoded_credential_screen_handles_percent_encoded_metadata() -> None:
    assert _contains_decoded_credential(
        {"description": "https://cdn.example/redirect?next=%41bCdEfGhIjKlMnOpQrStUvWx"},
        "AbCdEfGhIjKlMnOpQrStUvWx",
    )
    assert _contains_decoded_credential(
        {"%41bCdEfGhIjKlMnOpQrStUvWx": True},
        "AbCdEfGhIjKlMnOpQrStUvWx",
    )


def test_raw_structured_screen_handles_sse_and_invalid_utf8() -> None:
    sensitive_event = b'data: {"extension":{"token":"AbCdEfGhIjKlMnOpQrStUvWx"}}\n\n'
    assert _contains_sensitive_structured_response(sensitive_event)
    assert _contains_sensitive_structured_response(sensitive_event.replace(b"\n\n", b"\r\r"))
    assert _contains_sensitive_structured_response(
        b': {"token":"AbCdEfGhIjKlMnOpQrStUvWx"}\n'
        b'data: {"jsonrpc":"2.0","result":{"status":"safe"}}\n\n'
    )
    assert _contains_sensitive_structured_response(
        b'"token": "AbCdEfGhIjKlMnOpQrStUvWx"\n'
        b'data: {"jsonrpc":"2.0","result":{"status":"safe"}}\n\n'
    )
    assert _contains_sensitive_structured_response(
        b'{"token":"AbCdEfGhIjKlMnOpQrStUvWx"}\n'
        b'data: {"jsonrpc":"2.0","result":{"status":"safe"}}\n\n'
    )
    assert _contains_sensitive_structured_response(
        b'data: {"token"\r\ndata: :"AbCdEfGhIjKlMnOpQrStUvWx"}\r\n\r\n'
    )
    assert not _contains_sensitive_structured_response(b'data: {"status":"ready"}\n\n')
    assert not _contains_sensitive_structured_response(b"\xff")
    assert _contains_sensitive_structured_response(
        b'{"extension":{"token":"AbCdEfGhIjKlMnOpQrStUvWx"},"extension":{"status":"safe"}}'
    )


def test_url_secret_screen_handles_embedded_and_multiple_markers() -> None:
    assert contains_sensitive_url(
        "See https://cdn.example/token-AbCdEfGhIjKlMnOpQrStUvWx for details"
    )
    assert contains_sensitive_url("https://cdn.example/token-12345678901234567890")
    assert contains_sensitive_url(
        "https://cdn.example/token-aaaaaaaaaaaa;token-AbCdEfGhIjKlMnOpQrStUvWx"
    )
    assert contains_sensitive_url("https://token-AbCdEfGhIjKlMnOpQrStUvWx@cdn.example/path")
    assert contains_sensitive_url("https://cdn.example/path?token-AbCdEfGhIjKlMnOpQrStUvWx")
    assert contains_sensitive_url("https://cdn.example/path?token%3DAbCdEfGhIjKlMnOpQrStUvWx")
    assert contains_sensitive_url("https://cdn.example/path/token%3DAbCdEfGhIjKlMnOpQrStUvWx")
    assert contains_sensitive_url("https://bad_host.example/token-AbCdEfGhIjKlMnOpQrStUvWx")
    assert contains_sensitive_url("https%3A%2F%2Fcdn.example%2Ftoken-AbCdEfGhIjKlMnOpQrStUvWx")
    assert _contains_sensitive_tool(
        {
            "name": "safe-tool",
            "description": "See https://cdn.example/token-AbCdEfGhIjKlMnOpQrStUvWx",
            "inputSchema": {"type": "object"},
        }
    )


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
            (public, "https://token.abcdefgh12345678.example.com/mcp"),
            (public, "https://mcp.example/token-AbCdEfGh12345678"),
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
    class OneByteStream(httpx.AsyncByteStream):
        def __init__(self, content: bytes) -> None:
            self._content = content

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for value in self._content:
                yield bytes((value,))

        async def aclose(self) -> None:
            return None

    class OneChunkStream(httpx.AsyncByteStream):
        def __init__(self, content: bytes) -> None:
            self._content = content

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield self._content

        async def aclose(self) -> None:
            return None

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

        async def paginated(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"123456", request=request)

        shared_transport = LimitedTransport(httpx.MockTransport(paginated), 10)
        async with httpx.AsyncClient(transport=shared_transport) as client:
            assert (await client.get("https://example.test/page-1")).content == b"123456"
            with pytest.raises(ResponseLimitExceeded):
                await client.get("https://example.test/page-2")

        async def sensitive_status(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                extensions={"reason_phrase": b"fixture-token-not-a-real-secret"},
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(
                httpx.MockTransport(sensitive_status),
                100,
                forbidden_response_values=(FIXTURE_TOKEN,),
            )
        ) as client:
            with pytest.raises(EndpointPolicyError, match="response status"):
                await client.get("https://example.test")

        async def sensitive_json_status(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                request=request,
                extensions={"reason_phrase": b'{"token":"AbCdEfGhIjKlMnOpQrStUvWx"}'},
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(sensitive_json_status), 100)
        ) as client:
            with pytest.raises(EndpointPolicyError, match="response status"):
                await client.get("https://example.test")

        async def sensitive_header(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"X-Upstream-State": "token=AbCdEfGhIjKlMnOpQrStUvWx"},
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(sensitive_header), 100)
        ) as client:
            with pytest.raises(EndpointPolicyError, match="response header"):
                await client.get("https://example.test")

        async def sensitive_json_header(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"X-Upstream-State": '{"token":"AbCdEfGhIjKlMnOpQrStUvWx"}'},
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(sensitive_json_header), 100)
        ) as client:
            with pytest.raises(EndpointPolicyError, match="response header"):
                await client.get("https://example.test")

        async def sensitive_url_header(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"X-Upstream-State": "https://cdn.example/token-AbCdEfGhIjKlMnOpQrStUvWx"},
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(sensitive_url_header), 100)
        ) as client:
            with pytest.raises(EndpointPolicyError, match="response header"):
                await client.get("https://example.test")

        async def accepted_sensitive_body(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                headers={"Content-Type": "application/json"},
                content=b'{"token":"AbCdEfGhIjKlMnOpQrStUvWx"}',
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(accepted_sensitive_body), 100)
        ) as client:
            request = client.build_request("POST", "https://example.test")
            with pytest.raises(EndpointPolicyError, match="response body"):
                await client.send(request, stream=True)

        async def accepted_concatenated_body(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                headers={"Content-Type": "application/json"},
                content=b'{}\n{"token":"AbCdEfGhIjKlMnOpQrStUvWx"}',
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(accepted_concatenated_body), 100)
        ) as client:
            request = client.build_request("POST", "https://example.test")
            with pytest.raises(EndpointPolicyError, match="response body"):
                await client.send(request, stream=True)

        async def accepted_prefixed_body(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                headers={"Content-Type": "application/json"},
                content=b'x\n{"token":{"value":"AbCdEfGhIjKlMnOpQrStUvWx"}}',
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(accepted_prefixed_body), 100)
        ) as client:
            request = client.build_request("POST", "https://example.test")
            with pytest.raises(EndpointPolicyError, match="response body"):
                await client.send(request, stream=True)

        async def accepted_member_fragment(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                headers={"Content-Type": "application/json"},
                content=b'"token":{"value":"AbCdEfGhIjKlMnOpQrStUvWx"}',
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(accepted_member_fragment), 100)
        ) as client:
            request = client.build_request("POST", "https://example.test")
            with pytest.raises(EndpointPolicyError, match="response body"):
                await client.send(request, stream=True)

        async def accepted_prefixed_member_fragment(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                headers={"Content-Type": "application/json"},
                content=b'x "token":{"value":"AbCdEfGhIjKlMnOpQrStUvWx"}',
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(accepted_prefixed_member_fragment), 100)
        ) as client:
            request = client.build_request("POST", "https://example.test")
            with pytest.raises(EndpointPolicyError, match="response body"):
                await client.send(request, stream=True)

        async def accepted_invalid_utf8_body(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                headers={"Content-Type": "application/json"},
                content=b'\xff{"token":{"value":"AbCdEfGhIjKlMnOpQrStUvWx"}}',
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(accepted_invalid_utf8_body), 100)
        ) as client:
            request = client.build_request("POST", "https://example.test")
            with pytest.raises(EndpointPolicyError, match="response body"):
                await client.send(request, stream=True)

        async def accepted_recursive_prefix(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                202,
                headers={"Content-Type": "application/json"},
                content=(b"[" * 10_000) + b'{"token":{"value":"AbCdEfGhIjKlMnOpQrStUvWx"}}',
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(accepted_recursive_prefix), 20_000)
        ) as client:
            request = client.build_request("POST", "https://example.test")
            with pytest.raises(EndpointPolicyError, match="response body"):
                await client.send(request, stream=True)

        async def sse(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=OneByteStream(b'data: {"status":"ready"}\r\r'),
                request=request,
            )

        response_less_transport = LimitedTransport(httpx.MockTransport(sse), 100)
        async with httpx.AsyncClient(transport=response_less_transport) as client:
            response = await client.post(
                "https://example.test",
                json={"jsonrpc": "2.0", "id": 7, "method": "tools/call"},
            )
            assert response.content.endswith(b"\r\r")
            assert response_less_transport.tool_call_response_completed is False

        async def json_notification(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/json"},
                content=(
                    b'{"jsonrpc":"2.0","method":"notifications/progress",'
                    b'"params":{"progress":1}}'
                ),
                request=request,
            )

        notification_transport = LimitedTransport(
            httpx.MockTransport(json_notification), 256
        )
        async with httpx.AsyncClient(transport=notification_transport) as client:
            response = await client.post(
                "https://example.test",
                json={"jsonrpc": "2.0", "id": 7, "method": "tools/call"},
            )
            assert response.json()["method"] == "notifications/progress"
            assert notification_transport.tool_call_response_completed is False

        async def sensitive_sse(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=OneByteStream(
                    b'data: {"extension":{"token":"AbCdEfGhIjKlMnOpQrStUvWx"}}\n\n'
                ),
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(sensitive_sse), 100)
        ) as client:
            with pytest.raises(EndpointPolicyError, match="sensitive upstream response body"):
                await client.get("https://example.test")

        async def sensitive_sse_tail(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=OneByteStream(
                    b'data: {"status":"ready"}\n\n: {"token":"AbCdEfGhIjKlMnOpQrStUvWx"}'
                ),
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(sensitive_sse_tail), 100)
        ) as client:
            with pytest.raises(EndpointPolicyError, match="sensitive upstream response body"):
                await client.get("https://example.test")

        async def early_close_sse(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=OneChunkStream(
                    b'data: {"status":"ready"}\n\n: {"token":{"value":"AbCdEfGhIjKlMnOpQrStUvWx"}}'
                ),
                request=request,
            )

        early_close_transport = LimitedTransport(httpx.MockTransport(early_close_sse), 100)
        async with httpx.AsyncClient(transport=early_close_transport) as client:
            request = client.build_request("GET", "https://example.test")
            response = await client.send(request, stream=True)
            assert await anext(response.aiter_raw())
            with pytest.raises(EndpointPolicyError, match="sensitive upstream response body"):
                await response.aclose()
            assert early_close_transport.sensitive_response_detected

        async def many_sse_events(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=OneChunkStream(b"\n\n" * 100_000),
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(httpx.MockTransport(many_sse_events), 262_144)
        ) as client:
            assert len((await client.get("https://example.test")).content) == 200_000

        long_credential = "AbCd" * 1024

        async def long_streamed_credential(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=OneByteStream(long_credential.encode()),
                request=request,
            )

        async with httpx.AsyncClient(
            transport=LimitedTransport(
                httpx.MockTransport(long_streamed_credential),
                8192,
                forbidden_response_values=(long_credential,),
            )
        ) as client:
            with pytest.raises(EndpointPolicyError, match="sensitive upstream response body"):
                await client.get("https://example.test")

    asyncio.run(scenario())

    for invalid_limits in (
        {"response_bytes": 0},
        {"connect_seconds": float("inf")},
        {"read_seconds": float("-inf")},
        {"total_seconds": float("nan")},
    ):
        with pytest.raises(ValueError):
            TransportLimits(**invalid_limits)
    with pytest.raises(ValueError):
        McpClientAdapter(endpoint_policy=EndpointPolicy(environment="test"), max_pages=0)

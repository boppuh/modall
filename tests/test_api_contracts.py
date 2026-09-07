import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modall.api.contracts import (
    ConnectionResponse,
    _decode_audit_cursor,
    _decode_sequence_cursor,
    _encode_audit_cursor,
    _encode_sequence_cursor,
    _run_response,
)
from modall.api.idempotency import idempotent_mutation, purge_expired_api_idempotency
from modall.api.main import create_app
from modall.config import Settings
from modall.execution.service import ExecutionService
from modall.execution.types import ExecutionError, ExecutionFailureCode, HmacKeyVersion
from modall.identity.repository import AuthorizationService
from modall.identity.service import IdentityService
from modall.identity.types import Permission, Principal, Role, WorkspaceContext
from modall.persistence.database import create_engine, create_session_factory, transaction
from modall.persistence.models import (
    ApiIdempotencyRecord,
    AuditEvent,
    Base,
    Capability,
    CapabilityVersion,
    DiscoveryPayload,
    DiscoverySnapshot,
    DiscoverySnapshotCapability,
    McpToolBinding,
    RegistryEntry,
    RegistryEntryVersion,
    Run,
    RunResult,
    ServerConnectionVersion,
    WorkspaceMembership,
)
from modall.registry.official import (
    OfficialRegistryAdapter,
    OfficialRegistryError,
    OfficialRegistryFailureCode,
    OfficialRegistryService,
)
from modall.registry.service import CapabilityService, ConnectionService
from modall.registry.types import CapabilityStatus, RegistrySource


@asynccontextmanager
async def api_client(
    registry_adapter: OfficialRegistryAdapter | None = None,
    settings: Settings | None = None,
) -> AsyncIterator[tuple[httpx.AsyncClient, AsyncEngine, UUID]]:
    engine = create_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection: object, connection_record: object) -> None:
        del connection_record
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    async with transaction(factory) as session:
        user = await IdentityService(session).resolve_user(
            Principal("modall-local", "api-user", "API User")
        )
        workspace = await IdentityService(session).create_workspace(owner=user, name="API")
        workspace_id = workspace.id
    app = create_app(
        settings or Settings(environment="test", local_subject="api-user"),
        readiness_probe=_ready,
        engine=engine,
        registry_adapter=registry_adapter,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, engine, workspace_id
    await engine.dispose()


async def _ready() -> bool:
    return True


def test_control_plane_requires_workspace_and_has_stable_errors() -> None:
    async def scenario() -> None:
        async with api_client() as (client, _engine, workspace_id):
            missing = await client.get("/v1/server-connections")
            assert missing.status_code == 403
            assert missing.json()["error"]["code"] == "access_denied"
            assert missing.headers["cache-control"] == "no-store"
            assert UUID(missing.headers["x-correlation-id"])

            bearer = await client.get(
                "/v1/server-connections",
                headers={
                    "X-Workspace-ID": str(workspace_id),
                    "Authorization": "Bearer forbidden-locally",
                },
            )
            assert bearer.status_code == 401
            assert bearer.json()["error"]["code"] == "authentication_required"

            invalid = await client.get(
                "/v1/server-connections?limit=101",
                headers={
                    "X-Workspace-ID": str(workspace_id),
                    "X-Correlation-ID": "not-a-uuid",
                },
            )
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "invalid_request"

            invalid_cursor = await client.get(
                "/v1/runs?cursor=bad", headers={"X-Workspace-ID": str(workspace_id)}
            )
            assert invalid_cursor.status_code == 422
            invalid_base64_cursor = await client.get(
                f"/v1/runs?cursor={'A' * 21}!", headers={"X-Workspace-ID": str(workspace_id)}
            )
            assert invalid_base64_cursor.status_code == 422

            current = await client.get("/v1/session", headers={"X-Workspace-ID": str(workspace_id)})
            assert current.status_code == 200
            assert current.json()["workspace_id"] == str(workspace_id)
            assert current.json()["role"] == "admin"

            unknown = await client.get(
                f"/v1/capabilities/{uuid4()}",
                headers={"X-Workspace-ID": str(workspace_id)},
            )
            assert unknown.status_code == 403

    asyncio.run(scenario())


def test_browser_origin_can_preflight_workspace_requests() -> None:
    async def scenario() -> None:
        settings = Settings(
            environment="test",
            local_subject="api-user",
            cors_allowed_origins=("http://localhost:5173",),
        )
        async with api_client(settings=settings) as (client, _engine, _workspace_id):
            response = await client.options(
                "/v1/server-connections",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization,x-workspace-id",
                },
            )
            assert response.status_code == 200
            assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
            assert "x-workspace-id" in response.headers["access-control-allow-headers"].lower()
            assert UUID(response.headers["x-correlation-id"])
            assert response.headers["cache-control"] == "no-store"

    asyncio.run(scenario())


def test_specialized_cursors_round_trip_and_reject_malformed_values() -> None:
    identifier = uuid4()
    occurred_at = datetime(2026, 1, 3)
    assert _decode_sequence_cursor(_encode_sequence_cursor(42)) == 42
    assert _decode_audit_cursor(_encode_audit_cursor(occurred_at, identifier)) == (
        occurred_at.replace(tzinfo=UTC),
        identifier,
    )
    with pytest.raises(ValueError, match="invalid cursor"):
        _decode_sequence_cursor("a")
    with pytest.raises(ValueError, match="invalid cursor"):
        _decode_sequence_cursor("eHh4eHh4eHh4eHh4")
    with pytest.raises(ValueError, match="invalid cursor"):
        _decode_audit_cursor("bad")
    with pytest.raises(ValueError, match="invalid cursor"):
        _decode_audit_cursor("!" * 32)


def test_connection_contract_and_required_idempotency_key() -> None:
    async def scenario() -> None:
        async with api_client() as (client, engine, workspace_id):
            headers = {"X-Workspace-ID": str(workspace_id)}
            body = {
                "name": "Fixture server",
                "endpoint_url": "http://127.0.0.1:8765/mcp",
                "policy_version": "v1",
            }
            missing_key = await client.post("/v1/server-connections", headers=headers, json=body)
            assert missing_key.status_code == 422

            created = await client.post(
                "/v1/server-connections",
                headers={**headers, "Idempotency-Key": "connection-create"},
                json=body,
            )
            assert created.status_code == 201
            connection = created.json()
            assert connection["lifecycle"] == "verifying"
            connection_id = connection["id"]

            replayed = await client.post(
                "/v1/server-connections",
                headers={**headers, "Idempotency-Key": "connection-create"},
                json=body,
            )
            assert replayed.status_code == 201
            assert replayed.json()["id"] == connection_id

            conflict = await client.post(
                "/v1/server-connections",
                headers={**headers, "Idempotency-Key": "connection-create"},
                json={**body, "name": "Different"},
            )
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "idempotency_conflict"

            factory = create_session_factory(engine)
            async with transaction(factory) as session:
                record = await session.scalar(select(ApiIdempotencyRecord))
                assert record is not None
                record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            reused_after_expiry = await client.post(
                "/v1/server-connections",
                headers={**headers, "Idempotency-Key": "connection-create"},
                json={**body, "name": "After expiry"},
            )
            assert reused_after_expiry.status_code == 201
            assert reused_after_expiry.json()["id"] != connection_id

            listed = await client.get("/v1/server-connections", headers=headers)
            assert listed.status_code == 200
            assert connection_id in {item["id"] for item in listed.json()["items"]}

            detail = await client.get(f"/v1/server-connections/{connection_id}", headers=headers)
            assert detail.status_code == 200
            assert detail.json()["versions"][0]["sequence"] == 1

            appended = await client.post(
                f"/v1/server-connections/{connection_id}/versions",
                headers={**headers, "Idempotency-Key": "connection-version"},
                json={
                    "endpoint_url": "http://127.0.0.1:8766/mcp",
                    "policy_version": "v1",
                },
            )
            assert appended.status_code == 201
            assert appended.json()["sequence"] == 2

            async with transaction(factory) as session:
                seed_version = await session.scalar(
                    select(ServerConnectionVersion).where(
                        ServerConnectionVersion.connection_id == UUID(connection_id)
                    )
                )
                assert seed_version is not None
                session.add_all(
                    ServerConnectionVersion(
                        workspace_id=workspace_id,
                        connection_id=UUID(connection_id),
                        sequence=sequence,
                        endpoint_url="http://127.0.0.1:8766/mcp",
                        secret_binding_id=None,
                        transport="streamable_http",
                        policy_version="v1",
                        created_by_user_id=seed_version.created_by_user_id,
                    )
                    for sequence in range(3, 104)
                )
            bounded_detail = await client.get(
                f"/v1/server-connections/{connection_id}", headers=headers
            )
            assert len(bounded_detail.json()["versions"]) == 100
            assert bounded_detail.json()["versions_truncated"] is True

            refresh = await client.post(
                f"/v1/server-connections/{connection_id}/verify",
                headers={**headers, "Idempotency-Key": "verify"},
            )
            assert refresh.status_code == 200
            assert refresh.json()["status"] == "queued"

            disabled = await client.post(
                f"/v1/server-connections/{connection_id}/disable",
                headers={**headers, "Idempotency-Key": "disable"},
            )
            assert disabled.status_code == 200
            assert disabled.json()["lifecycle"] == "disabled"

            enabled = await client.post(
                f"/v1/server-connections/{connection_id}/enable",
                headers={**headers, "Idempotency-Key": "enable"},
            )
            assert enabled.status_code == 200
            assert enabled.json()["status"] == "queued"

            async with transaction(factory) as session:
                record = await session.scalar(
                    select(ApiIdempotencyRecord).where(
                        ApiIdempotencyRecord.route == "/v1/server-connections"
                    )
                )
                assert record is not None
                record.key_version = "retired"
            incomplete_history = await client.post(
                "/v1/server-connections",
                headers={**headers, "Idempotency-Key": "new-key"},
                json={**body, "name": "Blocked rotation"},
            )
            assert incomplete_history.status_code == 503
            assert (
                incomplete_history.json()["error"]["code"] == "idempotency_key_history_incomplete"
            )

            async with transaction(factory) as session:
                record = await session.scalar(
                    select(ApiIdempotencyRecord).where(
                        ApiIdempotencyRecord.key_version == "retired"
                    )
                )
                assert record is not None
                record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                await session.flush()
                assert await purge_expired_api_idempotency(session) == 1
                membership = await session.scalar(
                    select(WorkspaceMembership).where(
                        WorkspaceMembership.workspace_id == workspace_id
                    )
                )
                assert membership is not None
                membership.role = Role.VIEWER.value
            denied_replay = await client.post(
                f"/v1/server-connections/{connection_id}/enable",
                headers={**headers, "Idempotency-Key": "enable"},
            )
            assert denied_replay.status_code == 403

    asyncio.run(scenario())


def test_registry_capability_and_audit_read_contracts() -> None:
    async def scenario() -> None:
        async with api_client() as (client, engine, workspace_id):
            headers = {"X-Workspace-ID": str(workspace_id)}
            connection = await client.post(
                "/v1/server-connections",
                headers={**headers, "Idempotency-Key": "seed-connection"},
                json={
                    "name": "Seed",
                    "endpoint_url": "http://127.0.0.1:8765/mcp",
                    "policy_version": "v1",
                },
            )
            connection_id = UUID(connection.json()["id"])
            connection_version_id = UUID(connection.json()["pending_version_id"])
            capability_id = uuid4()
            capability_version_id = uuid4()
            registry_id = uuid4()
            registry_version_id = uuid4()
            newer_capability_version_ids = [uuid4() for _ in range(101)]
            factory = create_session_factory(engine)
            async with transaction(factory) as session:
                session.add_all(
                    (
                        Capability(
                            id=capability_id,
                            workspace_id=workspace_id,
                            connection_id=connection_id,
                            tool_identity="io.modall/test",
                            pending_version_id=capability_version_id,
                            enabled_version_id=None,
                            status=CapabilityStatus.PENDING_REVIEW.value,
                            status_epoch=1,
                        ),
                        CapabilityVersion(
                            id=capability_version_id,
                            workspace_id=workspace_id,
                            capability_id=capability_id,
                            sequence=1,
                            display_name="Test tool",
                            description="Safe metadata",
                            input_schema={"type": "object"},
                            output_schema=None,
                            metadata_digest="a" * 64,
                            schema_supported=True,
                        ),
                        McpToolBinding(
                            capability_version_id=capability_version_id,
                            capability_id=capability_id,
                            workspace_id=workspace_id,
                            connection_id=connection_id,
                            connection_version_id=connection_version_id,
                            tool_name="test",
                            protocol_revision="2025-06-18",
                        ),
                        RegistryEntry(
                            id=registry_id,
                            workspace_id=workspace_id,
                            source=RegistrySource.MANUAL.value,
                            external_id=None,
                            current_version_id=registry_version_id,
                        ),
                        RegistryEntryVersion(
                            id=registry_version_id,
                            workspace_id=workspace_id,
                            registry_entry_id=registry_id,
                            sequence=1,
                            name="Registry tool",
                            description="Description",
                            provenance_digest="b" * 64,
                            source_version=None,
                            source_uri=None,
                            normalized_metadata=None,
                            imported_by_user_id=None,
                        ),
                    )
                )
                for sequence, newer_version_id in enumerate(newer_capability_version_ids, start=2):
                    session.add(
                        CapabilityVersion(
                            id=newer_version_id,
                            workspace_id=workspace_id,
                            capability_id=capability_id,
                            sequence=sequence,
                            display_name=f"Test tool {sequence}",
                            description=None,
                            input_schema={"type": "object"},
                            output_schema=None,
                            metadata_digest=f"{sequence:064x}",
                            schema_supported=True,
                        )
                    )
                    session.add(
                        McpToolBinding(
                            capability_version_id=newer_version_id,
                            capability_id=capability_id,
                            workspace_id=workspace_id,
                            connection_id=connection_id,
                            connection_version_id=connection_version_id,
                            tool_name="test",
                            protocol_revision="2025-06-18",
                        )
                    )

            registry = await client.get("/v1/registry/entries", headers=headers)
            assert registry.status_code == 200
            assert registry.json()["items"][0]["name"] == "Registry tool"

            capabilities = await client.get("/v1/capabilities", headers=headers)
            assert capabilities.status_code == 200
            assert capabilities.json()["items"][0]["status"] == "pending_review"

            capability = await client.get(f"/v1/capabilities/{capability_id}", headers=headers)
            assert capability.status_code == 200
            assert capability.json()["versions"][0]["display_name"] == "Test tool 102"
            assert len(capability.json()["versions"]) == 101
            assert capability.json()["versions_truncated"] is True
            assert capability.json()["observed_in_current_snapshot"] is False
            pending_version = next(
                item
                for item in capability.json()["versions"]
                if item["id"] == str(capability_version_id)
            )
            assert pending_version["display_name"] == "Test tool"
            assert pending_version["connection_version_id"] == str(connection_version_id)

            version = await client.get(
                f"/v1/capability-versions/{capability_version_id}", headers=headers
            )
            assert version.status_code == 200
            assert version.json()["input_schema"] == {"type": "object"}
            assert version.json()["connection_version_id"] == str(connection_version_id)

            disabled = await client.post(
                f"/v1/capability-versions/{capability_version_id}/disable",
                headers={**headers, "Idempotency-Key": "disable-capability"},
            )
            assert disabled.status_code == 200
            assert disabled.json()["status"] == "disabled"

            audit = await client.get("/v1/audit-events?resource_type=capability", headers=headers)
            assert audit.status_code == 200
            assert audit.json()["items"][0]["action"] == "capability.disabled"
            assert "payload" not in audit.json()["items"][0]

            factory = create_session_factory(engine)
            async with transaction(factory) as session:
                events = list(
                    (
                        await session.scalars(
                            select(AuditEvent).where(
                                AuditEvent.workspace_id == workspace_id,
                                AuditEvent.resource_type == "capability",
                            )
                        )
                    ).all()
                )
                disabled_event = events[0]
                disabled_event.occurred_at = datetime(2026, 1, 3, tzinfo=UTC)
                for action, occurred_at in (
                    ("capability.version_recorded", datetime(2026, 1, 1, tzinfo=UTC)),
                    ("capability.enabled", datetime(2026, 1, 2, tzinfo=UTC)),
                ):
                    session.add(
                        AuditEvent(
                            workspace_id=workspace_id,
                            actor_user_id=disabled_event.actor_user_id,
                            action=action,
                            resource_type="capability",
                            resource_id=capability_id,
                            outcome="succeeded",
                            correlation_id=disabled_event.correlation_id,
                            occurred_at=occurred_at,
                        )
                    )

            first_page = await client.get(
                "/v1/audit-events?resource_type=capability&limit=1", headers=headers
            )
            assert first_page.json()["items"][0]["action"] == "capability.disabled"
            next_cursor = first_page.json()["page"]["next_cursor"]
            second_page = await client.get(
                "/v1/audit-events",
                headers=headers,
                params={"resource_type": "capability", "limit": 1, "cursor": next_cursor},
            )
            assert second_page.status_code == 200, f"{next_cursor!r}: {second_page.text}"
            assert second_page.json()["items"][0]["action"] == "capability.enabled"

            naive_time = await client.get(
                "/v1/audit-events?occurred_after=2026-09-06T12:00:00", headers=headers
            )
            assert naive_time.status_code == 422

            filtered_time = await client.get(
                "/v1/audit-events?occurred_after=2020-01-01T00:00:00Z"
                "&occurred_before=2030-01-01T00:00:00Z&outcome=succeeded"
                "&action=capability.disabled",
                headers=headers,
            )
            assert filtered_time.status_code == 200
            assert len(filtered_time.json()["items"]) == 1

            invalid_range = await client.get(
                "/v1/audit-events?occurred_after=2030-01-01T00:00:00Z"
                "&occurred_before=2020-01-01T00:00:00Z",
                headers=headers,
            )
            assert invalid_range.status_code == 422

    asyncio.run(scenario())


def test_official_registry_search_import_and_replay_contract() -> None:
    def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json"},
            json={
                "servers": [
                    {
                        "server": {
                            "name": "io.modall.fixture/weather",
                            "description": "Read public weather observations",
                            "version": "1.2.0",
                            "remotes": [
                                {
                                    "type": "streamable-http",
                                    "url": "https://weather.example/mcp",
                                }
                            ],
                        },
                        "_meta": {
                            "io.modelcontextprotocol.registry/official": {
                                "status": "active",
                                "isLatest": True,
                            }
                        },
                    }
                ],
                "metadata": {"count": 1},
            },
        )

    async def scenario() -> None:
        adapter = OfficialRegistryAdapter(transport=httpx.MockTransport(upstream))
        async with api_client(adapter) as (client, _engine, workspace_id):
            headers = {"X-Workspace-ID": str(workspace_id)}
            searched = await client.post(
                "/v1/registry/searches", headers=headers, json={"query": "weather"}
            )
            assert searched.status_code == 200
            result = searched.json()
            assert result["items"][0]["name"] == "io.modall.fixture/weather"
            assert result["server_observed_at"]

            body = {
                "cache_id": result["cache_id"],
                "provenance_digest": result["items"][0]["provenance_digest"],
            }
            imported = await client.post(
                "/v1/registry/imports",
                headers={**headers, "Idempotency-Key": "registry-import"},
                json=body,
            )
            assert imported.status_code == 201
            assert imported.json()["source"] == "official"

            replayed = await client.post(
                "/v1/registry/imports",
                headers={**headers, "Idempotency-Key": "registry-import"},
                json=body,
            )
            assert replayed.status_code == 201
            assert replayed.json()["id"] == imported.json()["id"]

    asyncio.run(scenario())


def test_unhandled_v1_failure_keeps_safe_response_policy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken_upstream(_: httpx.Request) -> httpx.Response:
        raise RuntimeError("sensitive-upstream-value")

    async def scenario() -> None:
        adapter = OfficialRegistryAdapter(transport=httpx.MockTransport(broken_upstream))
        settings = Settings(
            environment="test",
            local_subject="api-user",
            cors_allowed_origins=("http://localhost:5173",),
        )
        async with api_client(adapter, settings) as (client, _engine, workspace_id):
            response = await client.post(
                "/v1/registry/searches",
                headers={
                    "Origin": "http://localhost:5173",
                    "X-Workspace-ID": str(workspace_id),
                },
                json={"query": "weather"},
            )
            assert response.status_code == 500
            assert response.json()["error"]["code"] == "internal_error"
            assert response.headers["cache-control"] == "no-store"
            assert UUID(response.headers["x-correlation-id"])
            assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
            failure = next(
                record
                for record in caplog.records
                if record.getMessage() == "unhandled_request_failure"
            )
            assert failure.telemetry["exception_type"] == "RuntimeError"  # type: ignore[attr-defined]
            assert failure.telemetry["exception_origin"] == "broken_upstream"  # type: ignore[attr-defined]
            assert "sensitive-upstream-value" not in caplog.text

    asyncio.run(scenario())


async def create_executable_target(engine: AsyncEngine, workspace_id: UUID) -> CapabilityVersion:
    factory = create_session_factory(engine)
    async with transaction(factory) as session:
        user = await IdentityService(session).resolve_user(
            Principal("modall-local", "api-user", "API User")
        )
        context: WorkspaceContext = await AuthorizationService(session).authorize(
            user_id=user.id,
            workspace_id=workspace_id,
            permission=Permission.INVOKE,
        )
        connection = await ConnectionService(session).create(
            context=context,
            name="Run target",
            endpoint_url="https://mcp.example/tools",
            secret_binding_id=None,
            policy_version="v1",
        )
        connection_version_id = connection.pending_version_id
        assert connection_version_id is not None
        generation, control_epoch, _ = await ConnectionService(session).allocate_refresh_generation(
            context=context, connection_id=connection.id
        )
        capabilities = CapabilityService(session)
        version = await capabilities.record_version(
            context=context,
            connection_id=connection.id,
            connection_version_id=connection_version_id,
            expected_control_epoch=control_epoch,
            expected_refresh_generation=generation,
            tool_identity="tools/search",
            tool_name="search",
            display_name="Search",
            description="Search public records",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "maxLength": 100}},
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema=None,
            metadata_digest="c" * 64,
            protocol_revision="2025-06-18",
        )
        payload = DiscoveryPayload(
            workspace_id=workspace_id,
            canonical_digest=connection.id.hex * 2,
            normalized_payload={"tools": []},
            byte_count=2,
        )
        session.add(payload)
        await session.flush()
        snapshot = DiscoverySnapshot(
            workspace_id=workspace_id,
            connection_id=connection.id,
            connection_version_id=connection_version_id,
            payload_id=payload.id,
            generation=generation,
            control_epoch=control_epoch,
            protocol_revision="2025-06-18",
        )
        session.add(snapshot)
        await session.flush()
        session.add(
            DiscoverySnapshotCapability(
                workspace_id=workspace_id,
                connection_id=connection.id,
                connection_version_id=connection_version_id,
                snapshot_id=snapshot.id,
                capability_version_id=version.id,
            )
        )
        await ConnectionService(session).promote_pending(
            context=context,
            connection_id=connection.id,
            expected_version_id=connection_version_id,
            expected_control_epoch=control_epoch,
            expected_refresh_generation=generation,
        )
        connection.current_snapshot_id = snapshot.id
        await session.flush()
        await capabilities.enable(
            context=context,
            capability_id=version.capability_id,
            expected_version_id=version.id,
        )
        return version


def test_run_preflight_create_read_event_and_cancel_contracts() -> None:
    async def scenario() -> None:
        async with api_client() as (client, engine, workspace_id):
            version = await create_executable_target(engine, workspace_id)
            headers = {"X-Workspace-ID": str(workspace_id)}
            arguments = {"query": "weather"}
            preflight = await client.post(
                "/v1/run-preflights",
                headers=headers,
                json={"capability_version_id": str(version.id), "arguments": arguments},
            )
            assert preflight.status_code == 200
            assert preflight.json()["server_observed_at"]
            confirmation = preflight.json()["confirmation_token"]
            correlation_id = uuid4()

            created = await client.post(
                "/v1/runs",
                headers={
                    **headers,
                    "Idempotency-Key": "run-create",
                    "X-Correlation-ID": str(correlation_id),
                },
                json={
                    "capability_version_id": str(version.id),
                    "arguments": arguments,
                    "confirmation_token": confirmation,
                },
            )
            assert created.status_code == 201
            assert created.json()["status"] == "queued"
            assert created.json()["actor_user_id"]
            assert created.json()["arguments_expires_at"]
            assert created.json()["result_expires_at"] is None
            assert created.json()["server_observed_at"]
            assert created.json()["correlation_id"] == str(correlation_id)
            run_id = created.json()["id"]

            factory = create_session_factory(engine)
            async with transaction(factory) as session:
                stored_run = await session.get(Run, UUID(run_id))
                assert stored_run is not None
                direct_response = await _run_response(session, stored_run)
                assert direct_response.id == UUID(run_id)

            result_queries: list[str] = []

            def count_result_queries(
                connection: object,
                cursor: object,
                statement: str,
                parameters: object,
                context: object,
                executemany: bool,
            ) -> None:
                del connection, cursor, parameters, context, executemany
                if "FROM run_results" in statement:
                    result_queries.append(statement)

            event.listen(engine.sync_engine, "before_cursor_execute", count_result_queries)
            listed = await client.get("/v1/runs", headers=headers)
            event.remove(engine.sync_engine, "before_cursor_execute", count_result_queries)
            assert listed.status_code == 200
            assert "arguments" not in listed.json()["items"][0]
            assert "result" not in listed.json()["items"][0]
            assert result_queries == []

            filtered = await client.get("/v1/runs?status=queued", headers=headers)
            assert filtered.status_code == 200
            assert len(filtered.json()["items"]) == 1
            active = await client.get("/v1/runs?active=true", headers=headers)
            assert active.status_code == 200
            assert [item["id"] for item in active.json()["items"]] == [run_id]
            active_with_terminal_status = await client.get(
                "/v1/runs?active=true&status=succeeded", headers=headers
            )
            assert active_with_terminal_status.status_code == 200
            assert active_with_terminal_status.json()["items"] == []
            executable = await client.get("/v1/capabilities?executable=true", headers=headers)
            assert executable.status_code == 200
            assert [item["id"] for item in executable.json()["items"]] == [
                str(version.capability_id)
            ]
            executable_detail = await client.get(
                f"/v1/capabilities/{version.capability_id}", headers=headers
            )
            assert executable_detail.status_code == 200
            assert executable_detail.json()["observed_version_id"] == str(version.id)
            scoped_capability = await client.get(
                "/v1/capabilities",
                headers=headers,
                params={
                    "connection_id": executable.json()["items"][0]["connection_id"],
                    "status": "enabled",
                    "limit": 1,
                },
            )
            assert scoped_capability.status_code == 200
            assert len(scoped_capability.json()["items"]) == 1

            second_arguments = {"query": "forecast"}
            second_preflight = await client.post(
                "/v1/run-preflights",
                headers=headers,
                json={
                    "capability_version_id": str(version.id),
                    "arguments": second_arguments,
                },
            )
            second_created = await client.post(
                "/v1/runs",
                headers={**headers, "Idempotency-Key": "run-create-second"},
                json={
                    "capability_version_id": str(version.id),
                    "arguments": second_arguments,
                    "confirmation_token": second_preflight.json()["confirmation_token"],
                },
            )
            assert second_created.status_code == 201
            factory = create_session_factory(engine)
            async with transaction(factory) as session:
                stored_run = await session.get(Run, UUID(run_id))
                assert stored_run is not None
                for offset in range(99):
                    session.add(
                        Run(
                            id=uuid4(),
                            workspace_id=stored_run.workspace_id,
                            actor_user_id=stored_run.actor_user_id,
                            capability_id=stored_run.capability_id,
                            capability_version_id=stored_run.capability_version_id,
                            connection_id=stored_run.connection_id,
                            connection_version_id=stored_run.connection_version_id,
                            connection_control_epoch=stored_run.connection_control_epoch,
                            capability_status_epoch=stored_run.capability_status_epoch,
                            protocol_revision=stored_run.protocol_revision,
                            status="preparing",
                            arguments={},
                            argument_digest="0" * 64,
                            arguments_expires_at=stored_run.arguments_expires_at,
                            deadline=stored_run.deadline,
                            cancellation_requested=False,
                            safe_error_code=None,
                            created_at=stored_run.created_at - timedelta(seconds=offset + 1),
                            updated_at=stored_run.updated_at,
                            terminal_at=None,
                        )
                    )
            legacy_active = await client.get("/v1/runs?active=true", headers=headers)
            assert legacy_active.status_code == 200
            assert len(legacy_active.json()["items"]) == 50
            assert legacy_active.json()["page"]["next_cursor"] is not None
            active_page = await client.get("/v1/runs?active=true&limit=1", headers=headers)
            assert active_page.status_code == 200
            assert len(active_page.json()["items"]) == 1
            assert active_page.json()["page"]["next_cursor"] is not None
            active_next_page = await client.get(
                "/v1/runs",
                headers=headers,
                params={
                    "active": "true",
                    "limit": 1,
                    "cursor": active_page.json()["page"]["next_cursor"],
                },
            )
            assert active_next_page.status_code == 200
            assert len(active_next_page.json()["items"]) == 1
            assert active_next_page.json()["items"][0]["id"] != active_page.json()["items"][0]["id"]
            active_by_duration = await client.get(
                "/v1/runs?status=queued&min_duration_seconds=0&max_duration_seconds=300",
                headers=headers,
            )
            assert active_by_duration.status_code == 200
            assert {item["id"] for item in active_by_duration.json()["items"]} == {
                run_id,
                second_created.json()["id"],
            }

            fetched = await client.get(f"/v1/runs/{run_id}", headers=headers)
            assert fetched.status_code == 200
            assert fetched.json()["result"] is None

            events = await client.get(f"/v1/runs/{run_id}/events", headers=headers)
            assert events.status_code == 200
            assert events.json()["items"][0]["event_type"] == "admitted"
            invalid_event_cursor = await client.get(
                f"/v1/runs/{run_id}/events?cursor=a", headers=headers
            )
            assert invalid_event_cursor.status_code == 422

            cancelled = await client.post(
                f"/v1/runs/{run_id}/cancel",
                headers={**headers, "Idempotency-Key": "run-cancel"},
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            assert cancelled.json()["safe_error_code"] == "cancelled_before_dispatch"
            filtered_cancelled = await client.get(
                "/v1/runs",
                headers=headers,
                params={
                    "status": "cancelled",
                    "capability_id": str(version.capability_id),
                    "actor_id": cancelled.json()["actor_user_id"],
                    "created_after": "2020-01-01T00:00:00Z",
                    "created_before": "2030-01-01T00:00:00Z",
                    "min_duration_seconds": 0,
                    "max_duration_seconds": 300,
                },
            )
            assert [item["id"] for item in filtered_cancelled.json()["items"]] == [run_id]

            naive_created_after = await client.get(
                "/v1/runs?created_after=2026-09-06T12:00:00", headers=headers
            )
            assert naive_created_after.status_code == 422
            naive_created_before = await client.get(
                "/v1/runs?created_before=2026-09-06T12:00:00", headers=headers
            )
            assert naive_created_before.status_code == 422
            invalid_run_time_range = await client.get(
                "/v1/runs?created_after=2030-01-01T00:00:00Z&created_before=2020-01-01T00:00:00Z",
                headers=headers,
            )
            assert invalid_run_time_range.status_code == 422
            invalid_run_duration_range = await client.get(
                "/v1/runs?min_duration_seconds=10&max_duration_seconds=5",
                headers=headers,
            )
            assert invalid_run_duration_range.status_code == 422

            replayed_cancel = await client.post(
                f"/v1/runs/{run_id}/cancel",
                headers={**headers, "Idempotency-Key": "run-cancel"},
            )
            assert replayed_cancel.status_code == 200
            assert replayed_cancel.json()["id"] == run_id

            retained_until = datetime.now(UTC) + timedelta(minutes=1)
            async with transaction(factory) as session:
                session.add(
                    RunResult(
                        run_id=UUID(run_id),
                        workspace_id=workspace_id,
                        payload={"secret": "redacted"},
                        canonical_digest="d" * 64,
                        byte_count=21,
                        captured_at=datetime.now(UTC),
                        expires_at=retained_until,
                    )
                )
            retained = await client.get(f"/v1/runs/{run_id}", headers=headers)
            assert retained.json()["result"] == {"secret": "redacted"}
            retained_page = await client.get("/v1/runs", headers=headers)
            retained_item = next(
                item for item in retained_page.json()["items"] if item["id"] == run_id
            )
            assert "result" not in retained_item
            assert "result_expires_at" not in retained_item

            expired_at = datetime.now(UTC) - timedelta(seconds=1)
            async with transaction(factory) as session:
                await session.execute(
                    update(RunResult)
                    .where(RunResult.run_id == UUID(run_id))
                    .values(expires_at=expired_at)
                )
            redacted = await client.get(f"/v1/runs/{run_id}", headers=headers)
            assert redacted.json()["result"] is None
            assert redacted.json()["result_expires_at"] == expired_at.isoformat().replace(
                "+00:00", "Z"
            )
            redacted_page = await client.get("/v1/runs", headers=headers)
            redacted_item = next(
                item for item in redacted_page.json()["items"] if item["id"] == run_id
            )
            assert "result" not in redacted_item
            assert "result_expires_at" not in redacted_item

            disabled = await client.post(
                f"/v1/capability-versions/{version.id}/disable",
                headers={**headers, "Idempotency-Key": "run-target-disable"},
            )
            assert disabled.status_code == 200
            enabled = await client.post(
                f"/v1/capability-versions/{version.id}/enable",
                headers={**headers, "Idempotency-Key": "run-target-enable"},
            )
            assert enabled.status_code == 200
            assert enabled.json()["status"] == "enabled"

    asyncio.run(scenario())


def test_retryable_and_internal_service_failures_have_server_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def timeout_search(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OfficialRegistryError(OfficialRegistryFailureCode.TIMEOUT)

    async def persistence_search(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OfficialRegistryError(OfficialRegistryFailureCode.PERSISTENCE_FAILURE)

    async def invalid_search(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OfficialRegistryError(OfficialRegistryFailureCode.INVALID_QUERY)

    async def unavailable_run(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ExecutionError(ExecutionFailureCode.IDEMPOTENCY_KEY_HISTORY_INCOMPLETE)

    async def persistence_run(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ExecutionError(ExecutionFailureCode.PERSISTENCE_FAILURE)

    async def invalid_run(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ExecutionError(ExecutionFailureCode.INVALID_ARGUMENTS)

    async def active_limit_run(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise ExecutionError(ExecutionFailureCode.ACTIVE_RUN_LIMIT)

    async def scenario() -> None:
        async with api_client() as (client, _engine, workspace_id):
            headers = {"X-Workspace-ID": str(workspace_id)}
            monkeypatch.setattr(OfficialRegistryService, "search", timeout_search)
            timeout = await client.post(
                "/v1/registry/searches", headers=headers, json={"query": "weather"}
            )
            assert timeout.status_code == 503
            assert timeout.json()["error"]["code"] == "timeout"

            monkeypatch.setattr(OfficialRegistryService, "search", persistence_search)
            persistence = await client.post(
                "/v1/registry/searches", headers=headers, json={"query": "weather"}
            )
            assert persistence.status_code == 500

            monkeypatch.setattr(OfficialRegistryService, "search", invalid_search)
            invalid = await client.post(
                "/v1/registry/searches", headers=headers, json={"query": "weather"}
            )
            assert invalid.status_code == 422

            monkeypatch.setattr(ExecutionService, "create_run", unavailable_run)
            unavailable = await client.post(
                "/v1/runs",
                headers={**headers, "Idempotency-Key": "unavailable-run"},
                json={
                    "capability_version_id": str(uuid4()),
                    "arguments": {},
                    "confirmation_token": "token",
                },
            )
            assert unavailable.status_code == 503
            assert unavailable.json()["error"]["code"] == "idempotency_key_history_incomplete"

            monkeypatch.setattr(ExecutionService, "create_run", active_limit_run)
            limited = await client.post(
                "/v1/runs",
                headers={**headers, "Idempotency-Key": "limited-run"},
                json={
                    "capability_version_id": str(uuid4()),
                    "arguments": {},
                    "confirmation_token": "token",
                },
            )
            assert limited.status_code == 429
            assert limited.json()["error"]["code"] == "active_run_limit"

            monkeypatch.setattr(ExecutionService, "create_run", persistence_run)
            persistence_failure = await client.post(
                "/v1/runs",
                headers={**headers, "Idempotency-Key": "persistence-run"},
                json={
                    "capability_version_id": str(uuid4()),
                    "arguments": {},
                    "confirmation_token": "token",
                },
            )
            assert persistence_failure.status_code == 500

            monkeypatch.setattr(ExecutionService, "create_run", invalid_run)
            invalid_arguments = await client.post(
                "/v1/runs",
                headers={**headers, "Idempotency-Key": "invalid-run"},
                json={
                    "capability_version_id": str(uuid4()),
                    "arguments": {},
                    "confirmation_token": "token",
                },
            )
            assert invalid_arguments.status_code == 409

    asyncio.run(scenario())


def test_openapi_publishes_every_planned_alpha_route() -> None:
    app = create_app(Settings(environment="test"), readiness_probe=_ready)
    paths = app.openapi()["paths"]
    assert {
        "/v1/registry/searches",
        "/v1/session",
        "/v1/registry/imports",
        "/v1/registry/entries",
        "/v1/server-connections",
        "/v1/server-connections/{connection_id}",
        "/v1/server-connections/{connection_id}/versions",
        "/v1/server-connections/{connection_id}/verify",
        "/v1/server-connections/{connection_id}/refresh",
        "/v1/server-connections/{connection_id}/disable",
        "/v1/server-connections/{connection_id}/enable",
        "/v1/capabilities",
        "/v1/capabilities/{capability_id}",
        "/v1/capability-versions/{capability_version_id}",
        "/v1/capability-versions/{capability_version_id}/enable",
        "/v1/capability-versions/{capability_version_id}/disable",
        "/v1/run-preflights",
        "/v1/runs",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/events",
        "/v1/runs/{run_id}/cancel",
        "/v1/audit-events",
    } <= set(paths)
    assert "429" in paths["/v1/runs"]["post"]["responses"]
    assert all(
        "429" in operation["responses"]
        for path_name, path in paths.items()
        for method, operation in path.items()
        if path_name.startswith("/v1/") and method in {"get", "post", "put", "patch", "delete"}
    )


def test_api_idempotency_rejects_invalid_configuration_before_database_access() -> None:
    async def operation() -> ConnectionResponse:
        raise AssertionError("operation must not run")

    async def scenario() -> None:
        session = cast(AsyncSession, object())
        context = WorkspaceContext(uuid4(), uuid4(), role=Role.ADMIN)
        with pytest.raises(ValueError, match="invalid idempotency key"):
            await idempotent_mutation(
                session=session,
                context=context,
                keys=(HmacKeyVersion("v1", b"k" * 32),),
                idempotency_key="bad\x00key",
                route="/v1/test",
                request_body={},
                response_type=ConnectionResponse,
                operation=operation,
                required_roles=(Role.ADMIN,),
            )
        with pytest.raises(RuntimeError, match="keyring is empty"):
            await idempotent_mutation(
                session=session,
                context=context,
                keys=(),
                idempotency_key="valid",
                route="/v1/test",
                request_body={},
                response_type=ConnectionResponse,
                operation=operation,
                required_roles=(Role.ADMIN,),
            )
        with pytest.raises(ValueError, match="request exceeds"):
            await idempotent_mutation(
                session=session,
                context=context,
                keys=(HmacKeyVersion("v1", b"k" * 32),),
                idempotency_key="valid",
                route="/v1/test",
                request_body={"value": "x" * 262_144},
                response_type=ConnectionResponse,
                operation=operation,
                required_roles=(Role.ADMIN,),
            )
        with pytest.raises(ValueError, match="cleanup batch size"):
            await purge_expired_api_idempotency(session, batch_size=0)

    asyncio.run(scenario())

"""Authenticated, workspace-scoped HTTP contracts for the alpha control plane."""

import asyncio
import base64
import binascii
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from modall.api.errors import InvalidRequest
from modall.api.idempotency import idempotent_mutation
from modall.audit.types import AuditAction, AuditOutcome, ResourceType
from modall.execution.service import ExecutionService
from modall.execution.types import HmacKeyVersion
from modall.identity.auth import Authenticator
from modall.identity.repository import AuthorizationDenied, AuthorizationService
from modall.identity.service import IdentityService
from modall.identity.types import Permission, Role, WorkspaceContext
from modall.persistence.database import transaction
from modall.persistence.models import (
    AuditEvent,
    Capability,
    CapabilityVersion,
    McpToolBinding,
    RegistryEntry,
    RegistryEntryVersion,
    Run,
    RunEvent,
    RunResult,
    ServerConnection,
    ServerConnectionVersion,
)
from modall.registry.discovery import RefreshJobService
from modall.registry.official import OfficialRegistryAdapter, OfficialRegistryService
from modall.registry.service import CapabilityService, ConnectionService

SessionFactory = async_sessionmaker[AsyncSession]
_DETAIL_VERSION_LIMIT = 100


class RegistrySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=256)


class RegistryImportRequest(BaseModel):
    cache_id: UUID
    provenance_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConnectionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    endpoint_url: str = Field(min_length=1, max_length=2048)
    secret_binding_id: UUID | None = None
    policy_version: str = Field(default="v1", min_length=1, max_length=64)


class ConnectionVersionCreateRequest(BaseModel):
    endpoint_url: str = Field(min_length=1, max_length=2048)
    secret_binding_id: UUID | None = None
    policy_version: str = Field(default="v1", min_length=1, max_length=64)


class CapabilityDecisionRequest(BaseModel):
    expected_version_id: UUID


class RunPreflightRequest(BaseModel):
    capability_version_id: UUID
    arguments: dict[str, object]


class RunCreateRequest(BaseModel):
    capability_version_id: UUID
    arguments: dict[str, object]
    confirmation_token: str = Field(min_length=1, max_length=8192)
    deadline: datetime | None = None


class PageInfo(BaseModel):
    next_cursor: str | None


class SessionResponse(BaseModel):
    workspace_id: UUID
    actor_user_id: UUID
    role: Role


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
    correlation_id: UUID


class RegistrySearchItemResponse(BaseModel):
    external_id: str
    source_version: str
    name: str
    description: str | None
    advertised_urls: list[str]
    provenance_digest: str


class RegistrySearchResponse(BaseModel):
    cache_id: UUID
    items: list[RegistrySearchItemResponse]
    fetched_at: datetime
    expires_at: datetime
    from_cache: bool


class RegistryEntryResponse(BaseModel):
    id: UUID
    source: str
    external_id: str | None
    current_version_id: UUID | None
    name: str | None
    description: str | None
    created_at: datetime


class RegistryEntryPage(BaseModel):
    items: list[RegistryEntryResponse]
    page: PageInfo


class ConnectionResponse(BaseModel):
    id: UUID
    name: str
    lifecycle: str
    pending_version_id: UUID | None
    verified_version_id: UUID | None
    control_epoch: int
    refresh_generation: int
    last_refresh_error_code: str | None
    last_refresh_at: datetime | None
    created_at: datetime


class ConnectionDetailResponse(ConnectionResponse):
    versions: list["ConnectionVersionResponse"]
    versions_truncated: bool


class ConnectionVersionResponse(BaseModel):
    id: UUID
    sequence: int
    endpoint_url: str
    secret_binding_id: UUID | None
    transport: str
    policy_version: str
    created_at: datetime


class ConnectionPage(BaseModel):
    items: list[ConnectionResponse]
    page: PageInfo


class RefreshResponse(BaseModel):
    job_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    generation: int
    status: str


class CapabilityResponse(BaseModel):
    id: UUID
    connection_id: UUID
    tool_identity: str
    pending_version_id: UUID | None
    enabled_version_id: UUID | None
    status: str
    status_epoch: int
    created_at: datetime


class CapabilityDetailResponse(CapabilityResponse):
    versions: list["CapabilityVersionResponse"]
    versions_truncated: bool


class CapabilityVersionResponse(BaseModel):
    id: UUID
    capability_id: UUID
    connection_version_id: UUID
    sequence: int
    display_name: str
    description: str | None
    input_schema: dict[str, object]
    output_schema: dict[str, object] | None
    metadata_digest: str
    schema_supported: bool
    created_at: datetime


class CapabilityPage(BaseModel):
    items: list[CapabilityResponse]
    page: PageInfo


class RunPreflightResponse(BaseModel):
    confirmation_token: str
    capability_version_id: UUID
    connection_version_id: UUID
    argument_digest: str
    expires_at: datetime


class RunResponse(BaseModel):
    id: UUID
    actor_user_id: UUID
    capability_id: UUID
    capability_version_id: UUID
    connection_id: UUID
    connection_version_id: UUID
    status: str
    arguments: dict[str, object] | None
    result: dict[str, object] | None
    safe_error_code: str | None
    arguments_expires_at: datetime
    result_expires_at: datetime | None
    cancellation_requested: bool
    deadline: datetime
    created_at: datetime
    updated_at: datetime
    terminal_at: datetime | None


class RunPage(BaseModel):
    items: list[RunResponse]
    page: PageInfo


class RunEventResponse(BaseModel):
    id: UUID
    sequence: int
    event_type: str
    status: str
    safe_error_code: str | None
    occurred_at: datetime


class RunEventPage(BaseModel):
    items: list[RunEventResponse]
    page: PageInfo


class AuditEventResponse(BaseModel):
    id: UUID
    actor_user_id: UUID
    action: str
    resource_type: str
    resource_id: UUID
    outcome: str
    correlation_id: UUID
    occurred_at: datetime


class AuditEventPage(BaseModel):
    items: list[AuditEventResponse]
    page: PageInfo


class _RequestState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    context: WorkspaceContext
    session: AsyncSession


def build_control_plane_router(
    *,
    session_factory: SessionFactory,
    authenticator: Authenticator,
    registry_adapter: OfficialRegistryAdapter,
    keyring_loader: Callable[[], tuple[Sequence[HmacKeyVersion], Sequence[HmacKeyVersion]]],
    environment: str,
) -> APIRouter:
    """Bind the HTTP surface to one process-scoped set of dependencies."""

    router = APIRouter(
        prefix="/v1",
        responses={
            status.HTTP_401_UNAUTHORIZED: {"model": ErrorResponse},
            status.HTTP_403_FORBIDDEN: {"model": ErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ErrorResponse},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse},
            status.HTTP_500_INTERNAL_SERVER_ERROR: {"model": ErrorResponse},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse},
        },
    )
    bearer = HTTPBearer(auto_error=False)

    async def session_dependency() -> AsyncIterator[AsyncSession]:
        async with transaction(session_factory) as session:
            yield session

    async def request_state(
        request: Request,
        session: Annotated[AsyncSession, Depends(session_dependency)],
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)] = None,
        workspace_header: Annotated[str | None, Header(alias="X-Workspace-ID")] = None,
    ) -> _RequestState:
        token = _bearer_token(request.headers.get("Authorization"), credentials)
        principal = await asyncio.to_thread(authenticator.authenticate, token)
        if workspace_header is None:
            raise AuthorizationDenied("workspace access denied")
        try:
            workspace_id = UUID(workspace_header)
        except ValueError:
            raise AuthorizationDenied("workspace access denied") from None
        user = await IdentityService(session).resolve_user(principal)
        context = await AuthorizationService(session).authorize(
            user_id=user.id,
            workspace_id=workspace_id,
            permission=Permission.VIEW_RESOURCES,
        )
        request.state.workspace_context = context
        session.info["correlation_id"] = request.state.correlation_id
        return _RequestState(context=context, session=session)

    State = Annotated[_RequestState, Depends(request_state)]
    Idempotency = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=256)]

    def execution_service(session: AsyncSession) -> ExecutionService:
        confirmation_keys, idempotency_keys = keyring_loader()
        return ExecutionService(
            session,
            confirmation_keys=confirmation_keys,
            idempotency_keys=idempotency_keys,
        )

    async def mutate[ResponseT: BaseModel](
        *,
        state: _RequestState,
        idempotency_key: str,
        route: str,
        request_body: object,
        response_type: type[ResponseT],
        operation: Callable[[], Awaitable[ResponseT]],
        required_roles: Sequence[Role],
        record_response: Callable[[ResponseT], dict[str, object]] | None = None,
        replay_response: Callable[[dict[str, object]], Awaitable[ResponseT]] | None = None,
    ) -> ResponseT:
        _, mutation_keys = keyring_loader()
        return await idempotent_mutation(
            session=state.session,
            context=state.context,
            keys=mutation_keys,
            idempotency_key=idempotency_key,
            route=route,
            request_body=request_body,
            response_type=response_type,
            operation=operation,
            required_roles=required_roles,
            record_response=record_response,
            replay_response=replay_response,
        )

    @router.get("/session", response_model=SessionResponse)
    async def get_session(state: State) -> SessionResponse:
        """Return the server-authoritative workspace membership for UI gating."""

        return SessionResponse(
            workspace_id=state.context.workspace_id,
            actor_user_id=state.context.actor_user_id,
            role=state.context.role,
        )

    @router.post("/registry/searches", response_model=RegistrySearchResponse)
    async def search_registry(body: RegistrySearchRequest, state: State) -> RegistrySearchResponse:
        found = await OfficialRegistryService(state.session, registry_adapter).search(
            context=state.context, query=body.query
        )
        return RegistrySearchResponse(
            cache_id=found.cache_id,
            items=[
                RegistrySearchItemResponse(
                    external_id=item.external_id,
                    source_version=item.source_version,
                    name=item.name,
                    description=item.description,
                    advertised_urls=list(item.advertised_urls),
                    provenance_digest=item.provenance_digest,
                )
                for item in found.items
            ],
            fetched_at=found.fetched_at,
            expires_at=found.expires_at,
            from_cache=found.from_cache,
        )

    @router.post(
        "/registry/imports",
        response_model=RegistryEntryResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_registry_entry(
        body: RegistryImportRequest, state: State, idempotency_key: Idempotency
    ) -> RegistryEntryResponse:
        async def operation() -> RegistryEntryResponse:
            version = await OfficialRegistryService(state.session, registry_adapter).import_cached(
                context=state.context,
                cache_id=body.cache_id,
                provenance_digest=body.provenance_digest,
                correlation_id=_correlation_id(state.session),
            )
            entry = await state.session.get(RegistryEntry, version.registry_entry_id)
            if entry is None:
                raise RuntimeError("imported Registry entry is unavailable")
            return _registry_entry(entry, version)

        return await mutate(
            state=state,
            idempotency_key=idempotency_key,
            route="/v1/registry/imports",
            request_body=body.model_dump(mode="json"),
            response_type=RegistryEntryResponse,
            operation=operation,
            required_roles=(Role.ADMIN, Role.OPERATOR),
        )

    @router.get("/registry/entries", response_model=RegistryEntryPage)
    async def list_registry_entries(
        state: State,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
    ) -> RegistryEntryPage:
        statement: Select[Any] = (
            select(RegistryEntry, RegistryEntryVersion)
            .outerjoin(
                RegistryEntryVersion,
                RegistryEntryVersion.id == RegistryEntry.current_version_id,
            )
            .where(RegistryEntry.workspace_id == state.context.workspace_id)
            .order_by(RegistryEntry.id.desc())
        )
        statement = _after_cursor(statement, RegistryEntry.id, cursor)
        rows = (await state.session.execute(statement.limit(limit + 1))).all()
        return RegistryEntryPage(
            items=[_registry_entry(entry, version) for entry, version in rows[:limit]],
            page=PageInfo(next_cursor=_next_cursor(rows, limit, lambda row: row[0].id)),
        )

    @router.post(
        "/server-connections",
        response_model=ConnectionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_connection(
        body: ConnectionCreateRequest, state: State, idempotency_key: Idempotency
    ) -> ConnectionResponse:
        async def operation() -> ConnectionResponse:
            try:
                connection = await ConnectionService(
                    state.session,
                    environment=environment,
                    allow_loopback_http=environment in {"local", "test"},
                ).create(
                    context=state.context,
                    name=body.name,
                    endpoint_url=body.endpoint_url,
                    secret_binding_id=body.secret_binding_id,
                    policy_version=body.policy_version,
                    correlation_id=_correlation_id(state.session),
                )
            except ValueError:
                raise InvalidRequest("invalid connection configuration") from None
            return _connection(connection)

        return await mutate(
            state=state,
            idempotency_key=idempotency_key,
            route="/v1/server-connections",
            request_body=body.model_dump(mode="json"),
            response_type=ConnectionResponse,
            operation=operation,
            required_roles=(Role.ADMIN,),
        )

    @router.get("/server-connections", response_model=ConnectionPage)
    async def list_connections(
        state: State,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
    ) -> ConnectionPage:
        statement: Select[Any] = (
            select(ServerConnection)
            .where(ServerConnection.workspace_id == state.context.workspace_id)
            .order_by(ServerConnection.id.desc())
        )
        statement = _after_cursor(statement, ServerConnection.id, cursor)
        rows = list((await state.session.scalars(statement.limit(limit + 1))).all())
        return ConnectionPage(
            items=[_connection(item) for item in rows[:limit]],
            page=PageInfo(next_cursor=_next_cursor(rows, limit, lambda row: row.id)),
        )

    @router.get("/server-connections/{connection_id}", response_model=ConnectionDetailResponse)
    async def get_connection(connection_id: UUID, state: State) -> ConnectionDetailResponse:
        connection = await _scoped_one(
            state.session, ServerConnection, connection_id, state.context.workspace_id
        )
        versions = list(
            (
                await state.session.scalars(
                    select(ServerConnectionVersion)
                    .where(ServerConnectionVersion.connection_id == connection.id)
                    .order_by(ServerConnectionVersion.sequence.desc())
                    .limit(_DETAIL_VERSION_LIMIT + 1)
                )
            ).all()
        )
        return ConnectionDetailResponse(
            **_connection(connection).model_dump(),
            versions=[_connection_version(item) for item in versions[:_DETAIL_VERSION_LIMIT]],
            versions_truncated=len(versions) > _DETAIL_VERSION_LIMIT,
        )

    @router.post(
        "/server-connections/{connection_id}/versions",
        response_model=ConnectionVersionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def append_connection_version(
        connection_id: UUID,
        body: ConnectionVersionCreateRequest,
        state: State,
        idempotency_key: Idempotency,
    ) -> ConnectionVersionResponse:
        async def operation() -> ConnectionVersionResponse:
            try:
                version = await ConnectionService(
                    state.session,
                    environment=environment,
                    allow_loopback_http=environment in {"local", "test"},
                ).append_version(
                    context=state.context,
                    connection_id=connection_id,
                    endpoint_url=body.endpoint_url,
                    secret_binding_id=body.secret_binding_id,
                    policy_version=body.policy_version,
                    correlation_id=_correlation_id(state.session),
                )
            except ValueError:
                raise InvalidRequest("invalid connection configuration") from None
            return _connection_version(version)

        return await mutate(
            state=state,
            idempotency_key=idempotency_key,
            route=f"/v1/server-connections/{connection_id}/versions",
            request_body=body.model_dump(mode="json"),
            response_type=ConnectionVersionResponse,
            operation=operation,
            required_roles=(Role.ADMIN,),
        )

    async def enqueue_refresh(connection_id: UUID, state: _RequestState) -> RefreshResponse:
        job = await RefreshJobService(state.session).enqueue(
            context=state.context, connection_id=connection_id
        )
        return RefreshResponse(
            job_id=job.id,
            connection_id=job.connection_id,
            connection_version_id=job.connection_version_id,
            generation=job.generation,
            status=job.status,
        )

    @router.post("/server-connections/{connection_id}/verify", response_model=RefreshResponse)
    async def verify_connection(
        connection_id: UUID, state: State, idempotency_key: Idempotency
    ) -> RefreshResponse:
        return await mutate(
            state=state,
            idempotency_key=idempotency_key,
            route=f"/v1/server-connections/{connection_id}/verify",
            request_body={},
            response_type=RefreshResponse,
            operation=lambda: enqueue_refresh(connection_id, state),
            required_roles=(Role.ADMIN, Role.OPERATOR),
        )

    @router.post("/server-connections/{connection_id}/refresh", response_model=RefreshResponse)
    async def refresh_connection(
        connection_id: UUID, state: State, idempotency_key: Idempotency
    ) -> RefreshResponse:
        return await mutate(
            state=state,
            idempotency_key=idempotency_key,
            route=f"/v1/server-connections/{connection_id}/refresh",
            request_body={},
            response_type=RefreshResponse,
            operation=lambda: enqueue_refresh(connection_id, state),
            required_roles=(Role.ADMIN, Role.OPERATOR),
        )

    @router.post("/server-connections/{connection_id}/disable", response_model=ConnectionResponse)
    async def disable_connection(
        connection_id: UUID, state: State, idempotency_key: Idempotency
    ) -> ConnectionResponse:
        async def operation() -> ConnectionResponse:
            result = await ConnectionService(state.session).disable(
                context=state.context,
                connection_id=connection_id,
                correlation_id=_correlation_id(state.session),
            )
            return _connection(result)

        return await mutate(
            state=state,
            idempotency_key=idempotency_key,
            route=f"/v1/server-connections/{connection_id}/disable",
            request_body={},
            response_type=ConnectionResponse,
            operation=operation,
            required_roles=(Role.ADMIN, Role.OPERATOR),
        )

    @router.post("/server-connections/{connection_id}/enable", response_model=RefreshResponse)
    async def enable_connection(
        connection_id: UUID, state: State, idempotency_key: Idempotency
    ) -> RefreshResponse:
        async def operation() -> RefreshResponse:
            await ConnectionService(state.session).enable(
                context=state.context,
                connection_id=connection_id,
                correlation_id=_correlation_id(state.session),
            )
            return await enqueue_refresh(connection_id, state)

        return await mutate(
            state=state,
            idempotency_key=idempotency_key,
            route=f"/v1/server-connections/{connection_id}/enable",
            request_body={},
            response_type=RefreshResponse,
            operation=operation,
            required_roles=(Role.ADMIN,),
        )

    @router.get("/capabilities", response_model=CapabilityPage)
    async def list_capabilities(
        state: State,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
        connection_id: UUID | None = None,
        capability_status: str | None = Query(default=None, alias="status"),
    ) -> CapabilityPage:
        statement: Select[Any] = select(Capability).where(
            Capability.workspace_id == state.context.workspace_id
        )
        if connection_id is not None:
            statement = statement.where(Capability.connection_id == connection_id)
        if capability_status is not None:
            statement = statement.where(Capability.status == capability_status)
        statement = statement.order_by(Capability.id.desc())
        statement = _after_cursor(statement, Capability.id, cursor)
        rows = list((await state.session.scalars(statement.limit(limit + 1))).all())
        return CapabilityPage(
            items=[_capability(item) for item in rows[:limit]],
            page=PageInfo(next_cursor=_next_cursor(rows, limit, lambda row: row.id)),
        )

    @router.get("/capabilities/{capability_id}", response_model=CapabilityDetailResponse)
    async def get_capability(capability_id: UUID, state: State) -> CapabilityDetailResponse:
        capability = await _scoped_one(
            state.session, Capability, capability_id, state.context.workspace_id
        )
        versions = list(
            (
                await state.session.execute(
                    select(CapabilityVersion, McpToolBinding.connection_version_id)
                    .join(
                        McpToolBinding,
                        McpToolBinding.capability_version_id == CapabilityVersion.id,
                    )
                    .where(CapabilityVersion.capability_id == capability.id)
                    .order_by(CapabilityVersion.sequence.desc())
                    .limit(_DETAIL_VERSION_LIMIT + 1)
                )
            ).all()
        )
        return CapabilityDetailResponse(
            **_capability(capability).model_dump(),
            versions=[
                _capability_version(item, connection_version_id)
                for item, connection_version_id in versions[:_DETAIL_VERSION_LIMIT]
            ],
            versions_truncated=len(versions) > _DETAIL_VERSION_LIMIT,
        )

    @router.get(
        "/capability-versions/{capability_version_id}", response_model=CapabilityVersionResponse
    )
    async def get_capability_version(
        capability_version_id: UUID, state: State
    ) -> CapabilityVersionResponse:
        version = await _scoped_one(
            state.session, CapabilityVersion, capability_version_id, state.context.workspace_id
        )
        connection_version_id = await state.session.scalar(
            select(McpToolBinding.connection_version_id).where(
                McpToolBinding.capability_version_id == version.id,
                McpToolBinding.workspace_id == state.context.workspace_id,
            )
        )
        if connection_version_id is None:
            raise RuntimeError("capability version is missing its immutable connection binding")
        return _capability_version(version, connection_version_id)

    @router.post(
        "/capability-versions/{capability_version_id}/enable",
        response_model=CapabilityResponse,
    )
    async def enable_capability(
        capability_version_id: UUID, state: State, idempotency_key: Idempotency
    ) -> CapabilityResponse:
        async def operation() -> CapabilityResponse:
            version = await _scoped_one(
                state.session,
                CapabilityVersion,
                capability_version_id,
                state.context.workspace_id,
            )
            capability = await CapabilityService(state.session).enable(
                context=state.context,
                capability_id=version.capability_id,
                expected_version_id=version.id,
                correlation_id=_correlation_id(state.session),
            )
            return _capability(capability)

        return await mutate(
            state=state,
            idempotency_key=idempotency_key,
            route=f"/v1/capability-versions/{capability_version_id}/enable",
            request_body={},
            response_type=CapabilityResponse,
            operation=operation,
            required_roles=(Role.ADMIN, Role.OPERATOR),
        )

    @router.post(
        "/capability-versions/{capability_version_id}/disable",
        response_model=CapabilityResponse,
    )
    async def disable_capability(
        capability_version_id: UUID, state: State, idempotency_key: Idempotency
    ) -> CapabilityResponse:
        async def operation() -> CapabilityResponse:
            version = await _scoped_one(
                state.session,
                CapabilityVersion,
                capability_version_id,
                state.context.workspace_id,
            )
            capability = await CapabilityService(state.session).disable(
                context=state.context,
                capability_id=version.capability_id,
                expected_version_id=version.id,
                correlation_id=_correlation_id(state.session),
            )
            return _capability(capability)

        return await mutate(
            state=state,
            idempotency_key=idempotency_key,
            route=f"/v1/capability-versions/{capability_version_id}/disable",
            request_body={},
            response_type=CapabilityResponse,
            operation=operation,
            required_roles=(Role.ADMIN, Role.OPERATOR),
        )

    @router.post("/run-preflights", response_model=RunPreflightResponse)
    async def preflight_run(body: RunPreflightRequest, state: State) -> RunPreflightResponse:
        result = await execution_service(state.session).preflight(
            context=state.context,
            capability_version_id=body.capability_version_id,
            arguments=body.arguments,
        )
        return RunPreflightResponse(
            confirmation_token=result.confirmation_token,
            capability_version_id=result.capability_version_id,
            connection_version_id=result.connection_version_id,
            argument_digest=result.argument_digest,
            expires_at=result.expires_at,
        )

    @router.post("/runs", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
    async def create_run(
        body: RunCreateRequest, state: State, idempotency_key: Idempotency
    ) -> RunResponse:
        run = await execution_service(state.session).create_run(
            context=state.context,
            capability_version_id=body.capability_version_id,
            arguments=body.arguments,
            confirmation_token=body.confirmation_token,
            idempotency_key=idempotency_key,
            deadline=body.deadline,
            correlation_id=_correlation_id(state.session),
        )
        return await _run_response(state.session, run)

    @router.get("/runs", response_model=RunPage)
    async def list_runs(
        state: State,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
        run_status: str | None = Query(default=None, alias="status"),
    ) -> RunPage:
        statement: Select[Any] = select(Run).where(Run.workspace_id == state.context.workspace_id)
        if run_status is not None:
            statement = statement.where(Run.status == run_status)
        statement = statement.order_by(Run.created_at.desc(), Run.id.desc())
        if cursor is not None:
            cursor_time, cursor_id = _decode_audit_cursor(cursor)
            statement = statement.where(
                or_(
                    Run.created_at < cursor_time,
                    and_(Run.created_at == cursor_time, Run.id < cursor_id),
                )
            )
        rows = list((await state.session.scalars(statement.limit(limit + 1))).all())
        page_runs = rows[:limit]
        results: dict[UUID, RunResult] = {}
        if page_runs:
            results = {
                result.run_id: result
                for result in (
                    await state.session.scalars(
                        select(RunResult).where(
                            RunResult.workspace_id == state.context.workspace_id,
                            RunResult.run_id.in_([run.id for run in page_runs]),
                            RunResult.expires_at > datetime.now(UTC),
                        )
                    )
                ).all()
            }
        return RunPage(
            items=[_run_response_value(run, results.get(run.id)) for run in page_runs],
            page=PageInfo(
                next_cursor=(
                    _encode_audit_cursor(page_runs[-1].created_at, page_runs[-1].id)
                    if len(rows) > limit
                    else None
                )
            ),
        )

    @router.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: UUID, state: State) -> RunResponse:
        run = await _scoped_one(state.session, Run, run_id, state.context.workspace_id)
        return await _run_response(state.session, run)

    @router.get("/runs/{run_id}/events", response_model=RunEventPage)
    async def list_run_events(
        run_id: UUID,
        state: State,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
    ) -> RunEventPage:
        await _scoped_one(state.session, Run, run_id, state.context.workspace_id)
        statement: Select[Any] = (
            select(RunEvent)
            .where(
                RunEvent.workspace_id == state.context.workspace_id,
                RunEvent.run_id == run_id,
            )
            .order_by(RunEvent.sequence.asc())
        )
        if cursor is not None:
            statement = statement.where(RunEvent.sequence > _decode_sequence_cursor(cursor))
        rows = list((await state.session.scalars(statement.limit(limit + 1))).all())
        return RunEventPage(
            items=[
                RunEventResponse(
                    id=item.id,
                    sequence=item.sequence,
                    event_type=item.event_type,
                    status=item.status,
                    safe_error_code=item.safe_error_code,
                    occurred_at=item.occurred_at,
                )
                for item in rows[:limit]
            ],
            page=PageInfo(
                next_cursor=(
                    _encode_sequence_cursor(rows[limit - 1].sequence) if len(rows) > limit else None
                )
            ),
        )

    @router.post("/runs/{run_id}/cancel", response_model=RunResponse)
    async def cancel_run(run_id: UUID, state: State, idempotency_key: Idempotency) -> RunResponse:
        async def operation() -> RunResponse:
            run = await execution_service(state.session).cancel_run(
                context=state.context,
                run_id=run_id,
                correlation_id=_correlation_id(state.session),
            )
            return await _run_response(state.session, run)

        async def replay(saved: dict[str, object]) -> RunResponse:
            saved_id = saved.get("id")
            if not isinstance(saved_id, str):
                raise RuntimeError("invalid idempotency record")
            run = await _scoped_one(state.session, Run, UUID(saved_id), state.context.workspace_id)
            return await _run_response(state.session, run)

        return await mutate(
            state=state,
            idempotency_key=idempotency_key,
            route=f"/v1/runs/{run_id}/cancel",
            request_body={"run_id": str(run_id)},
            response_type=RunResponse,
            operation=operation,
            required_roles=(Role.ADMIN, Role.OPERATOR),
            record_response=lambda response: {"id": str(response.id)},
            replay_response=replay,
        )

    @router.get("/audit-events", response_model=AuditEventPage)
    async def list_audit_events(
        state: State,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: str | None = None,
        resource_type: ResourceType | None = None,
        resource_id: UUID | None = None,
        actor_id: UUID | None = None,
        action: AuditAction | None = None,
        outcome: AuditOutcome | None = None,
        occurred_after: datetime | None = None,
        occurred_before: datetime | None = None,
    ) -> AuditEventPage:
        if state.context.role not in {Role.ADMIN, Role.OPERATOR}:
            raise AuthorizationDenied("workspace access denied")
        statement: Select[Any] = select(AuditEvent).where(
            AuditEvent.workspace_id == state.context.workspace_id
        )
        filters = (
            (AuditEvent.resource_type, resource_type.value if resource_type else None),
            (AuditEvent.resource_id, resource_id),
            (AuditEvent.actor_user_id, actor_id),
            (AuditEvent.action, action.value if action else None),
            (AuditEvent.outcome, outcome.value if outcome else None),
        )
        for column, value in filters:
            if value is not None:
                statement = statement.where(column == value)
        if occurred_after is not None:
            occurred_after = _require_aware_datetime(occurred_after)
            statement = statement.where(AuditEvent.occurred_at >= occurred_after)
        if occurred_before is not None:
            occurred_before = _require_aware_datetime(occurred_before)
            statement = statement.where(AuditEvent.occurred_at < occurred_before)
        if (
            occurred_after is not None
            and occurred_before is not None
            and occurred_after >= occurred_before
        ):
            raise InvalidRequest("invalid audit time range")
        statement = statement.order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
        if cursor is not None:
            cursor_time, cursor_id = _decode_audit_cursor(cursor)
            statement = statement.where(
                or_(
                    AuditEvent.occurred_at < cursor_time,
                    and_(
                        AuditEvent.occurred_at == cursor_time,
                        AuditEvent.id < cursor_id,
                    ),
                )
            )
        rows = list((await state.session.scalars(statement.limit(limit + 1))).all())
        return AuditEventPage(
            items=[
                AuditEventResponse(
                    id=item.id,
                    actor_user_id=item.actor_user_id,
                    action=item.action,
                    resource_type=item.resource_type,
                    resource_id=item.resource_id,
                    outcome=item.outcome,
                    correlation_id=item.correlation_id,
                    occurred_at=item.occurred_at,
                )
                for item in rows[:limit]
            ],
            page=PageInfo(
                next_cursor=(
                    _encode_audit_cursor(
                        rows[limit - 1].occurred_at,
                        rows[limit - 1].id,
                    )
                    if len(rows) > limit
                    else None
                )
            ),
        )

    return router


def _bearer_token(
    authorization: str | None, credentials: HTTPAuthorizationCredentials | None
) -> str | None:
    if authorization is None:
        return None
    if credentials is None or credentials.scheme.lower() != "bearer":
        return ""
    return credentials.credentials


def _correlation_id(session: AsyncSession) -> UUID:
    value = session.info.get("correlation_id")
    return value if isinstance(value, UUID) else uuid4()


def _encode_cursor(value: UUID) -> str:
    return base64.urlsafe_b64encode(value.bytes).rstrip(b"=").decode("ascii")


def _decode_cursor(value: str) -> UUID:
    if len(value) != 22:
        raise InvalidRequest("invalid cursor")
    try:
        return UUID(bytes=base64.urlsafe_b64decode(value + "=="))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise InvalidRequest("invalid cursor") from exc


def _encode_sequence_cursor(value: int) -> str:
    return base64.urlsafe_b64encode(str(value).encode()).rstrip(b"=").decode("ascii")


def _decode_sequence_cursor(value: str) -> int:
    if not 1 <= len(value) <= 16:
        raise InvalidRequest("invalid cursor")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
        sequence = int(decoded)
    except (binascii.Error, ValueError, UnicodeError) as exc:
        raise InvalidRequest("invalid cursor") from exc
    if sequence < 1:
        raise InvalidRequest("invalid cursor")
    return sequence


def _after_cursor(
    statement: Select[Any],
    column: InstrumentedAttribute[UUID],
    cursor: str | None,
) -> Select[Any]:
    if cursor is None:
        return statement
    return statement.where(column < _decode_cursor(cursor))


def _encode_audit_cursor(occurred_at: datetime, identifier: UUID) -> str:
    normalized = (
        occurred_at.replace(tzinfo=UTC)
        if occurred_at.tzinfo is None
        else occurred_at.astimezone(UTC)
    )
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = normalized - epoch
    microseconds = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    payload = microseconds.to_bytes(8, "big", signed=True) + identifier.bytes
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_audit_cursor(value: str) -> tuple[datetime, UUID]:
    if len(value) != 32:
        raise InvalidRequest("invalid cursor")
    try:
        payload = base64.b64decode(value, altchars=b"-_", validate=True)
        if len(payload) != 24:
            raise ValueError("invalid payload")
        microseconds = int.from_bytes(payload[:8], "big", signed=True)
        occurred_at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=microseconds)
        return occurred_at, UUID(bytes=payload[8:])
    except (binascii.Error, OverflowError, ValueError) as exc:
        raise InvalidRequest("invalid cursor") from exc


def _next_cursor(rows: Sequence[Any], limit: int, identifier: Callable[[Any], UUID]) -> str | None:
    return _encode_cursor(identifier(rows[limit - 1])) if len(rows) > limit else None


async def _scoped_one(
    session: AsyncSession, model: type[object], identifier: UUID, workspace_id: UUID
) -> Any:
    mapped = cast(Any, model)
    row = await session.scalar(
        select(mapped).where(mapped.id == identifier, mapped.workspace_id == workspace_id)
    )
    if row is None:
        raise AuthorizationDenied("workspace access denied")
    return row


def _registry_entry(
    entry: RegistryEntry, version: RegistryEntryVersion | None
) -> RegistryEntryResponse:
    return RegistryEntryResponse(
        id=entry.id,
        source=entry.source,
        external_id=entry.external_id,
        current_version_id=entry.current_version_id,
        name=version.name if version else None,
        description=version.description if version else None,
        created_at=entry.created_at,
    )


def _connection(item: ServerConnection) -> ConnectionResponse:
    return ConnectionResponse(
        id=item.id,
        name=item.name,
        lifecycle=item.lifecycle,
        pending_version_id=item.pending_version_id,
        verified_version_id=item.verified_version_id,
        control_epoch=item.control_epoch,
        refresh_generation=item.refresh_generation,
        last_refresh_error_code=item.last_refresh_error_code,
        last_refresh_at=item.last_refresh_at,
        created_at=item.created_at,
    )


def _connection_version(item: ServerConnectionVersion) -> ConnectionVersionResponse:
    return ConnectionVersionResponse(
        id=item.id,
        sequence=item.sequence,
        endpoint_url=item.endpoint_url,
        secret_binding_id=item.secret_binding_id,
        transport=item.transport,
        policy_version=item.policy_version,
        created_at=item.created_at,
    )


def _capability(item: Capability) -> CapabilityResponse:
    return CapabilityResponse(
        id=item.id,
        connection_id=item.connection_id,
        tool_identity=item.tool_identity,
        pending_version_id=item.pending_version_id,
        enabled_version_id=item.enabled_version_id,
        status=item.status,
        status_epoch=item.status_epoch,
        created_at=item.created_at,
    )


def _capability_version(
    item: CapabilityVersion, connection_version_id: UUID
) -> CapabilityVersionResponse:
    return CapabilityVersionResponse(
        id=item.id,
        capability_id=item.capability_id,
        connection_version_id=connection_version_id,
        sequence=item.sequence,
        display_name=item.display_name,
        description=item.description,
        input_schema=item.input_schema,
        output_schema=item.output_schema,
        metadata_digest=item.metadata_digest,
        schema_supported=item.schema_supported,
        created_at=item.created_at,
    )


async def _run_response(session: AsyncSession, run: Run) -> RunResponse:
    now = datetime.now(UTC)
    result = await session.scalar(
        select(RunResult).where(
            RunResult.run_id == run.id,
            RunResult.workspace_id == run.workspace_id,
            RunResult.expires_at > now,
        )
    )
    return _run_response_value(run, result, now=now)


def _run_response_value(
    run: Run, result: RunResult | None, *, now: datetime | None = None
) -> RunResponse:
    current = now or datetime.now(UTC)
    arguments = run.arguments if _utc(run.arguments_expires_at) > current else None
    return RunResponse(
        id=run.id,
        actor_user_id=run.actor_user_id,
        capability_id=run.capability_id,
        capability_version_id=run.capability_version_id,
        connection_id=run.connection_id,
        connection_version_id=run.connection_version_id,
        status=run.status,
        arguments=arguments,
        result=result.payload if result else None,
        safe_error_code=run.safe_error_code,
        arguments_expires_at=run.arguments_expires_at,
        result_expires_at=result.expires_at if result else None,
        cancellation_requested=run.cancellation_requested,
        deadline=run.deadline,
        created_at=run.created_at,
        updated_at=run.updated_at,
        terminal_at=run.terminal_at,
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _require_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidRequest("audit timestamps require an offset")
    return value.astimezone(UTC)

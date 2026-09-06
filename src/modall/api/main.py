"""FastAPI application entry point."""

import logging
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import uvicorn
from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modall.api.contracts import build_control_plane_router
from modall.api.errors import InvalidRequest
from modall.api.idempotency import ApiIdempotencyConflict, ApiIdempotencyHistoryIncomplete
from modall.config import Settings, get_settings
from modall.execution.runtime import build_execution_keyrings
from modall.execution.types import ExecutionError, ExecutionFailureCode, HmacKeyVersion
from modall.identity.auth import AuthenticationError, build_authenticator
from modall.identity.repository import AuthorizationDenied
from modall.persistence.database import (
    DatabaseProbe,
    async_database_url,
    create_engine,
    create_session_factory,
)
from modall.registry.official import (
    OfficialRegistryAdapter,
    OfficialRegistryError,
    OfficialRegistryFailureCode,
)
from modall.registry.service import InvalidCapabilityTransition, InvalidConnectionTransition


class HealthResponse(BaseModel):
    """Stable, payload-free health contract."""

    status: str
    service: str


ReadinessProbe = Callable[[], Awaitable[bool]]


def create_app(
    settings: Settings | None = None,
    *,
    readiness_probe: ReadinessProbe | None = None,
    engine: AsyncEngine | None = None,
    registry_adapter: OfficialRegistryAdapter | None = None,
) -> FastAPI:
    """Build an application instance without process-global test mutation."""

    resolved_settings = settings or get_settings()
    database_probe: DatabaseProbe | None = None
    owns_engine = engine is None
    engine = engine or create_engine(async_database_url(str(resolved_settings.database_url)))
    keyrings: tuple[tuple[HmacKeyVersion, ...], tuple[HmacKeyVersion, ...]] | None = None

    def keyring_loader() -> tuple[tuple[HmacKeyVersion, ...], tuple[HmacKeyVersion, ...]]:
        nonlocal keyrings
        if keyrings is None:
            keyrings = build_execution_keyrings(resolved_settings)
        return keyrings

    if readiness_probe is None:
        database_probe = DatabaseProbe(engine)
        readiness_probe = database_probe.ready

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        keyring_loader()
        yield
        if owns_engine:
            await engine.dispose()

    app = FastAPI(
        title="Modall API",
        version="0.1.0",
        docs_url="/docs" if resolved_settings.environment != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )

    if resolved_settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(resolved_settings.cors_allowed_origins),
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "X-Correlation-ID",
                "X-Workspace-ID",
            ],
            expose_headers=["X-Correlation-ID"],
        )

    @app.middleware("http")
    async def response_policy(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        supplied = request.headers.get("X-Correlation-ID")
        try:
            correlation_id = UUID(supplied) if supplied is not None else uuid4()
        except ValueError:
            correlation_id = uuid4()
        request.state.correlation_id = correlation_id
        try:
            response = await call_next(request)
        except Exception as exc:
            safe_stack = " <- ".join(
                f"{frame.filename}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_tb(exc.__traceback__)
            )
            logging.getLogger("modall.api").warning(
                "unhandled_request_failure correlation_id=%s exception_type=%s stack=%s",
                correlation_id,
                type(exc).__name__,
                safe_stack,
            )
            response = error_response(
                "internal_error", "The request could not be completed.", 500, request
            )
            origin = request.headers.get("Origin")
            if origin in resolved_settings.cors_allowed_origins:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Expose-Headers"] = "X-Correlation-ID"
                response.headers.add_vary_header("Origin")
        response.headers["X-Correlation-ID"] = str(correlation_id)
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def error_response(code: str, message: str, http_status: int, request: Request) -> JSONResponse:
        return JSONResponse(
            status_code=http_status,
            content={
                "error": {"code": code, "message": message},
                "correlation_id": str(request.state.correlation_id),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.exception_handler(AuthenticationError)
    async def authentication_error(request: Request, _: AuthenticationError) -> JSONResponse:
        return error_response("authentication_required", "Authentication failed.", 401, request)

    @app.exception_handler(AuthorizationDenied)
    async def authorization_error(request: Request, _: AuthorizationDenied) -> JSONResponse:
        return error_response("access_denied", "Workspace access denied.", 403, request)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, _: RequestValidationError) -> JSONResponse:
        return error_response("invalid_request", "Request validation failed.", 422, request)

    @app.exception_handler(InvalidRequest)
    async def invalid_request_error(request: Request, _: InvalidRequest) -> JSONResponse:
        return error_response("invalid_request", "Request validation failed.", 422, request)

    @app.exception_handler(OfficialRegistryError)
    async def registry_error(request: Request, exc: OfficialRegistryError) -> JSONResponse:
        if exc.code in {
            OfficialRegistryFailureCode.TIMEOUT,
            OfficialRegistryFailureCode.UPSTREAM_UNAVAILABLE,
        }:
            http_status = 503
        elif exc.code is OfficialRegistryFailureCode.PERSISTENCE_FAILURE:
            http_status = 500
        else:
            http_status = 422
        return error_response(exc.code.value, "Registry operation failed.", http_status, request)

    @app.exception_handler(ExecutionError)
    async def execution_error(request: Request, exc: ExecutionError) -> JSONResponse:
        if exc.code in {
            ExecutionFailureCode.IDEMPOTENCY_KEY_HISTORY_INCOMPLETE,
            ExecutionFailureCode.CONFIRMATION_KEY_HISTORY_INCOMPLETE,
        }:
            http_status = 503
        elif exc.code is ExecutionFailureCode.PERSISTENCE_FAILURE:
            http_status = 500
        else:
            http_status = 409
        return error_response(exc.code.value, "Run operation failed.", http_status, request)

    @app.exception_handler(InvalidConnectionTransition)
    @app.exception_handler(InvalidCapabilityTransition)
    async def transition_error(request: Request, _: Exception) -> JSONResponse:
        return error_response("state_conflict", "Resource state changed.", 409, request)

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, _: IntegrityError) -> JSONResponse:
        return error_response("state_conflict", "Resource state changed.", 409, request)

    @app.exception_handler(ApiIdempotencyConflict)
    async def idempotency_error(request: Request, _: ApiIdempotencyConflict) -> JSONResponse:
        return error_response(
            "idempotency_conflict", "Idempotency key reused for another request.", 409, request
        )

    @app.exception_handler(ApiIdempotencyHistoryIncomplete)
    async def idempotency_history_error(
        request: Request, _: ApiIdempotencyHistoryIncomplete
    ) -> JSONResponse:
        return error_response(
            "idempotency_key_history_incomplete",
            "Mutation replay protection is unavailable.",
            503,
            request,
        )

    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    app.include_router(
        build_control_plane_router(
            session_factory=session_factory,
            authenticator=build_authenticator(resolved_settings),
            registry_adapter=registry_adapter or OfficialRegistryAdapter(),
            keyring_loader=keyring_loader,
            environment=resolved_settings.environment,
        )
    )

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    async def live() -> HealthResponse:
        return HealthResponse(status="ok", service="api")

    @app.get("/health/ready", response_model=HealthResponse, tags=["health"])
    async def ready(response: Response) -> HealthResponse:
        if not await readiness_probe():
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return HealthResponse(status="unavailable", service="api")
        return HealthResponse(status="ready", service="api")

    return app


app = create_app()


def run() -> None:
    """Run the local API server."""

    settings = get_settings()
    uvicorn.run(
        "modall.api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()

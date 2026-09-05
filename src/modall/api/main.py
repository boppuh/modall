"""FastAPI application entry point."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Response, status
from pydantic import BaseModel

from modall.config import Settings, get_settings
from modall.persistence.database import DatabaseProbe, async_database_url, create_engine


class HealthResponse(BaseModel):
    """Stable, payload-free health contract."""

    status: str
    service: str


ReadinessProbe = Callable[[], Awaitable[bool]]


def create_app(
    settings: Settings | None = None,
    *,
    readiness_probe: ReadinessProbe | None = None,
) -> FastAPI:
    """Build an application instance without process-global test mutation."""

    resolved_settings = settings or get_settings()
    database_probe: DatabaseProbe | None = None
    if readiness_probe is None:
        database_probe = DatabaseProbe(
            create_engine(async_database_url(str(resolved_settings.database_url)))
        )
        readiness_probe = database_probe.ready

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if database_probe is not None:
            await database_probe.close()

    app = FastAPI(
        title="Modall API",
        version="0.1.0",
        docs_url="/docs" if resolved_settings.environment != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
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

"""FastAPI application entry point."""

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from modall.config import Settings, get_settings


class HealthResponse(BaseModel):
    """Stable, payload-free health contract."""

    status: str
    service: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build an application instance without process-global test mutation."""

    resolved_settings = settings or get_settings()
    app = FastAPI(
        title="Modall API",
        version="0.1.0",
        docs_url="/docs" if resolved_settings.environment != "production" else None,
        redoc_url=None,
    )

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    async def live() -> HealthResponse:
        return HealthResponse(status="ok", service="api")

    @app.get("/health/ready", response_model=HealthResponse, tags=["health"])
    async def ready() -> HealthResponse:
        # Database readiness is added with persistence in planned PR03.
        return HealthResponse(status="ready", service="api")

    return app


app = create_app()


def run() -> None:
    """Run the local API server."""

    uvicorn.run("modall.api.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()

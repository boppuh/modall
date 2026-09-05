import asyncio

import httpx
import pytest
from fastapi import FastAPI

from modall.api import main
from modall.api.main import create_app
from modall.config import Settings


async def get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


def test_liveness_contract() -> None:
    response = asyncio.run(get(create_app(Settings(environment="test")), "/health/live"))

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "api"}


def test_readiness_contract() -> None:
    response = asyncio.run(get(create_app(Settings(environment="test")), "/health/ready"))

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "service": "api"}


def test_production_disables_interactive_docs() -> None:
    response = asyncio.run(get(create_app(Settings(environment="production")), "/docs"))

    assert response.status_code == 404


def test_run_starts_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, int, bool, str]] = []

    def fake_run(app: str, *, host: str, port: int, reload: bool, log_level: str) -> None:
        calls.append((app, host, port, reload, log_level))

    monkeypatch.setattr(main, "get_settings", lambda: Settings(log_level="DEBUG"))
    monkeypatch.setattr("modall.api.main.uvicorn.run", fake_run)

    main.run()

    assert calls == [("modall.api.main:app", "0.0.0.0", 8000, False, "debug")]

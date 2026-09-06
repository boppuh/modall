"""Export the deterministic local OpenAPI document for client generation."""

import json
from pathlib import Path

from modall.api.main import create_app
from modall.config import Settings


async def _ready() -> bool:
    return True


def main() -> None:
    target = Path("apps/web/src/api/openapi.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    schema = create_app(Settings(environment="test"), readiness_probe=_ready).openapi()
    target.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

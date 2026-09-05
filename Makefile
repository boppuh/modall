.DEFAULT_GOAL := help

.PHONY: bootstrap check compose-down compose-up format help migrate python-check test web-check

help:
	@awk 'BEGIN {FS = ":.*## "; printf "Usage: make <target>\n\n"} /^[a-zA-Z_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

bootstrap: ## Install locked Python and web dependencies
	uv sync --frozen
	npm ci

format: ## Format Python sources
	uv run ruff format .
	uv run ruff check --fix .

migrate: ## Apply database migrations
	uv run alembic upgrade head

python-check: ## Run Python format, lint, types, and tests
	uv run ruff format --check .
	uv run ruff check .
	uv run mypy
	uv run pytest

web-check: ## Run web lint, types, tests, and production build
	npm run web:lint
	npm run web:typecheck
	npm run web:test
	npm run web:build

test: ## Run Python and web tests
	uv run pytest
	npm run web:test

check: python-check web-check ## Run every local quality gate
	docker compose config --quiet

compose-up: ## Build and start the local stack
	docker compose up --build --wait

compose-down: ## Stop the local stack
	docker compose down

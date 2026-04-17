.PHONY: install dev db-init db-upgrade test lint

install:
	uv sync --extra dev

db-init:
	bash scripts/init_triage_home.sh
	uv run alembic upgrade head

db-upgrade:
	uv run alembic upgrade head

dev:
	bash scripts/dev.sh

test:
	uv run pytest

lint:
	uv run ruff check . && uv run mypy src tests

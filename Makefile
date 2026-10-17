.PHONY: up down db db-fresh sim demo test test-fast lint fmt types check

up:
	docker compose up -d --wait db

down:
	docker compose down

db: up
	uv run python -m recon_storage.bootstrap --env dev
	uv run python -m provider_sim.db
	uv run python -m recon_verifier.db

db-fresh: up
	uv run python -m recon_storage.bootstrap --env dev --fresh
	uv run python -m provider_sim.db
	uv run python -m recon_verifier.db

sim:
	uv run uvicorn --factory provider_sim.app:create_app --port 8100

# spec §25: kill the worker after the provider commits, recover to one refund (no model calls)
demo: db
	uv run recon demo dup-charge --fault FP4

test: up
	uv run pytest

test-fast:
	uv run pytest tests/domain tests/test_no_floats.py tests/test_boundaries.py

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

types:
	uv run pyright

check: lint types test

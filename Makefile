.PHONY: up down db db-fresh sim demo dev web-install web-check e2e faults test test-fast lint fmt types check

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

# local stack for the ui: sim, mcp-read, mcp-exec, worker (restarts when killed), api, web
dev: db
	uv run recon dev

web-install:
	pnpm --dir apps/web install

web-check:
	pnpm --dir apps/web typecheck
	pnpm --dir apps/web build

# playwright against the real local stack (starts `recon dev` unless it's already up)
e2e:
	pnpm --dir apps/web e2e

# spec §25: kill the worker after the provider commits, recover to one refund (no model calls)
demo: db
	uv run recon demo dup-charge --fault FP4

test: up
	uv run pytest

# e3: all 10 fault families x 10 instances + no-fault controls (ci runs one instance each)
faults: up
	RECON_FAULT_SUITE=full uv run pytest tests/faults/test_fault_suite.py -q

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

.PHONY: install test test-mongo lint fmt ping db-init check scan rank backlog

install:
	uv sync

test:
	uv run pytest

# Also run storage contract tests against MONGODB_URI from .env (Atlas)
test-mongo:
	JOBBOT_TEST_MONGO=1 uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

check: lint test

ping:
	uv run jobbot ping

db-init:
	uv run jobbot db-init

scan:
	uv run jobbot scan

rank:
	uv run jobbot rank --top 20

backlog:
	uv run jobbot backlog

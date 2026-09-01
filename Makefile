.PHONY: install format lint typecheck test check run worker spec demo

install:
	uv sync --all-extras --dev

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

typecheck:
	uv run mypy src

test:
	uv run pytest

check: lint typecheck test

run:
	uv run harnessix serve

worker:
	uv run harnessix worker

spec:
	uv run python scripts/generate_specs.py

demo:
	uv run python examples/mvp.py

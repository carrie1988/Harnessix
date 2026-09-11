.PHONY: install format lint readability typecheck test check run worker spec demo

install:
	uv sync --all-extras --dev

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

readability:
	uv run python scripts/readability_report.py --check --check-final-report --quiet

typecheck:
	uv run mypy src

test:
	uv run pytest

check: lint readability typecheck test

run:
	uv run harnessix serve

worker:
	uv run harnessix worker

spec:
	uv run python scripts/generate_specs.py

demo:
	uv run python examples/mvp.py

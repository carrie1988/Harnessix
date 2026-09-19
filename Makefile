.PHONY: install format lint readability documentation contracts typecheck test check run spec

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

documentation:
	uv run python scripts/documentation_check.py

contracts:
	uv run python scripts/generate_specs.py --check

typecheck:
	uv run mypy src

test:
	uv run pytest

check: lint readability documentation contracts typecheck test

run:
	uv run harnessix code

spec:
	uv run python scripts/generate_specs.py

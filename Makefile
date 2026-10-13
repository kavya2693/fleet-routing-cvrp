.PHONY: data data-small run run-small check test lint

data:
	uv run python scripts/generate_data.py --profile full

data-small:
	uv run python scripts/generate_data.py --profile small

# Full size: 300 trucks, four depots, ~2,000 orders a weekday. 20-30 minutes on a laptop.
run:
	uv run fleet-routing --profile full --refresh 2>&1 | tee outputs/full/run_log.txt

# CI size: one depot, 12 trucks. About a minute.
run-small:
	uv run fleet-routing --profile small --refresh

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

test:
	uv run pytest -q

check: lint test

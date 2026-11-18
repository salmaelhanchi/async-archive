.PHONY: dev lint format types test test-unit test-integration demo docker-demo clean

dev:  ## install everything and the git hooks
	uv sync
	uv run pre-commit install

lint:  ## ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

types:
	uv run mypy

test-unit:
	uv run pytest tests/unit

test-integration:
	uv run pytest tests/integration

test: test-unit test-integration

demo:  ## dry run of the real CLI against the local archive server (85 listings, 2,125 files)
	uv run python -m bench.demo --style nginx

docker-demo:  ## the same, in containers
	docker compose -f deploy/docker-compose.yml up --build --abort-on-container-exit --exit-code-from mirror

clean:
	rm -rf data/demo-*.sqlite* .pytest_cache .mypy_cache .ruff_cache

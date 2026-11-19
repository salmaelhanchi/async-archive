.PHONY: dev lint format types test test-unit test-integration demo demo-mirror kill-resume docker-demo clean

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

demo-mirror:  ## download a smaller tree for real, with published checksums
	uv run python -m bench.demo --style nginx --mirror --port 8471 --depth 2 --files 10 --max-size 2000000 --checksums

kill-resume:  ## M1 proof: kill the CLI at random moments 20 times, resume, compare every byte
	uv run python -m bench.kill_resume --trials 20

docker-demo:  ## the same, in containers
	docker compose -f deploy/docker-compose.yml up --build --abort-on-container-exit --exit-code-from mirror

clean:
	rm -rf data/demo-* data/kill-resume .pytest_cache .mypy_cache .ruff_cache

# Multi-stage build. The runtime image holds only the virtualenv and runs as a non-root user.

FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# Local archive server for the compose demo (needs the dev group for aiohttp).
FROM build AS bench
RUN uv sync --locked --no-editable
COPY bench ./bench
RUN useradd --create-home --uid 10001 bench
USER bench
EXPOSE 8080
CMD ["/app/.venv/bin/python", "-m", "bench.archive_server", "--host", "0.0.0.0", "--port", "8080"]

FROM python:3.12-slim AS runtime
RUN useradd --create-home --uid 10001 mirror && mkdir /data && chown mirror:mirror /data
WORKDIR /app
COPY --from=build --chown=mirror:mirror /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    ARCHIVE_MIRROR_STATE_DB=/data/state.sqlite
USER mirror
VOLUME ["/data"]
ENTRYPOINT ["archive-mirror"]
CMD ["--help"]

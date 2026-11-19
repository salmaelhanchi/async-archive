"""Shared helpers for integration tests: a running archive server and run settings."""

import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp.test_utils import TestServer
from bench.archive_server import ServerConfig, make_app
from bench.tree import Archive

from archive_mirror.domain.urls import local_relative_path
from archive_mirror.settings import Settings

SettingsFactory = Callable[..., Settings]


@asynccontextmanager
async def archive(config: ServerConfig) -> AsyncIterator[TestServer]:
    server = TestServer(make_app(config))
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


def make_settings(tmp_path: Path) -> SettingsFactory:
    def make(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "state_db": tmp_path / "state.sqlite",
            "mirror_root": tmp_path / "mirror",
            "per_host_rate": 500.0,
            "per_host_burst": 50,
            "backoff_base_s": 0.01,
            "min_free_bytes": 0,
            "http2": False,
        }
        return Settings(_env_file=None, **(values | overrides))  # type: ignore[arg-type]

    return make


def mismatches(root: Path, base_url: str, expected: Archive) -> list[str]:
    """Paths whose mirrored copy is missing or not byte-identical, plus stray part files."""
    problems: list[str] = []
    for path in expected.files:
        local = root / local_relative_path(base_url.rstrip("/") + path)
        if not local.is_file():
            problems.append(f"missing {path}")
        elif hashlib.sha256(local.read_bytes()).hexdigest() != expected.digest(path):
            problems.append(f"differs {path}")
    problems += [f"leftover {p.name}" for p in root.rglob("*.part*")]
    return problems

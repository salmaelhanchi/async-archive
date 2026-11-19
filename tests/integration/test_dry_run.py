"""End to end: the real HTTP stack and SQLite against the local archive server."""

import time

import aiosqlite
import pytest
from bench.archive_server import HITS, STYLES, ServerConfig
from bench.tree import TreeSpec, build_tree, expected_files

from archive_mirror.composition import run_dry_run
from archive_mirror.domain.status import ListingStatus
from tests.integration.serving import SettingsFactory, archive

pytestmark = pytest.mark.integration

SPEC = TreeSpec(depth=2, dirs_per_dir=3, files_per_dir=4)  # 13 directories, 52 files
TREE = build_tree(SPEC)
FILES = expected_files(TREE)


@pytest.mark.parametrize("style", STYLES)
async def test_dry_run_finds_every_file_in_each_format(
    style: str, settings: SettingsFactory
) -> None:
    async with archive(ServerConfig(style=style, tree=SPEC)) as server:
        summary = await run_dry_run(settings(), [str(server.make_url("/"))], "run-1")

    assert summary.files == len(FILES) == 52
    assert summary.listings[ListingStatus.LISTED] == len(TREE) == 13
    assert summary.listings[ListingStatus.FAILED] == 0
    assert summary.listings_by_format == {style: 13}
    if style == "apache":  # rounded sizes, so only close to the real total
        assert summary.files_size_approximate > 0
        assert summary.bytes_listed == pytest.approx(sum(FILES.values()), rel=0.01)
    else:
        assert summary.files_size_approximate == 0
        assert summary.bytes_listed == sum(FILES.values())


async def test_second_run_is_idempotent(settings: SettingsFactory) -> None:
    async with archive(ServerConfig(tree=SPEC)) as server:
        url = str(server.make_url("/"))
        first = await run_dry_run(settings(), [url], "run-1")
        second = await run_dry_run(settings(), [url], "run-2")

    async with (
        aiosqlite.connect(settings().state_db) as db,
        db.execute("SELECT COUNT(*), COUNT(DISTINCT first_seen_run) FROM files") as cursor,
    ):
        assert await cursor.fetchone() == (52, 1)
    assert (first.files, first.bytes_listed) == (second.files, second.bytes_listed)


async def test_robots_txt_is_fetched_once_and_obeyed(settings: SettingsFactory) -> None:
    config = ServerConfig(tree=SPEC, robots_txt="User-agent: *\nDisallow: /dir-0/\n")
    async with archive(config) as server:
        summary = await run_dry_run(settings(), [str(server.make_url("/"))], "run-1")
        hits = server.app[HITS]

    assert hits["/robots.txt"] == 1
    assert not any(path.startswith("/dir-0/") for path in hits)
    assert summary.files == len([p for p in FILES if not p.startswith("/dir-0/")])
    assert summary.listings[ListingStatus.SKIPPED] == 1


async def test_503s_are_retried_and_attempts_recorded(settings: SettingsFactory) -> None:
    config = ServerConfig(tree=SPEC, flaky_paths={"/dir-1/": 2})
    async with archive(config) as server:
        summary = await run_dry_run(settings(), [str(server.make_url("/"))], "run-1")
        flaky_url = str(server.make_url("/dir-1/"))

    async with (
        aiosqlite.connect(settings().state_db) as db,
        db.execute("SELECT attempts FROM listings WHERE url = ?", (flaky_url,)) as cursor,
    ):
        assert await cursor.fetchone() == (3,)
    assert summary.files == 52


async def test_per_host_rate_limit_is_honoured(settings: SettingsFactory) -> None:
    rate = 40.0
    async with archive(ServerConfig(tree=SPEC)) as server:
        started = time.perf_counter()
        await run_dry_run(
            settings(per_host_rate=rate, per_host_burst=1, listing_workers=8, max_in_flight=8),
            [str(server.make_url("/"))],
            "run-1",
        )
        elapsed = time.perf_counter() - started
        requests = sum(server.app[HITS].values())

    # robots.txt + 13 listings, one token each, spaced 1/rate apart
    assert requests == 14
    assert elapsed >= (requests - 1) / rate

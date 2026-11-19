"""End to end downloads: real HTTP stack, SQLite and disk against the local archive server.

Every test ends by comparing each mirrored file's SHA-256 with the bytes the server holds.
"""

from dataclasses import replace

import aiosqlite
import pytest
from bench.archive_server import HITS, RANGE_HITS, STYLES, ServerConfig
from bench.tree import Archive, TreeSpec

from archive_mirror.adapters.state_lock import exclusive_state
from archive_mirror.composition import run_dry_run, run_mirror
from archive_mirror.domain.errors import StateLocked
from archive_mirror.domain.status import FileStatus
from tests.integration.serving import SettingsFactory, archive, mismatches

pytestmark = pytest.mark.integration

# 3 directories, 9 data files with a .sha256 sidecar each: 18 files, up to 200 kB
SPEC = TreeSpec(depth=1, dirs_per_dir=2, files_per_dir=3, max_size=200_000, checksums=True)
ARCHIVE = Archive(SPEC)
DATA_FILES = [p for p in ARCHIVE.files if not p.endswith(".sha256")]


@pytest.mark.parametrize("style", STYLES)
async def test_every_file_arrives_byte_identical_in_each_format(
    style: str, settings: SettingsFactory
) -> None:
    config = settings()
    async with archive(ServerConfig(style=style, tree=SPEC)) as server:
        url = str(server.make_url("/"))
        summary = await run_mirror(config, [url], "run-1")

    assert mismatches(config.mirror_root, url, ARCHIVE) == []
    assert summary.files_by_status[FileStatus.DONE] == len(ARCHIVE.files) == 18
    assert summary.downloads.files_verified_by_checksum == len(DATA_FILES) == 9
    assert summary.bytes_done == ARCHIVE.total_bytes


@pytest.mark.parametrize(
    ("server_change", "expect_ranges"),
    [
        ({}, True),
        ({"validators": False}, True),  # no ETag: Range without If-Range, checksum still guards
        ({"ranges": False}, False),  # server ignores Range: each retry starts over
    ],
    ids=["ranges", "no-validators", "ranges-ignored"],
)
async def test_cut_connections_resume_and_still_match(
    server_change: dict[str, bool], expect_ranges: bool, settings: SettingsFactory
) -> None:
    big = TreeSpec(depth=1, dirs_per_dir=1, files_per_dir=3, min_size=300_000, max_size=600_000)
    expected = Archive(big)
    cuts = dict.fromkeys(expected.files, 2)  # every file is cut halfway, twice
    config = settings(max_attempts=4)
    async with archive(ServerConfig(tree=big, cut_paths=cuts, **server_change)) as server:
        url = str(server.make_url("/"))
        summary = await run_mirror(config, [url], "run-1")
        range_hits = server.app[RANGE_HITS]

    assert mismatches(config.mirror_root, url, expected) == []
    assert summary.downloads.files_done == len(expected.files) == 6
    if expect_ranges:
        assert summary.downloads.files_resumed == 6
        assert all(range_hits[p] >= 1 for p in expected.files)
    else:
        assert summary.downloads.files_resumed == 0


async def test_a_corrupt_file_fails_verification_and_a_later_run_fixes_it(
    settings: SettingsFactory,
) -> None:
    bad = DATA_FILES[0]
    config = settings()
    served = ServerConfig(tree=SPEC, corrupt_paths=frozenset({bad}))
    async with archive(served) as server:
        url = str(server.make_url("/"))
        first = await run_mirror(config, [url], "run-1")
        async with (
            aiosqlite.connect(config.state_db) as db,
            db.execute("SELECT url, last_error FROM files WHERE status = 'failed'") as cursor,
        ):
            failed = await cursor.fetchall()
        served.corrupt_paths = frozenset()  # the server now sends the right bytes
        server.app[HITS].clear()
        second = await run_mirror(config, [url], "run-2")
        refetched = {path for path, count in server.app[HITS].items() if count}

    assert (first.downloads.files_failed, first.downloads.files_done) == (1, 17)
    assert len(failed) == 1
    assert failed[0][0].endswith(bad.replace("&", "%26"))
    assert "is not the published" in failed[0][1]
    assert second.downloads.files_done == 1
    # only the file and its checksum, besides robots.txt and the listings
    assert refetched - set(ARCHIVE.tree) - {"/robots.txt"} == {bad, bad + ".sha256"}
    assert mismatches(config.mirror_root, url, ARCHIVE) == []


async def test_a_rerun_downloads_nothing_new(settings: SettingsFactory) -> None:
    config = settings()
    async with archive(ServerConfig(tree=SPEC)) as server:
        url = str(server.make_url("/"))
        await run_mirror(config, [url], "run-1")
        server.app[HITS].clear()
        second = await run_mirror(config, [url], "run-2")
        hits = server.app[HITS]

    assert second.downloads.files_done == 0
    assert not any(hits[path] for path in ARCHIVE.files)
    assert second.files_by_status[FileStatus.DONE] == 18


async def test_files_that_changed_upstream_are_downloaded_again(
    settings: SettingsFactory,
) -> None:
    config = settings()
    served = ServerConfig(tree=SPEC)
    changed = Archive(replace(SPEC, seed=8))  # same paths; new sizes, dates and bytes
    async with archive(served) as server:
        url = str(server.make_url("/"))
        await run_mirror(config, [url], "run-1")
        served.tree = changed.spec
        summary = await run_mirror(config, [url], "run-2")

    assert summary.downloads.files_done == 18
    assert mismatches(config.mirror_root, url, changed) == []


async def test_resume_downloads_what_a_dry_run_listed_without_listing_again(
    settings: SettingsFactory,
) -> None:
    config = settings()
    async with archive(ServerConfig(tree=SPEC)) as server:
        url = str(server.make_url("/"))
        await run_dry_run(config, [url], "run-1")
        listing_hits = {path: server.app[HITS][path] for path in ARCHIVE.tree}
        summary = await run_mirror(config, [], "run-2")
        hits = server.app[HITS]

    assert summary.crawl is None
    assert summary.downloads.files_done == 18
    assert {path: hits[path] for path in ARCHIVE.tree} == listing_hits  # no listing refetched
    assert mismatches(config.mirror_root, url, ARCHIVE) == []


async def test_only_one_process_may_use_a_state_file(settings: SettingsFactory) -> None:
    config = settings()
    with exclusive_state(config.state_db), pytest.raises(StateLocked):
        await run_mirror(config, [], "run-1")

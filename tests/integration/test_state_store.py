"""SQLite state store on a real database file."""

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import aiosqlite
import pytest

from archive_mirror.adapters.state_store import SCHEMA_VERSION, SqliteStateStore
from archive_mirror.domain.errors import InvalidTransition
from archive_mirror.domain.models import (
    EntryKind,
    Listing,
    ListingEntry,
    ListingFormat,
    ListingOutcome,
    RemoteFile,
)
from archive_mirror.domain.status import FileStatus, ListingStatus, RunOutcome

pytestmark = pytest.mark.integration

DIR_URL = "https://archive.example/pub/"
LISTING = Listing(url=DIR_URL, format=ListingFormat.NGINX, entries=())


def remote(name: str, size: int | None = 100, exact: bool = True) -> RemoteFile:
    entry = ListingEntry(
        url=DIR_URL + name,
        name=name,
        kind=EntryKind.FILE,
        size_bytes=size,
        size_exact=exact,
        modified=datetime(2024, 3, 5, 17, 39),
    )
    return RemoteFile.from_entry(entry, LISTING)


def listed(entries: int = 2) -> ListingOutcome:
    return ListingOutcome(
        url=DIR_URL,
        depth=0,
        parent_url=None,
        status=ListingStatus.LISTED,
        format=ListingFormat.NGINX,
        entries=entries,
        attempts=1,
        detail=None,
    )


async def open_run(path: Path, run_id: str) -> SqliteStateStore:
    store = await SqliteStateStore.open(path, run_id)
    await store.start_run(mode="dry_run", start_urls=[DIR_URL], config={}, tool_version="test")
    return store


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStateStore]:
    s = await open_run(tmp_path / "state.sqlite", "run-a")
    yield s
    await s.close()


async def test_schema_is_created_with_a_version(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.sqlite"
    store = await SqliteStateStore.open(path, "run-a")
    await store.close()

    async with aiosqlite.connect(path) as db, db.execute("PRAGMA user_version") as cursor:
        assert await cursor.fetchone() == (SCHEMA_VERSION,)


async def test_check_constraint_rejects_unknown_status(store: SqliteStateStore) -> None:
    await store.record_listing(listed(), [remote("a.bin")])

    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        await store._db.execute("UPDATE files SET status = 'half-done'")


async def test_rerun_updates_rows_instead_of_duplicating_them(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    first = await open_run(path, "run-a")
    await first.record_listing(listed(), [remote("a.bin"), remote("b.bin")])
    await first.close()

    second = await open_run(path, "run-b")
    await second.record_listing(listed(), [remote("a.bin"), remote("b.bin")])
    async with second._db.execute(
        "SELECT COUNT(*), MIN(first_seen_run), MIN(last_seen_run) FROM files"
    ) as cursor:
        row = await cursor.fetchone()
    summary = await second.run_summary()
    await second.close()

    assert row == (2, "run-a", "run-b")
    assert summary.files == 2


async def test_done_file_goes_back_to_listed_when_upstream_changes(
    store: SqliteStateStore,
) -> None:
    same, grown = remote("a.bin"), remote("b.bin")
    await store.record_listing(listed(), [same, grown])
    for f in (same, grown):
        await store.transition(f.file_id, FileStatus.LISTED, FileStatus.DOWNLOADING)
        await store.transition(f.file_id, FileStatus.DOWNLOADING, FileStatus.DONE)

    await store.record_listing(listed(), [same, replace(grown, size_bytes=999)])

    assert await store.file_status(same.file_id) is FileStatus.DONE
    assert await store.file_status(grown.file_id) is FileStatus.LISTED


async def test_transition_is_compare_and_set(store: SqliteStateStore) -> None:
    f = remote("a.bin")
    await store.record_listing(listed(), [f])

    with pytest.raises(InvalidTransition):  # not allowed by the state machine
        await store.transition(f.file_id, FileStatus.LISTED, FileStatus.DONE)
    with pytest.raises(InvalidTransition, match="not in status"):  # allowed, but stale
        await store.transition(f.file_id, FileStatus.FAILED, FileStatus.DOWNLOADING)

    await store.transition(f.file_id, FileStatus.LISTED, FileStatus.DOWNLOADING)
    async with store._db.execute("SELECT attempts FROM files") as cursor:
        assert await cursor.fetchone() == (1,)


async def test_summary_counts_sizes_formats_and_listing_outcomes(store: SqliteStateStore) -> None:
    files = [remote("a.tar.gz", 100), remote("b.txt", 2048, exact=False), remote("c", None)]
    await store.record_listing(listed(3), files)
    failed = ListingOutcome(
        DIR_URL + "x/", 1, DIR_URL, ListingStatus.FAILED, None, 0, 5, "HTTP 500"
    )
    await store.record_listing(failed, [])
    await store.finish_run(RunOutcome.COMPLETED)

    summary = await store.run_summary()

    assert (summary.files, summary.bytes_listed, summary.archives) == (3, 2148, 1)
    assert (summary.files_size_approximate, summary.files_size_unknown) == (1, 1)
    assert summary.listings[ListingStatus.LISTED] == 1
    assert summary.listings[ListingStatus.FAILED] == 1
    assert summary.listings_by_format == {"nginx": 1}


async def test_a_failed_write_rolls_back_the_whole_listing(store: SqliteStateStore) -> None:
    bad = replace(remote("b.bin"), size_bytes=-1)  # violates CHECK (size_bytes >= 0)

    with pytest.raises(sqlite3.IntegrityError):
        await store.record_listing(listed(), [remote("a.bin"), bad])

    async with store._db.execute("SELECT COUNT(*) FROM files") as cursor:
        assert await cursor.fetchone() == (0,)
    async with store._db.execute("SELECT COUNT(*) FROM listings") as cursor:
        assert await cursor.fetchone() == (0,)

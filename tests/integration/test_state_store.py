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
    DownloadedFile,
    EntryKind,
    Listing,
    ListingEntry,
    ListingFormat,
    ListingOutcome,
    RemoteFile,
)
from archive_mirror.domain.status import FileStatus, ListingStatus, RunOutcome

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent.parent / "fixtures"

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


PROOF = DownloadedFile(sha256="f" * 64, size=100, verified_by="size", etag='"e"')


async def finish(store: SqliteStateStore, f: RemoteFile) -> None:
    await store.transition(f.file_id, FileStatus.LISTED, FileStatus.DOWNLOADING)
    await store.mark_done(f.file_id, PROOF)


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
        await finish(store, f)

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


# --- version 2: downloads ---------------------------------------------------------------


@pytest.mark.parametrize("version", [1, 2])
async def test_an_older_file_is_migrated_to_the_same_schema_as_a_new_one(
    tmp_path: Path, version: int
) -> None:
    old = tmp_path / "old.sqlite"
    with sqlite3.connect(old) as db:
        db.executescript((FIXTURES / "schema" / f"v{version}.sql").read_text(encoding="utf-8"))
        db.execute(f"PRAGMA user_version = {version}")
    await (await SqliteStateStore.open(old, "run-a")).close()
    await (await SqliteStateStore.open(tmp_path / "new.sqlite", "run-a")).close()

    def shape(path: Path) -> tuple[object, ...]:
        with sqlite3.connect(path) as db:
            columns = db.execute("SELECT name, type FROM pragma_table_info('files')").fetchall()
            indexes = db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
            ).fetchall()
            version = db.execute("PRAGMA user_version").fetchone()
        return sorted(columns), sorted(indexes), version

    assert shape(old) == shape(tmp_path / "new.sqlite")


async def test_record_listing_returns_only_files_not_done(store: SqliteStateStore) -> None:
    a, b = remote("a.bin"), remote("b.bin")
    pending = await store.record_listing(listed(), [a, b])
    assert [(p.file.name, p.status) for p in pending] == [
        ("a.bin", FileStatus.LISTED),
        ("b.bin", FileStatus.LISTED),
    ]

    await finish(store, a)
    await store.transition(b.file_id, FileStatus.LISTED, FileStatus.DOWNLOADING)
    await store.transition(b.file_id, FileStatus.DOWNLOADING, FileStatus.FAILED, error="HTTP 500")
    pending = await store.record_listing(listed(), [a, b])

    assert [(p.file.name, p.status) for p in pending] == [("b.bin", FileStatus.FAILED)]


async def test_done_needs_proof(store: SqliteStateStore) -> None:
    f = remote("a.bin")
    await store.record_listing(listed(), [f])
    await store.transition(f.file_id, FileStatus.LISTED, FileStatus.DOWNLOADING)

    with pytest.raises(InvalidTransition, match="mark_done"):
        await store.transition(f.file_id, FileStatus.DOWNLOADING, FileStatus.DONE)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # not even by hand
        await store._db.execute("UPDATE files SET status = 'done'")

    await store.mark_done(f.file_id, PROOF)
    async with store._db.execute(
        "SELECT status, sha256, bytes_on_disk, verified_by, done_run FROM files"
    ) as cursor:
        assert await cursor.fetchone() == ("done", "f" * 64, 100, "size", "run-a")
    with pytest.raises(InvalidTransition, match="not in status downloading"):
        await store.mark_done(f.file_id, PROOF)


async def test_recovery_after_a_kill_resets_downloads_and_closes_old_runs(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    killed = await open_run(path, "run-a")
    a, b = remote("a.bin"), remote("b.bin")
    await killed.record_listing(listed(), [a, b])
    await killed.transition(a.file_id, FileStatus.LISTED, FileStatus.DOWNLOADING)
    await killed.close()  # no finish_run: the process "died"

    resumed = await open_run(path, "run-b")
    recovered = await resumed.recover_interrupted()
    async with resumed._db.execute("SELECT run_id, outcome FROM runs ORDER BY run_id") as c:
        runs = await c.fetchall()
    status_a = await resumed.file_status(a.file_id)
    await resumed.close()

    assert recovered == (1, 1, 0)
    assert runs == [("run-a", "interrupted"), ("run-b", "running")]
    assert status_a is FileStatus.LISTED


async def test_pending_files_pages_through_everything_not_done(store: SqliteStateStore) -> None:
    files = [remote(f"f{i}.bin") for i in range(5)]
    await store.record_listing(listed(5), files)
    await finish(store, files[0])

    pending = [p async for p in store.pending_files(batch_size=2)]

    assert sorted(p.file.name for p in pending) == ["f1.bin", "f2.bin", "f3.bin", "f4.bin"]
    assert pending[0].file == next(f for f in files if f.file_id == pending[0].file.file_id)
    totals, done_bytes = await store.file_totals()
    assert (totals[FileStatus.DONE], totals[FileStatus.LISTED], done_bytes) == (1, 4, 100)

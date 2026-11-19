"""The extraction state machine on a real SQLite file."""

import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest

from archive_mirror.adapters.extract_state import SqliteExtractState
from archive_mirror.adapters.state_store import SqliteStateStore
from archive_mirror.domain.errors import InvalidTransition
from archive_mirror.domain.extraction import ExtractResult
from archive_mirror.domain.models import RemoteFile
from archive_mirror.domain.status import ExtractStatus, FileStatus
from tests.integration.test_state_store import FIXTURES, finish, listed, open_run, remote

pytestmark = pytest.mark.integration

EXTRACTING, PENDING = ExtractStatus.EXTRACTING, ExtractStatus.PENDING


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStateStore]:
    s = await open_run(tmp_path / "state.sqlite", "run-a")
    yield s
    await s.close()


async def extract_status(store: SqliteStateStore, f: RemoteFile) -> str | None:
    async with store.connection.execute(
        "SELECT extract_status FROM files WHERE file_id = ?", (f.file_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def test_a_done_archive_is_queued_for_extraction_and_a_plain_file_is_not(
    store: SqliteStateStore,
) -> None:
    archive, plain = remote("data.tar.gz"), remote("notes.txt")
    await store.record_listing(listed(), [archive, plain])
    assert await extract_status(store, archive) is None  # not before the download is done

    await finish(store, archive)
    await finish(store, plain)

    assert await extract_status(store, archive) == "pending"
    assert await extract_status(store, plain) is None


async def test_extracted_is_recorded_with_its_counts_and_only_from_extracting(
    store: SqliteStateStore,
) -> None:
    archive = remote("data.zip")
    await store.record_listing(listed(), [archive])
    await finish(store, archive)
    state = SqliteExtractState(store)

    with pytest.raises(InvalidTransition):
        await state.mark_extracted(archive.file_id, ExtractResult(2, 20, 0))
    await state.extract_transition(archive.file_id, PENDING, EXTRACTING)
    await state.mark_extracted(archive.file_id, ExtractResult(2, 20, 0))

    async with store.connection.execute(
        "SELECT extract_status, extracted_files, extracted_bytes FROM files"
    ) as cursor:
        assert await cursor.fetchone() == ("done", 2, 20)
    assert (await state.totals())[ExtractStatus.DONE] == 1


async def test_check_constraints_reject_impossible_extraction_rows(
    store: SqliteStateStore,
) -> None:
    archive, plain = remote("data.zip"), remote("notes.txt")
    await store.record_listing(listed(), [archive, plain])

    for bad in (
        "UPDATE files SET extract_status = 'pending' WHERE name = 'data.zip'",  # not downloaded
        "UPDATE files SET extract_status = 'half' WHERE name = 'data.zip'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            await store.connection.execute(bad)
    await finish(store, archive)
    await finish(store, plain)
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # not an archive
        await store.connection.execute(
            "UPDATE files SET extract_status = 'pending' WHERE name = 'notes.txt'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):  # done without counts
        await store.connection.execute(
            "UPDATE files SET extract_status = 'done' WHERE name = 'data.zip'"
        )


async def test_a_new_upstream_copy_resets_the_extraction(store: SqliteStateStore) -> None:
    archive = remote("data.zip")
    await store.record_listing(listed(), [archive])
    await finish(store, archive)
    state = SqliteExtractState(store)
    await state.extract_transition(archive.file_id, PENDING, EXTRACTING)
    await state.mark_extracted(archive.file_id, ExtractResult(2, 20, 0))

    await store.record_listing(listed(), [replace(archive, size_bytes=999)])

    assert await store.file_status(archive.file_id) is FileStatus.LISTED
    assert await extract_status(store, archive) is None
    await finish(store, archive)
    assert await extract_status(store, archive) == "pending"


async def test_the_backlog_holds_earlier_runs_archives_only(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    earlier = await open_run(path, "run-a")
    old, failed = remote("old.zip"), remote("failed.zip")
    await earlier.record_listing(listed(), [old, failed])
    await finish(earlier, old)
    await finish(earlier, failed)
    state = SqliteExtractState(earlier)
    await state.extract_transition(failed.file_id, PENDING, EXTRACTING)
    await state.extract_transition(failed.file_id, EXTRACTING, ExtractStatus.FAILED, error="x")
    await earlier.close()

    current = await open_run(path, "run-b")
    new = remote("new.zip")
    await current.record_listing(listed(), [new])
    await finish(current, new)  # this run hands it to the extractor directly
    backlog = [job async for job in SqliteExtractState(current).backlog(batch_size=1)]
    await current.close()

    assert sorted((j.name, j.status) for j in backlog) == [
        ("failed.zip", ExtractStatus.FAILED),
        ("old.zip", PENDING),
    ]


async def test_recovery_puts_interrupted_extractions_back_to_pending(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    killed = await open_run(path, "run-a")
    archive = remote("data.zip")
    await killed.record_listing(listed(), [archive])
    await finish(killed, archive)
    await SqliteExtractState(killed).extract_transition(archive.file_id, PENDING, EXTRACTING)
    await killed.close()

    resumed = await open_run(path, "run-b")
    recovered = await resumed.recover_interrupted()
    status = await extract_status(resumed, archive)
    await resumed.close()

    assert recovered == (1, 0, 1)
    assert status == "pending"


async def test_migrating_from_version_2_queues_archives_that_were_already_done(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript((FIXTURES / "schema" / "v2.sql").read_text(encoding="utf-8"))
        db.execute("PRAGMA user_version = 2")
        db.execute(
            "INSERT INTO runs VALUES ('run-0', 'mirror', 'completed', 't', 't', '[]', '{}', 'v')"
        )
        for name, is_archive, status in (("a.zip", 1, "done"), ("b.zip", 1, "listed")):
            sha, size = ("f" * 64, 1) if status == "done" else (None, None)
            db.execute(
                "INSERT INTO files (file_id, url, relative_path, name, size_exact, is_archive,"
                " status, listing_url, listing_format, first_seen_run, last_seen_run,"
                " updated_at, sha256, bytes_on_disk) VALUES"
                " (?, ?, ?, ?, 1, ?, ?, 'u', 'nginx', 'run-0', 'run-0', 't', ?, ?)",
                (name, "https://x/" + name, "x/" + name, name, is_archive, status, sha, size),
            )

    store = await SqliteStateStore.open(path, "run-1")
    async with store.connection.execute(
        "SELECT name, extract_status FROM files ORDER BY name"
    ) as cursor:
        rows = await cursor.fetchall()
    await store.close()

    assert rows == [("a.zip", "pending"), ("b.zip", None)]

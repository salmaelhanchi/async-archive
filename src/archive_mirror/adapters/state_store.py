"""SQLite state (via aiosqlite): runs, listings and files, with status ``CHECK``s.

One connection per run. Every write runs inside ``_transaction``: an ``asyncio.Lock`` makes
transactions take turns, so a download worker's commit can never commit half of the
listing writer's batch (see ADR 004). Every write method commits before returning, so
anything a caller reports has already reached disk ("durable before visible").
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any, Self

import aiosqlite

from archive_mirror.adapters import state_sql as sql
from archive_mirror.domain.checksums import make_ref
from archive_mirror.domain.errors import InvalidTransition
from archive_mirror.domain.models import (
    DownloadedFile,
    ListingFormat,
    ListingOutcome,
    PendingFile,
    RemoteFile,
    RunSummary,
)
from archive_mirror.domain.status import FileStatus, ListingStatus, RunOutcome, check_transition

SCHEMA_VERSION = 2


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SqliteStateStore:
    def __init__(self, db: aiosqlite.Connection, run_id: str) -> None:
        self._db = db
        self._lock = asyncio.Lock()
        self.run_id = run_id

    @classmethod
    async def open(cls, path: Path, run_id: str) -> Self:
        path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(path)
        try:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA synchronous = NORMAL")
            await db.execute("PRAGMA foreign_keys = ON")
            await cls._migrate(db)
        except BaseException:
            await db.close()
            raise
        return cls(db, run_id)

    @staticmethod
    async def _migrate(db: aiosqlite.Connection) -> None:
        async with db.execute("PRAGMA user_version") as cursor:
            row = await cursor.fetchone()
        version = int(row[0]) if row else 0
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"state DB schema {version} is newer than {SCHEMA_VERSION}")
        if version == SCHEMA_VERSION:
            return
        if version == 0:  # new file: create the latest schema directly
            schema = package_files("archive_mirror.adapters").joinpath("schema.sql").read_text()
            await db.executescript(schema)
        else:
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in sql.MIGRATIONS[target]:
                    await db.execute(statement)
        await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        await db.commit()

    async def close(self) -> None:
        await self._db.close()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            try:
                yield self._db
                await self._db.commit()
            except BaseException:
                await self._db.rollback()
                raise

    # runs -------------------------------------------------------------------------------

    async def start_run(
        self, *, mode: str, start_urls: Sequence[str], config: dict[str, Any], tool_version: str
    ) -> None:
        async with self._transaction() as db:
            await db.execute(
                sql.INSERT_RUN,
                (
                    self.run_id,
                    mode,
                    RunOutcome.RUNNING.value,
                    _now(),
                    json.dumps(list(start_urls)),
                    json.dumps(config, default=str, sort_keys=True),
                    tool_version,
                ),
            )

    async def recover_interrupted(self) -> tuple[int, int]:
        """Clean up after a process that was killed. Call only while holding the state lock.

        Earlier runs still marked ``running`` become ``interrupted``, and files left in
        ``downloading`` go back to ``listed``; their ``.part`` files are resumed later.
        Returns ``(runs, files)`` recovered.
        """
        async with self._transaction() as db:
            runs = await db.execute(
                "UPDATE runs SET outcome = 'interrupted', finished_at = ?"
                " WHERE outcome = 'running' AND run_id != ?",
                (_now(), self.run_id),
            )
            check_transition(FileStatus.DOWNLOADING, FileStatus.LISTED)
            files = await db.execute(
                "UPDATE files SET status = 'listed', updated_at = ?,"
                " last_error = 'interrupted, will resume' WHERE status = 'downloading'",
                (_now(),),
            )
        return runs.rowcount, files.rowcount

    async def finish_run(self, outcome: RunOutcome) -> None:
        async with self._transaction() as db:
            await db.execute(
                "UPDATE runs SET outcome = ?, finished_at = ? WHERE run_id = ?",
                (outcome.value, _now(), self.run_id),
            )

    # listings ---------------------------------------------------------------------------

    async def record_listing(
        self, outcome: ListingOutcome, files: Sequence[RemoteFile]
    ) -> list[PendingFile]:
        """Store a listing and its files in one transaction. Returns the files not yet done."""
        now = _now()
        async with self._transaction() as db:
            await db.execute(
                sql.UPSERT_LISTING,
                (
                    outcome.url,
                    outcome.status.value,
                    outcome.format.value if outcome.format else None,
                    outcome.depth,
                    outcome.parent_url,
                    outcome.entries,
                    outcome.attempts,
                    outcome.detail,
                    self.run_id,
                    self.run_id,
                    now,
                ),
            )
            if not files:
                return []
            await db.executemany(sql.UPSERT_FILE, [self._file_row(f, now) for f in files])
            async with db.execute(
                "SELECT file_id, status FROM files"
                " WHERE listing_url = ? AND last_seen_run = ? AND status != 'done'",
                (files[0].listing_url, self.run_id),
            ) as cursor:
                statuses = {row[0]: FileStatus(row[1]) async for row in cursor}
        return [PendingFile(f, statuses[f.file_id]) for f in files if f.file_id in statuses]

    def _file_row(self, f: RemoteFile, now: str) -> tuple[object, ...]:
        return (
            f.file_id,
            f.url,
            f.relative_path,
            f.name,
            f.size_bytes,
            int(f.size_exact),
            f.modified.isoformat() if f.modified else None,
            int(f.is_archive),
            f.listing_url,
            f.listing_format.value,
            self.run_id,
            self.run_id,
            now,
            f.checksum.url if f.checksum else None,
            f.checksum.algorithm if f.checksum else None,
        )

    # files ------------------------------------------------------------------------------

    async def file_status(self, file_id: str) -> FileStatus | None:
        async with self._db.execute("SELECT status FROM files WHERE file_id = ?", (file_id,)) as c:
            row = await c.fetchone()
        return FileStatus(row[0]) if row else None

    async def transition(
        self, file_id: str, current: FileStatus, target: FileStatus, *, error: str | None = None
    ) -> None:
        """Move one file between statuses. The ``WHERE status = ?`` makes it a compare-and-set.

        Moving to ``done`` goes through ``mark_done``, which records the proof.
        """
        if target is FileStatus.DONE:
            raise InvalidTransition("use mark_done to finish a file")
        check_transition(current, target)
        async with self._transaction() as db:
            cursor = await db.execute(
                "UPDATE files SET status = ?, last_error = ?, updated_at = ?,"
                " attempts = attempts + (? = 'downloading')"
                " WHERE file_id = ? AND status = ?",
                (target.value, error, _now(), target.value, file_id, current.value),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition(f"{file_id} is not in status {current}")

    async def mark_done(self, file_id: str, proof: DownloadedFile) -> None:
        """``downloading -> done``, with the digest and size that justify it."""
        check_transition(FileStatus.DOWNLOADING, FileStatus.DONE)
        async with self._transaction() as db:
            cursor = await db.execute(
                "UPDATE files SET status = 'done', sha256 = ?, bytes_on_disk = ?,"
                " verified_by = ?, etag = ?, done_run = ?, last_error = NULL, updated_at = ?"
                " WHERE file_id = ? AND status = 'downloading'",
                (
                    proof.sha256,
                    proof.size,
                    proof.verified_by,
                    proof.etag,
                    self.run_id,
                    _now(),
                    file_id,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition(f"{file_id} is not in status downloading")

    async def pending_files(self, batch_size: int = 500) -> AsyncIterator[PendingFile]:
        """Every file not yet done, in ``file_id`` order, read in batches (keyset paging)."""
        after = ""
        while True:
            async with self._db.execute(sql.SELECT_PENDING, (after, batch_size)) as cursor:
                rows = list(await cursor.fetchall())
            for row in rows:
                yield _pending_from_row(row)
            if len(rows) < batch_size:
                return
            after = rows[-1][0]

    async def file_totals(self) -> tuple[dict[FileStatus, int], int]:
        """Files by status across the whole state database, and bytes of done files."""
        totals = dict.fromkeys(FileStatus, 0)
        done_bytes = 0
        async with self._db.execute(
            "SELECT status, COUNT(*), COALESCE(SUM(bytes_on_disk), 0) FROM files GROUP BY status"
        ) as cursor:
            async for status, count, size in cursor:
                totals[FileStatus(status)] = count
                if status == FileStatus.DONE.value:
                    done_bytes = size
        return totals, done_bytes

    async def run_summary(self) -> RunSummary:
        async with self._db.execute(sql.SUMMARY_FILES, (self.run_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:  # an aggregate query always returns one row
            raise RuntimeError("summary query returned no row")
        async with self._db.execute(sql.SUMMARY_LISTINGS, (self.run_id,)) as cursor:
            groups = await cursor.fetchall()
        by_status: dict[ListingStatus, int] = dict.fromkeys(ListingStatus, 0)
        by_format: dict[str, int] = {}
        for status, fmt, count in groups:
            by_status[ListingStatus(status)] += count
            if status == ListingStatus.LISTED.value:
                by_format[fmt] = by_format.get(fmt, 0) + count
        return RunSummary(
            run_id=self.run_id,
            files=row[0],
            files_size_unknown=row[1],
            files_size_approximate=row[2],
            bytes_listed=row[3],
            archives=row[4],
            files_already_done=row[5],
            listings=by_status,
            listings_by_format=by_format,
        )


def _pending_from_row(row: Sequence[Any]) -> PendingFile:
    (fid, url, rel, name, size, exact, modified, archive, listing_url, fmt, c_url, c_algo) = row[
        :12
    ]
    remote = RemoteFile(
        file_id=fid,
        url=url,
        relative_path=rel,
        name=name,
        size_bytes=size,
        size_exact=bool(exact),
        modified=datetime.fromisoformat(modified) if modified else None,
        is_archive=bool(archive),
        listing_url=listing_url,
        listing_format=ListingFormat(fmt),
        checksum=make_ref(c_url, c_algo) if c_url and c_algo else None,
    )
    return PendingFile(remote, FileStatus(row[12]))

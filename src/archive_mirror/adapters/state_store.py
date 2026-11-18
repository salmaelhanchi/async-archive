"""SQLite state (via aiosqlite): runs, listings and files, with status ``CHECK``s.

One connection, one writer. Every write method commits before returning, so anything a
caller reports has already reached disk ("durable before visible").
"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.resources import files as package_files
from pathlib import Path
from typing import Any, Self

import aiosqlite

from archive_mirror.domain.errors import InvalidTransition
from archive_mirror.domain.models import ListingOutcome, RemoteFile, RunSummary
from archive_mirror.domain.status import (
    FileStatus,
    ListingStatus,
    RunOutcome,
    check_transition,
)

SCHEMA_VERSION = 1

_UPSERT_FILE = """
INSERT INTO files (
    file_id, url, relative_path, name, size_bytes, size_exact, modified, is_archive, status,
    listing_url, listing_format, first_seen_run, last_seen_run, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'listed', ?, ?, ?, ?, ?)
ON CONFLICT (file_id) DO UPDATE SET
    status = CASE
        WHEN files.status = 'done'
             AND (files.size_bytes IS NOT excluded.size_bytes
                  OR files.modified IS NOT excluded.modified)
        THEN 'listed' ELSE files.status END,
    relative_path = excluded.relative_path,
    name = excluded.name,
    size_bytes = excluded.size_bytes,
    size_exact = excluded.size_exact,
    modified = excluded.modified,
    is_archive = excluded.is_archive,
    listing_url = excluded.listing_url,
    listing_format = excluded.listing_format,
    last_seen_run = excluded.last_seen_run,
    updated_at = excluded.updated_at
"""

_UPSERT_LISTING = """
INSERT INTO listings (
    url, status, format, depth, parent_url, entries, attempts, detail,
    first_seen_run, last_run, updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (url) DO UPDATE SET
    status = excluded.status,
    format = excluded.format,
    depth = excluded.depth,
    parent_url = excluded.parent_url,
    entries = excluded.entries,
    attempts = excluded.attempts,
    detail = excluded.detail,
    last_run = excluded.last_run,
    updated_at = excluded.updated_at
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SqliteStateStore:
    def __init__(self, db: aiosqlite.Connection, run_id: str) -> None:
        self._db = db
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
        if version < SCHEMA_VERSION:
            schema = package_files("archive_mirror.adapters").joinpath("schema.sql").read_text()
            await db.executescript(schema)
            await db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await db.commit()

    async def close(self) -> None:
        await self._db.close()

    async def start_run(
        self, *, mode: str, start_urls: Sequence[str], config: dict[str, Any], tool_version: str
    ) -> None:
        await self._db.execute(
            "INSERT INTO runs (run_id, mode, outcome, started_at, start_urls, config, tool_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
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
        await self._db.commit()

    async def finish_run(self, outcome: RunOutcome) -> None:
        await self._db.execute(
            "UPDATE runs SET outcome = ?, finished_at = ? WHERE run_id = ?",
            (outcome.value, _now(), self.run_id),
        )
        await self._db.commit()

    async def record_listing(self, outcome: ListingOutcome, files: Sequence[RemoteFile]) -> None:
        now = _now()
        try:
            await self._db.execute(
                _UPSERT_LISTING,
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
            await self._db.executemany(_UPSERT_FILE, [self._file_row(f, now) for f in files])
            await self._db.commit()
        except BaseException:
            await self._db.rollback()
            raise

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
        )

    async def file_status(self, file_id: str) -> FileStatus | None:
        async with self._db.execute("SELECT status FROM files WHERE file_id = ?", (file_id,)) as c:
            row = await c.fetchone()
        return FileStatus(row[0]) if row else None

    async def transition(
        self, file_id: str, current: FileStatus, target: FileStatus, *, error: str | None = None
    ) -> None:
        """Move one file between statuses. The ``WHERE status = ?`` makes it a compare-and-set."""
        check_transition(current, target)
        cursor = await self._db.execute(
            "UPDATE files SET status = ?, last_error = ?, updated_at = ?,"
            " attempts = attempts + (? = 'downloading')"
            " WHERE file_id = ? AND status = ?",
            (target.value, error, _now(), target.value, file_id, current.value),
        )
        await self._db.commit()
        if cursor.rowcount != 1:
            raise InvalidTransition(f"{file_id} is not in status {current}")

    async def run_summary(self) -> RunSummary:
        async with self._db.execute(
            "SELECT COUNT(*), COALESCE(SUM(size_bytes IS NULL), 0),"
            " COALESCE(SUM(size_bytes IS NOT NULL AND size_exact = 0), 0),"
            " COALESCE(SUM(size_bytes), 0), COALESCE(SUM(is_archive), 0),"
            " COALESCE(SUM(status = 'done'), 0)"
            " FROM files WHERE last_seen_run = ?",
            (self.run_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:  # an aggregate query always returns one row
            raise RuntimeError("summary query returned no row")
        async with self._db.execute(
            "SELECT status, format, COUNT(*) FROM listings WHERE last_run = ?"
            " GROUP BY status, format",
            (self.run_id,),
        ) as cursor:
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

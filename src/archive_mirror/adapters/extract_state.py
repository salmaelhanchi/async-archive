"""The extraction state machine in SQLite, on the same connection and lock as the store."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from archive_mirror.adapters import state_sql as sql
from archive_mirror.adapters.state_store import SqliteStateStore
from archive_mirror.domain.errors import InvalidTransition
from archive_mirror.domain.extraction import ExtractResult
from archive_mirror.domain.models import ArchiveJob
from archive_mirror.domain.status import ExtractStatus, check_extract_transition


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class SqliteExtractState:
    def __init__(self, store: SqliteStateStore) -> None:
        self._store = store

    async def extract_transition(
        self,
        file_id: str,
        current: ExtractStatus,
        target: ExtractStatus,
        *,
        error: str | None = None,
    ) -> None:
        """Compare-and-set, like ``SqliteStateStore.transition``. ``done`` goes through
        ``mark_extracted``, which records the counts."""
        if target is ExtractStatus.DONE:
            raise InvalidTransition("use mark_extracted to finish an extraction")
        check_extract_transition(current, target)
        async with self._store.transaction() as db:
            cursor = await db.execute(
                "UPDATE files SET extract_status = ?, extract_error = ?, updated_at = ?"
                " WHERE file_id = ? AND extract_status = ?",
                (target.value, error, _now(), file_id, current.value),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition(f"{file_id} is not in extract status {current}")

    async def mark_extracted(self, file_id: str, result: ExtractResult) -> None:
        check_extract_transition(ExtractStatus.EXTRACTING, ExtractStatus.DONE)
        async with self._store.transaction() as db:
            cursor = await db.execute(
                "UPDATE files SET extract_status = 'done', extract_error = NULL,"
                " extracted_files = ?, extracted_bytes = ?, updated_at = ?"
                " WHERE file_id = ? AND extract_status = 'extracting'",
                (result.files, result.bytes, _now(), file_id),
            )
            if cursor.rowcount != 1:
                raise InvalidTransition(f"{file_id} is not in extract status extracting")

    async def backlog(self, batch_size: int = 500) -> AsyncIterator[ArchiveJob]:
        """Archives that earlier runs downloaded but did not unpack, keyset-paged."""
        after = ""
        while True:
            async with self._store.connection.execute(
                sql.SELECT_PENDING_EXTRACTIONS, (self._store.run_id, after, batch_size)
            ) as cursor:
                rows = list(await cursor.fetchall())
            for file_id, relative_path, name, status in rows:
                yield ArchiveJob(file_id, relative_path, name, ExtractStatus(status))
            if len(rows) < batch_size:
                return
            after = rows[-1][0]

    async def totals(self) -> dict[ExtractStatus, int]:
        """Archives by extract status across the whole state database."""
        totals = dict.fromkeys(ExtractStatus, 0)
        async with self._store.connection.execute(
            "SELECT extract_status, COUNT(*) FROM files"
            " WHERE extract_status IS NOT NULL GROUP BY extract_status"
        ) as cursor:
            async for status, count in cursor:
                totals[ExtractStatus(status)] = count
        return totals

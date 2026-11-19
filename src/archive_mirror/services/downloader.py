"""Download workers: take pending files from a bounded queue, fetch, verify, finish.

Shape of the pipeline::

    listing writer --(after commit)--> [bounded download queue] --> N workers, each file:
        1. budget before request: run byte budget and free disk space, else skip (stays listed)
        2. listed|failed -> downloading            compare-and-set in SQLite, attempts + 1
        3. transfer into <path>.part               resumes with Range after any interruption
        4. verify size, and checksum if published  hashing runs in a thread
        5. fsync + atomic rename to <path>
        6. downloading -> done, with sha256 + size committed; then counters and logs move
        7. an archive is handed to the extract queue (bounded too)

A file is ``done`` only after its bytes are at the final path, so a kill between 5 and 6
costs one re-download, never a missing file. A kill anywhere else leaves a ``.part`` that
the next run resumes (see ADR 004).

Cancellation (Ctrl+C, see ADR 005): during 1-4 the row is rolled back to ``listed`` and the
``.part`` stays for the range resume. Steps 5-6 are short and are shielded: once a file is
verified, a cancellation waits for it to be finished and recorded.

The queue is bounded, so the listing writer blocks when downloads fall behind, which in
turn blocks the listers: memory stays flat however large the archive is.
"""

import asyncio
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

import structlog

from archive_mirror import metrics
from archive_mirror.domain.errors import FetchFailed, InvalidTransition, VerificationFailed
from archive_mirror.domain.models import (
    ArchiveJob,
    DownloadedFile,
    DownloadReport,
    PendingFile,
    RemoteFile,
    TransferResult,
)
from archive_mirror.domain.sizes import size_matches
from archive_mirror.domain.status import ExtractStatus, FileStatus
from archive_mirror.services.ports import (
    ChecksumSource,
    DownloadStateWriter,
    FileTransfer,
    PartStorage,
)
from archive_mirror.services.writes import claim, write_through


@dataclass(frozen=True, slots=True)
class DownloadLimits:
    workers: int
    queue_size: int
    max_bytes: int | None  # byte budget for this run, counted by listed size
    min_free_bytes: int  # never let a download take the disk below this


OnArchive = Callable[[ArchiveJob], Awaitable[None]]


@dataclass
class DownloadStats:
    queued: int = 0
    queued_bytes: int = 0  # listed sizes of queued files
    done: int = 0
    failed: int = 0
    skipped: int = 0
    resumed: int = 0
    verified_by_checksum: int = 0
    bytes_received: int = 0

    def report(self) -> DownloadReport:
        return DownloadReport(
            files_done=self.done,
            files_failed=self.failed,
            files_skipped=self.skipped,
            files_resumed=self.resumed,
            files_verified_by_checksum=self.verified_by_checksum,
            bytes_received=self.bytes_received,
        )


def verification_problem(
    file: RemoteFile,
    result: TransferResult,
    digests: Mapping[str, str],
    expected_digest: str | None,
) -> str | None:
    """Why a finished part cannot be accepted, or ``None`` when it can."""
    if not size_matches(result.size, file.size_bytes, exact=file.size_exact):
        shown = "" if file.size_exact else "about "
        return f"size {result.size} does not match the listing ({shown}{file.size_bytes})"
    if expected_digest is not None and file.checksum is not None:
        algorithm = file.checksum.algorithm
        actual = digests[algorithm]
        if actual != expected_digest:
            return f"{algorithm} {actual[:16]}... is not the published {expected_digest[:16]}..."
    return None


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, FetchFailed):
        return exc.reason
    if isinstance(exc, VerificationFailed):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


class Downloader:
    """One run's download workers."""

    def __init__(
        self,
        *,
        transfer: FileTransfer,
        storage: PartStorage,
        checksums: ChecksumSource,
        state: DownloadStateWriter,
        limits: DownloadLimits,
        on_archive: OnArchive | None = None,
    ) -> None:
        self._transfer = transfer
        self._storage = storage
        self._checksums = checksums
        self._state = state
        self._limits = limits
        self._on_archive = on_archive
        self._queue: asyncio.Queue[PendingFile | None] = asyncio.Queue(limits.queue_size)
        self._budget_used = 0  # listed bytes of every download started this run
        self._in_flight_bytes = 0  # listed bytes of downloads not finished yet
        self.stats = DownloadStats()
        self._log = structlog.get_logger(__name__)

    async def run(self, feed: Callable[[], Awaitable[object]]) -> DownloadStats:
        """Run the workers while ``feed`` submits files; return once every file is handled."""
        async with asyncio.TaskGroup() as group:
            for index in range(self._limits.workers):
                group.create_task(self._work(), name=f"downloader-{index}")
            await feed()
            for _ in range(self._limits.workers):
                await self._queue.put(None)
        return self.stats

    async def submit(self, files: Sequence[PendingFile]) -> None:
        """Queue files for download. Blocks while the queue is full (backpressure)."""
        for item in files:
            await self._queue.put(item)
            self.stats.queued += 1
            self.stats.queued_bytes += item.file.size_bytes or 0
            metrics.QUEUE_DEPTH.labels(queue="downloads").set(self._queue.qsize())

    async def submit_all(self, files: AsyncIterable[PendingFile]) -> None:
        async for item in files:
            await self.submit([item])

    async def _work(self) -> None:
        while (item := await self._queue.get()) is not None:
            metrics.QUEUE_DEPTH.labels(queue="downloads").set(self._queue.qsize())
            await self._process(item)

    def _over_budget(self, file: RemoteFile) -> str | None:
        """Budget before request. No await between this check and the reservation."""
        size = file.size_bytes or 0
        limit = self._limits.max_bytes
        if limit is not None and self._budget_used + size > limit:
            return f"run byte budget of {limit} spent"
        free = self._storage.free_bytes() - self._in_flight_bytes - size
        if free < self._limits.min_free_bytes:
            return f"would leave less than {self._limits.min_free_bytes} bytes of free disk"
        return None

    async def _process(self, item: PendingFile) -> None:
        file = item.file
        log = self._log.bind(url=file.url)
        if reason := self._over_budget(file):
            self.stats.skipped += 1
            metrics.FILES_FINISHED.labels(outcome="skipped").inc()
            log.info("download_skipped", reason=reason)
            return
        reserved = file.size_bytes or 0
        self._budget_used += reserved
        self._in_flight_bytes += reserved
        try:
            await claim(
                self._state.transition(file.file_id, item.status, FileStatus.DOWNLOADING),
                undo=lambda: self._roll_back(file),
            )
        except InvalidTransition as exc:  # the row moved on since it was queued
            self._budget_used -= reserved
            self._in_flight_bytes -= reserved
            log.warning("download_not_claimed", reason=str(exc))
            return
        try:
            proof, result = await self._fetch_verified(file)
        except (FetchFailed, VerificationFailed, OSError) as exc:
            # OSError: a local problem with this one file (path too long, permissions).
            # It fails the file, not the run; the next run tries it again.
            await self._fail(file, exc)
            return
        except asyncio.CancelledError:
            await self._roll_back(file)
            raise
        finally:
            self._in_flight_bytes -= reserved

        # Verified bytes are finished and recorded even if a Ctrl+C arrives meanwhile.
        if not await write_through(self._finish(file, proof)):
            return
        self.stats.done += 1
        self.stats.resumed += result.resumed_from > 0
        self.stats.verified_by_checksum += proof.verified_by == "checksum"
        metrics.FILES_FINISHED.labels(outcome="done").inc()
        log.info(
            "download_done",
            bytes=proof.size,
            resumed_from=result.resumed_from,
            verified_by=proof.verified_by,
        )
        if file.is_archive and self._on_archive is not None:
            job = ArchiveJob(file.file_id, file.relative_path, file.name, ExtractStatus.PENDING)
            await self._on_archive(job)  # blocks while the extract queue is full

    async def _finish(self, file: RemoteFile, proof: DownloadedFile) -> bool:
        """fsync + rename, then ``done``. False when the rename failed (the file is failed)."""
        try:
            await self._storage.finalize(file.relative_path)
        except OSError as exc:
            await self._fail(file, exc)
            return False
        await self._state.mark_done(file.file_id, proof)
        return True

    async def _roll_back(self, file: RemoteFile) -> None:
        await self._state.transition(
            file.file_id,
            FileStatus.DOWNLOADING,
            FileStatus.LISTED,
            error="interrupted, will resume",
        )

    async def _fail(self, file: RemoteFile, exc: Exception) -> None:
        reason = _failure_reason(exc)
        await write_through(
            self._state.transition(
                file.file_id, FileStatus.DOWNLOADING, FileStatus.FAILED, error=reason
            )
        )
        self.stats.failed += 1
        metrics.FILES_FINISHED.labels(outcome="failed").inc()
        self._log.warning("download_failed", url=file.url, reason=reason)

    async def _fetch_verified(self, file: RemoteFile) -> tuple[DownloadedFile, TransferResult]:
        """Bring the part to full length and verify it. Does not finalize."""
        expected = None
        if file.checksum is not None:
            expected = await self._checksums.expected(file.checksum, file.name)
        algorithms = frozenset({"sha256"})
        if expected is not None and file.checksum is not None:
            algorithms |= {file.checksum.algorithm}

        for last_try in (False, True):
            result = await self._transfer.download(file.url, file.relative_path)
            self.stats.bytes_received += result.received
            digests = await self._storage.digest_part(file.relative_path, algorithms)
            problem = verification_problem(file, result, digests, expected)
            if problem is None:
                if expected is not None:
                    verified_by = "checksum"
                elif file.size_exact or result.total is not None:
                    verified_by = "size"
                else:
                    verified_by = "none"
                proof = DownloadedFile(digests["sha256"], result.size, verified_by, result.etag)
                return proof, result
            self._storage.discard_part(file.relative_path)
            # A resumed part may join an old head to a new tail when the server sends no
            # validator. One clean download from byte 0 settles it; a second failure is real.
            if result.resumed_from == 0 or last_try:
                raise VerificationFailed(problem)
            self._log.warning("resumed_copy_rejected", url=file.url, problem=problem)
        raise AssertionError("unreachable")

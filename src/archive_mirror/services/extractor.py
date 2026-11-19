"""Extraction workers: take downloaded archives from a bounded queue and unpack them.

    download worker --(archive done, after commit)--> [bounded extract queue] --> N workers
    earlier runs' archives (backlog) ---------------^         |
                                                             v
        pending|failed -> extracting -> unpack in a worker process -> done | failed | unsupported

The unpacking itself is CPU-bound and runs in a process pool behind the ``ArchiveExtractor``
port, so the event loop keeps streaming downloads meanwhile. The queue is bounded, so a
slow extraction slows the downloads that feed it, never memory.

Cancellation (Ctrl+C): a worker waiting on its archive rolls the row back to ``pending``.
The worker process finishes on its own, into a temporary directory the next attempt
replaces, so nothing half-written is ever recorded as ``done``.
"""

import asyncio
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass

import structlog

from archive_mirror import metrics
from archive_mirror.domain.errors import ExtractionFailed, InvalidTransition, UnsupportedArchive
from archive_mirror.domain.models import ArchiveJob, ExtractReport
from archive_mirror.domain.status import ExtractStatus
from archive_mirror.services.ports import ArchiveExtractor, ExtractStateWriter
from archive_mirror.services.writes import claim, write_through


@dataclass
class ExtractStats:
    queued: int = 0
    done: int = 0
    failed: int = 0
    unsupported: int = 0
    files: int = 0
    bytes: int = 0

    def report(self) -> ExtractReport:
        return ExtractReport(
            archives_done=self.done,
            archives_failed=self.failed,
            archives_unsupported=self.unsupported,
            files_written=self.files,
            bytes_written=self.bytes,
        )


class Extractor:
    """One run's extraction workers."""

    def __init__(
        self,
        *,
        extractor: ArchiveExtractor,
        state: ExtractStateWriter,
        workers: int,
        queue_size: int,
    ) -> None:
        self._extractor = extractor
        self._state = state
        self._workers = workers
        self._queue: asyncio.Queue[ArchiveJob | None] = asyncio.Queue(queue_size)
        self.stats = ExtractStats()
        self._log = structlog.get_logger(__name__)

    async def run(self, feed: Callable[[], Awaitable[object]]) -> ExtractStats:
        """Run the workers while ``feed`` submits archives; return once every one is handled."""
        async with asyncio.TaskGroup() as group:
            for index in range(self._workers):
                group.create_task(self._work(), name=f"extractor-{index}")
            await feed()
            for _ in range(self._workers):
                await self._queue.put(None)
        return self.stats

    async def submit(self, job: ArchiveJob) -> None:
        """Queue one archive. Blocks while the queue is full (backpressure)."""
        await self._queue.put(job)
        self.stats.queued += 1
        metrics.QUEUE_DEPTH.labels(queue="extractions").set(self._queue.qsize())

    async def submit_all(self, jobs: AsyncIterable[ArchiveJob]) -> None:
        async for job in jobs:
            await self.submit(job)

    async def _work(self) -> None:
        while (job := await self._queue.get()) is not None:
            metrics.QUEUE_DEPTH.labels(queue="extractions").set(self._queue.qsize())
            await self._process(job)

    async def _process(self, job: ArchiveJob) -> None:
        log = self._log.bind(path=job.relative_path)
        try:
            await claim(
                self._state.extract_transition(job.file_id, job.status, ExtractStatus.EXTRACTING),
                undo=lambda: self._roll_back(job),
            )
        except InvalidTransition as exc:  # handled already, or downloaded again since
            log.debug("extraction_not_claimed", reason=str(exc))
            return
        try:
            result = await self._extractor.extract(job.relative_path, job.name)
        except UnsupportedArchive as exc:
            if await self._settle(job, ExtractStatus.UNSUPPORTED, str(exc)):
                self.stats.unsupported += 1
                log.info("extraction_unsupported", reason=str(exc))
            return
        except (ExtractionFailed, OSError) as exc:
            # OSError: a local problem (disk full, path too long). A later run retries it.
            reason = str(exc)
            if not isinstance(exc, ExtractionFailed):
                reason = f"{type(exc).__name__}: {exc}"
            if await self._settle(job, ExtractStatus.FAILED, reason):
                self.stats.failed += 1
                log.warning("extraction_failed", reason=reason)
            return
        except asyncio.CancelledError:
            await self._roll_back(job)
            raise
        try:
            await write_through(self._state.mark_extracted(job.file_id, result))
        except InvalidTransition:
            self._superseded(job)
            return
        self.stats.done += 1
        self.stats.files += result.files
        self.stats.bytes += result.bytes
        metrics.ARCHIVES_FINISHED.labels(outcome="done").inc()
        log.info("extraction_done", files=result.files, bytes=result.bytes, skipped=result.skipped)

    async def _settle(self, job: ArchiveJob, target: ExtractStatus, reason: str) -> bool:
        """Leave ``extracting`` for ``target``. False when the row moved on meanwhile."""
        try:
            await write_through(
                self._state.extract_transition(
                    job.file_id, ExtractStatus.EXTRACTING, target, error=reason
                )
            )
        except InvalidTransition:
            self._superseded(job)
            return False
        if target is not ExtractStatus.PENDING:
            metrics.ARCHIVES_FINISHED.labels(outcome=target.value).inc()
        return True

    async def _roll_back(self, job: ArchiveJob) -> None:
        if await self._settle(job, ExtractStatus.PENDING, "interrupted, will extract again"):
            self._log.info("extraction_rolled_back", path=job.relative_path)

    def _superseded(self, job: ArchiveJob) -> None:
        # The listing writer reset the row while we worked: upstream has a new copy of the
        # archive. This result is stale; the new copy is downloaded and queued on its own.
        self._log.info("extraction_superseded", path=job.relative_path)

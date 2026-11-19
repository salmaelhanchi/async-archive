"""Crawl directory listings concurrently and record every file found.

Shape of the pipeline::

    start URLs -> [directory queue] -> N lister tasks -> [bounded write queue] -> 1 writer -> SQLite
                        ^                    |
                        +---- subdirectories +

* ``asyncio.TaskGroup`` owns every task: if the writer or a lister hits a bug, the others are
  cancelled and the error surfaces from ``crawl``.
* The write queue is bounded, so listers pause when the database falls behind (backpressure).
* There is a single writer, so SQLite never sees two transactions racing on one connection.
* Counters move only after the writer's commit returns: what is reported is on disk.
* In a mirror run, the writer hands the committed files that still need downloading to
  ``on_recorded`` (the download queue). It is bounded too, so slow downloads slow the crawl.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import structlog

from archive_mirror import metrics
from archive_mirror.domain.checksums import checksum_refs
from archive_mirror.domain.errors import FetchFailed, UnrecognizedListing
from archive_mirror.domain.models import ListingFormat, ListingOutcome, PendingFile, RemoteFile
from archive_mirror.domain.status import ListingStatus
from archive_mirror.domain.urls import as_directory_url, is_within
from archive_mirror.services.ports import (
    CrawlStateWriter,
    FetchPolicy,
    ListingParserFn,
    PageFetcher,
)

OnRecorded = Callable[[Sequence[PendingFile]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CrawlLimits:
    workers: int
    max_depth: int
    max_listing_pages: int
    write_queue_size: int


@dataclass
class CrawlStats:
    listings: dict[ListingStatus, int] = field(
        default_factory=lambda: dict.fromkeys(ListingStatus, 0)
    )
    files: int = 0
    finished: bool = False


@dataclass(frozen=True, slots=True)
class _Directory:
    url: str
    root: str
    depth: int
    parent: str | None


@dataclass(frozen=True, slots=True)
class _Visit:
    outcome: ListingOutcome
    files: tuple[RemoteFile, ...] = ()


class Lister:
    """One crawl per instance."""

    def __init__(
        self,
        *,
        fetcher: PageFetcher,
        parse: ListingParserFn,
        policy: FetchPolicy,
        writer: CrawlStateWriter,
        limits: CrawlLimits,
        on_recorded: OnRecorded | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._parse = parse
        self._policy = policy
        self._writer = writer
        self._limits = limits
        self._on_recorded = on_recorded
        self._directories: asyncio.Queue[_Directory | None] = asyncio.Queue()
        self._writes: asyncio.Queue[_Visit | None] = asyncio.Queue(limits.write_queue_size)
        self._seen: set[str] = set()
        self._pages_started = 0
        self.stats = CrawlStats()
        self._log = structlog.get_logger(__name__)

    async def crawl(self, start_urls: Sequence[str]) -> CrawlStats:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._write_loop(), name="state-writer")
            for index in range(self._limits.workers):
                group.create_task(self._list_loop(), name=f"lister-{index}")
            for url in start_urls:
                root = as_directory_url(url)
                self._enqueue(_Directory(url=root, root=root, depth=0, parent=None))
            await self._directories.join()
            # Every visit is in the write queue before its task_done, so this is the last item.
            for _ in range(self._limits.workers):
                self._directories.put_nowait(None)
            await self._writes.put(None)
        self.stats.finished = True
        return self.stats

    def _enqueue(self, directory: _Directory) -> None:
        if directory.url in self._seen:
            return
        self._seen.add(directory.url)
        self._directories.put_nowait(directory)
        metrics.QUEUE_DEPTH.labels(queue="directories").set(self._directories.qsize())

    async def _list_loop(self) -> None:
        while True:
            directory = await self._directories.get()
            try:
                if directory is None:
                    return
                visit = await self._visit(directory)
                await self._writes.put(visit)  # blocks while the writer is behind
                metrics.QUEUE_DEPTH.labels(queue="writes").set(self._writes.qsize())
            finally:
                self._directories.task_done()

    async def _write_loop(self) -> None:
        while True:
            visit = await self._writes.get()
            if visit is None:
                return
            pending = await self._writer.record_listing(visit.outcome, visit.files)
            outcome = visit.outcome
            self.stats.listings[outcome.status] += 1
            self.stats.files += len(visit.files)
            metrics.LISTINGS.labels(
                status=outcome.status.value, format=outcome.format or "none"
            ).inc()
            metrics.FILES_RECORDED.inc(len(visit.files))
            log = self._log.info if outcome.status is ListingStatus.LISTED else self._log.warning
            log(
                "listing_recorded",
                url=outcome.url,
                status=outcome.status.value,
                format=outcome.format,
                files=len(visit.files),
                detail=outcome.detail,
            )
            if self._on_recorded is not None and pending:
                await self._on_recorded(pending)  # blocks while the download queue is full

    async def _visit(self, directory: _Directory) -> _Visit:
        def done(
            status: ListingStatus,
            detail: str | None = None,
            *,
            fmt: ListingFormat | None = None,
            entries: int = 0,
            attempts: int = 0,
        ) -> ListingOutcome:
            return ListingOutcome(
                url=directory.url,
                depth=directory.depth,
                parent_url=directory.parent,
                status=status,
                format=fmt,
                entries=entries,
                attempts=attempts,
                detail=detail,
            )

        if directory.depth > self._limits.max_depth:
            return _Visit(done(ListingStatus.SKIPPED, f"deeper than {self._limits.max_depth}"))
        if not await self._policy.allowed(directory.url):
            return _Visit(done(ListingStatus.SKIPPED, "disallowed by robots.txt"))
        # Budget before request. No await between the check and the increment, so concurrent
        # listers cannot both take the last page.
        if self._pages_started >= self._limits.max_listing_pages:
            return _Visit(done(ListingStatus.SKIPPED, "listing page budget spent"))
        self._pages_started += 1

        try:
            page = await self._fetcher.fetch(directory.url)
        except FetchFailed as exc:
            return _Visit(done(ListingStatus.FAILED, exc.reason, attempts=exc.attempts))
        if page.status != 200:
            return _Visit(done(ListingStatus.FAILED, f"HTTP {page.status}", attempts=page.attempts))
        if page.content_type and "html" not in page.content_type.lower():
            detail = f"not HTML ({page.content_type})"
            return _Visit(done(ListingStatus.FAILED, detail, attempts=page.attempts))
        final_url = as_directory_url(page.url)
        if final_url != directory.url:
            if final_url in self._seen:
                detail = f"redirects to {final_url}, already listed"
                return _Visit(done(ListingStatus.SKIPPED, detail, attempts=page.attempts))
            self._seen.add(final_url)

        try:
            listing = self._parse(page.text, final_url)
        except UnrecognizedListing as exc:
            return _Visit(done(ListingStatus.FAILED, str(exc), attempts=page.attempts))

        for entry in listing.directories:
            if is_within(entry.url, directory.root):
                self._enqueue(
                    _Directory(entry.url, directory.root, directory.depth + 1, directory.url)
                )
        checksums = checksum_refs(listing.files)
        files = tuple(
            [
                RemoteFile.from_entry(entry, listing, checksums.get(entry.url))
                for entry in listing.files
                if is_within(entry.url, directory.root) and await self._policy.allowed(entry.url)
            ]
        )
        outcome = done(
            ListingStatus.LISTED,
            fmt=listing.format,
            entries=len(listing.entries),
            attempts=page.attempts,
        )
        return _Visit(outcome, files)

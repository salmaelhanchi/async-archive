"""Test doubles for the service ports and for time."""

import asyncio
from collections import Counter
from collections.abc import Sequence

from archive_mirror.domain.errors import FetchFailed, InvalidTransition
from archive_mirror.domain.extraction import ExtractResult
from archive_mirror.domain.models import (
    ChecksumRef,
    DownloadedFile,
    FetchedPage,
    ListingOutcome,
    PendingFile,
    RemoteFile,
    TransferResult,
)
from archive_mirror.domain.status import (
    ExtractStatus,
    FileStatus,
    check_extract_transition,
    check_transition,
)


class FakeClock:
    """A clock whose ``sleep`` advances time instantly and records every wait."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


def html_page(url: str, text: str, *, status: int = 200, attempts: int = 1) -> FetchedPage:
    return FetchedPage(
        url=url, status=status, text=text, content_type="text/html", attempts=attempts
    )


class FakeFetcher:
    def __init__(self, pages: dict[str, FetchedPage | FetchFailed], delay: float = 0.0) -> None:
        self.pages = pages
        self.delay = delay
        self.calls: Counter[str] = Counter()

    async def fetch(self, url: str) -> FetchedPage:
        self.calls[url] += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        page = self.pages.get(url)
        if page is None:
            return html_page(url, "not found", status=404)
        if isinstance(page, FetchFailed):
            raise page
        return page


class AllowPolicy:
    def __init__(self, denied_prefixes: Sequence[str] = ()) -> None:
        self.denied = tuple(denied_prefixes)

    async def allowed(self, url: str) -> bool:
        return not url.startswith(self.denied) if self.denied else True


class MemoryWriter:
    def __init__(self, delay: float = 0.0, fail_on: str | None = None) -> None:
        self.delay = delay
        self.fail_on = fail_on
        self.outcomes: list[ListingOutcome] = []
        self.files: list[RemoteFile] = []

    async def record_listing(
        self, outcome: ListingOutcome, files: Sequence[RemoteFile]
    ) -> list[PendingFile]:
        if self.delay:
            await asyncio.sleep(self.delay)
        if outcome.url == self.fail_on:
            raise RuntimeError("disk full")
        self.outcomes.append(outcome)
        self.files.extend(files)
        return [PendingFile(f, FileStatus.LISTED) for f in files]

    def by_url(self) -> dict[str, ListingOutcome]:
        return {o.url: o for o in self.outcomes}


# --- downloads --------------------------------------------------------------------------


class FakeTransfer:
    """Returns scripted results per URL (the last one repeats); can raise or be slow."""

    def __init__(self, results: dict[str, list[TransferResult | Exception]], delay: float = 0.0):
        self.results = results
        self.delay = delay
        self.calls: Counter[str] = Counter()
        self.active = 0
        self.peak_active = 0

    async def download(self, url: str, relative_path: str) -> TransferResult:
        self.calls[url] += 1
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            script = self.results[url]
            outcome = script[min(self.calls[url], len(script)) - 1]
        finally:
            self.active -= 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeStorage:
    """Digests are scripted per relative path (the last one repeats)."""

    def __init__(self, digests: dict[str, list[str]] | None = None, free: int = 10**12) -> None:
        self.digests = digests or {}
        self.free = free
        self.finalized: list[str] = []
        self.discarded: list[str] = []
        self._reads: Counter[str] = Counter()

    async def digest_part(self, relative_path: str, algorithms: frozenset[str]) -> dict[str, str]:
        self._reads[relative_path] += 1
        script = self.digests.get(relative_path, ["0" * 64])
        digest = script[min(self._reads[relative_path], len(script)) - 1]
        return dict.fromkeys(algorithms, digest)

    async def finalize(self, relative_path: str) -> None:
        self.finalized.append(relative_path)

    def discard_part(self, relative_path: str) -> None:
        self.discarded.append(relative_path)

    def free_bytes(self) -> int:
        return self.free


class FakeChecksums:
    def __init__(self, digests: dict[str, str] | None = None) -> None:
        self.digests = digests or {}

    async def expected(self, ref: ChecksumRef, file_name: str) -> str | None:
        return self.digests.get(file_name)


class MemoryDownloadState:
    """Applies the real state machine to an in-memory status table."""

    def __init__(self, statuses: dict[str, FileStatus] | None = None) -> None:
        self.statuses = statuses or {}
        self.done: dict[str, DownloadedFile] = {}
        self.errors: dict[str, str | None] = {}

    async def transition(
        self, file_id: str, current: FileStatus, target: FileStatus, *, error: str | None = None
    ) -> None:
        check_transition(current, target)
        if self.statuses.get(file_id, FileStatus.LISTED) is not current:
            raise InvalidTransition(f"{file_id} is not in status {current}")
        self.statuses[file_id] = target
        self.errors[file_id] = error

    async def mark_done(self, file_id: str, proof: DownloadedFile) -> None:
        if self.statuses.get(file_id) is not FileStatus.DOWNLOADING:
            raise InvalidTransition(f"{file_id} is not in status downloading")
        self.statuses[file_id] = FileStatus.DONE
        self.done[file_id] = proof


# --- extraction -------------------------------------------------------------------------


class FakeArchiveExtractor:
    """Scripted outcome per path (default: one file of 10 bytes); can block until released."""

    def __init__(
        self,
        outcomes: dict[str, ExtractResult | Exception] | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.gate = gate
        self.calls: list[str] = []
        self.started = asyncio.Event()

    async def extract(self, relative_path: str, name: str) -> ExtractResult:
        self.calls.append(relative_path)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        outcome = self.outcomes.get(relative_path, ExtractResult(1, 10, 0))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class MemoryExtractState:
    """Applies the real extraction state machine to an in-memory table."""

    def __init__(self, statuses: dict[str, ExtractStatus] | None = None) -> None:
        self.statuses = statuses or {}
        self.errors: dict[str, str | None] = {}
        self.results: dict[str, ExtractResult] = {}

    async def extract_transition(
        self,
        file_id: str,
        current: ExtractStatus,
        target: ExtractStatus,
        *,
        error: str | None = None,
    ) -> None:
        check_extract_transition(current, target)
        if self.statuses.get(file_id, ExtractStatus.PENDING) is not current:
            raise InvalidTransition(f"{file_id} is not in extract status {current}")
        self.statuses[file_id] = target
        self.errors[file_id] = error

    async def mark_extracted(self, file_id: str, result: ExtractResult) -> None:
        if self.statuses.get(file_id) is not ExtractStatus.EXTRACTING:
            raise InvalidTransition(f"{file_id} is not in extract status extracting")
        self.statuses[file_id] = ExtractStatus.DONE
        self.results[file_id] = result

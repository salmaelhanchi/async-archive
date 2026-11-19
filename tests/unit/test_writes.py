"""Cancellation that lands while a status write is in flight.

The fakes apply the change and then stall before returning, like a SQLite commit that lands
on aiosqlite's thread after its caller was cancelled. Without the shielding in
``services/writes.py`` these rows were left ``downloading`` / ``extracting`` by a graceful
stop (found by stopping the compose demo with SIGTERM).
"""

import asyncio

import pytest

from archive_mirror.domain.extraction import ExtractResult
from archive_mirror.domain.models import ArchiveJob, PendingFile
from archive_mirror.domain.status import ExtractStatus, FileStatus
from archive_mirror.services.downloader import Downloader, DownloadLimits
from archive_mirror.services.extractor import Extractor
from archive_mirror.services.writes import claim, write_through
from tests.fakes import (
    FakeArchiveExtractor,
    FakeChecksums,
    FakeStorage,
    FakeTransfer,
    MemoryDownloadState,
    MemoryExtractState,
)
from tests.unit.test_downloader import ok, remote


class StallAfterClaim(MemoryDownloadState):
    def __init__(self) -> None:
        super().__init__()
        self.claimed = asyncio.Event()

    async def transition(
        self, file_id: str, current: FileStatus, target: FileStatus, *, error: str | None = None
    ) -> None:
        await super().transition(file_id, current, target, error=error)
        if target is FileStatus.DOWNLOADING:
            self.claimed.set()
            await asyncio.sleep(0.05)  # committed, but the caller has not heard back yet


class StallExtractState(MemoryExtractState):
    def __init__(self, stall_on: ExtractStatus) -> None:
        super().__init__()
        self.stall_on = stall_on
        self.reached = asyncio.Event()

    async def extract_transition(
        self,
        file_id: str,
        current: ExtractStatus,
        target: ExtractStatus,
        *,
        error: str | None = None,
    ) -> None:
        await super().extract_transition(file_id, current, target, error=error)
        if target is self.stall_on:
            self.reached.set()
            await asyncio.sleep(0.05)

    async def mark_extracted(self, file_id: str, result: ExtractResult) -> None:
        await super().mark_extracted(file_id, result)
        if self.stall_on is ExtractStatus.DONE:
            self.reached.set()
            await asyncio.sleep(0.05)


async def cancel_when(task: asyncio.Task[None], event: asyncio.Event) -> None:
    await event.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_download_claim_that_lands_as_the_run_is_cancelled_is_rolled_back() -> None:
    f = remote("a.bin")
    state = StallAfterClaim()
    d = Downloader(
        transfer=FakeTransfer({f.url: [ok()]}),
        storage=FakeStorage(),
        checksums=FakeChecksums(),
        state=state,
        limits=DownloadLimits(workers=1, queue_size=1, max_bytes=None, min_free_bytes=0),
    )
    task = asyncio.create_task(d.run(lambda: d.submit([PendingFile(f, FileStatus.LISTED)])))

    await cancel_when(task, state.claimed)  # type: ignore[arg-type]

    assert state.statuses[f.file_id] is FileStatus.LISTED


@pytest.mark.parametrize(
    ("stall_on", "expected"),
    [
        (ExtractStatus.EXTRACTING, ExtractStatus.PENDING),  # the claim: rolled back
        (ExtractStatus.DONE, ExtractStatus.DONE),  # the result: kept
    ],
)
async def test_an_extraction_write_cut_by_cancellation_never_leaves_extracting(
    stall_on: ExtractStatus, expected: ExtractStatus
) -> None:
    archive = ArchiveJob("id", "host/a.zip", "a.zip", ExtractStatus.PENDING)
    state = StallExtractState(stall_on)
    e = Extractor(extractor=FakeArchiveExtractor(), state=state, workers=1, queue_size=1)
    task = asyncio.create_task(e.run(lambda: e.submit(archive)))

    await cancel_when(task, state.reached)  # type: ignore[arg-type]

    assert state.statuses["id"] is expected


async def test_write_through_returns_the_result_when_not_cancelled() -> None:
    async def write() -> int:
        return 3

    assert await write_through(write()) == 3


async def test_a_claim_that_fails_raises_its_own_error_and_undoes_nothing() -> None:
    undone: list[str] = []

    async def failing() -> None:
        raise ValueError("not claimed")

    async def undo() -> None:
        undone.append("undo")

    with pytest.raises(ValueError, match="not claimed"):
        await claim(failing(), undo)
    assert undone == []

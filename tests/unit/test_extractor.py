"""Extraction service with a fake extractor and an in-memory state machine."""

import asyncio

import pytest

from archive_mirror.domain.errors import ExtractionFailed, UnsupportedArchive
from archive_mirror.domain.extraction import ExtractResult
from archive_mirror.domain.models import ArchiveJob
from archive_mirror.domain.status import ExtractStatus
from archive_mirror.services.extractor import Extractor
from tests.fakes import FakeArchiveExtractor, MemoryExtractState


def job(name: str, status: ExtractStatus = ExtractStatus.PENDING) -> ArchiveJob:
    return ArchiveJob(f"id-{name}", f"host/{name}", name, status)


def extractor(
    fake: FakeArchiveExtractor, state: MemoryExtractState, *, workers: int = 2
) -> Extractor:
    return Extractor(extractor=fake, state=state, workers=workers, queue_size=2)


async def run(e: Extractor, jobs: list[ArchiveJob]) -> None:
    async def feed() -> None:
        for item in jobs:
            await e.submit(item)

    await e.run(feed)


async def test_each_outcome_lands_in_its_status_with_the_reason() -> None:
    ok, bad, rar, disk = job("ok.zip"), job("bad.zip"), job("x.rar"), job("disk.zip")
    fake = FakeArchiveExtractor(
        {
            ok.relative_path: ExtractResult(3, 300, 1),
            bad.relative_path: ExtractionFailed("BadZipFile: not a zip"),
            rar.relative_path: UnsupportedArchive("rar archives are not extracted"),
            disk.relative_path: OSError("No space left on device"),
        }
    )
    state = MemoryExtractState()
    e = extractor(fake, state)

    await run(e, [ok, bad, rar, disk])

    assert state.statuses == {
        ok.file_id: ExtractStatus.DONE,
        bad.file_id: ExtractStatus.FAILED,
        rar.file_id: ExtractStatus.UNSUPPORTED,
        disk.file_id: ExtractStatus.FAILED,
    }
    assert state.results[ok.file_id] == ExtractResult(3, 300, 1)
    assert state.errors[bad.file_id] == "BadZipFile: not a zip"
    assert state.errors[disk.file_id] == "OSError: No space left on device"
    stats = e.stats
    assert (stats.done, stats.failed, stats.unsupported, stats.files, stats.bytes) == (
        1,
        2,
        1,
        3,
        300,
    )


async def test_a_failed_archive_from_an_earlier_run_is_tried_again() -> None:
    retry = job("retry.zip", ExtractStatus.FAILED)
    state = MemoryExtractState({retry.file_id: ExtractStatus.FAILED})

    await run(extractor(FakeArchiveExtractor(), state), [retry])

    assert state.statuses[retry.file_id] is ExtractStatus.DONE


async def test_an_archive_queued_twice_is_unpacked_once() -> None:
    twice = job("twice.zip")
    fake, state = FakeArchiveExtractor(), MemoryExtractState()

    await run(extractor(fake, state, workers=1), [twice, twice])

    assert fake.calls == [twice.relative_path]
    assert state.statuses[twice.file_id] is ExtractStatus.DONE


async def test_a_row_reset_by_a_new_upstream_copy_drops_the_stale_result() -> None:
    stale = job("stale.zip")
    gate = asyncio.Event()
    fake, state = FakeArchiveExtractor(gate=gate), MemoryExtractState()
    e = extractor(fake, state)

    async def feed() -> None:
        await e.submit(stale)
        await fake.started.wait()
        del state.statuses[stale.file_id]  # the listing writer cleared it: upstream changed
        state.statuses[stale.file_id] = ExtractStatus.PENDING  # and the new copy is done
        gate.set()

    await e.run(feed)

    assert state.statuses[stale.file_id] is ExtractStatus.PENDING
    assert e.stats.done == 0


async def test_cancellation_rolls_the_archive_back_to_pending() -> None:
    held = job("held.zip")
    fake, state = FakeArchiveExtractor(gate=asyncio.Event()), MemoryExtractState()
    e = extractor(fake, state)
    task = asyncio.create_task(run(e, [held]))
    await fake.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert state.statuses[held.file_id] is ExtractStatus.PENDING
    assert state.errors[held.file_id] == "interrupted, will extract again"

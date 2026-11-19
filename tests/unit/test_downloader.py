"""Download service with fake transfer, storage, checksums and state."""

import asyncio
from collections.abc import Sequence

import pytest

from archive_mirror.domain.errors import FetchFailed
from archive_mirror.domain.models import (
    ChecksumRef,
    EntryKind,
    Listing,
    ListingEntry,
    ListingFormat,
    PendingFile,
    RemoteFile,
    TransferResult,
)
from archive_mirror.domain.status import FileStatus
from archive_mirror.services.downloader import Downloader, DownloadLimits
from tests.fakes import FakeChecksums, FakeStorage, FakeTransfer, MemoryDownloadState

DIR = "https://archive.example/pub/"
LISTING = Listing(DIR, ListingFormat.NGINX, ())
GOOD, BAD = "1" * 64, "2" * 64


def remote(
    name: str, size: int | None = 100, *, exact: bool = True, sums: bool = False
) -> RemoteFile:
    entry = ListingEntry(DIR + name, name, EntryKind.FILE, size, exact, None)
    checksum = ChecksumRef(DIR + "SHA256SUMS", "sha256", shared=True) if sums else None
    return RemoteFile.from_entry(entry, LISTING, checksum)


def ok(size: int = 100, resumed_from: int = 0) -> TransferResult:
    return TransferResult(size, size, resumed_from, size - resumed_from, '"e"')


def downloader(
    transfer: FakeTransfer,
    *,
    storage: FakeStorage | None = None,
    checksums: FakeChecksums | None = None,
    state: MemoryDownloadState | None = None,
    workers: int = 2,
    queue_size: int = 4,
    max_bytes: int | None = None,
    min_free: int = 0,
) -> Downloader:
    return Downloader(
        transfer=transfer,
        storage=storage or FakeStorage(),
        checksums=checksums or FakeChecksums(),
        state=state or MemoryDownloadState(),
        limits=DownloadLimits(
            workers=workers, queue_size=queue_size, max_bytes=max_bytes, min_free_bytes=min_free
        ),
    )


async def run(d: Downloader, files: Sequence[RemoteFile]) -> None:
    await d.run(lambda: d.submit([PendingFile(f, FileStatus.LISTED) for f in files]))


async def test_a_verified_file_is_finalized_then_marked_done_with_proof() -> None:
    f = remote("a.bin", sums=True)
    storage, state = FakeStorage({f.relative_path: [GOOD]}), MemoryDownloadState()
    d = downloader(
        FakeTransfer({f.url: [ok()]}),
        storage=storage,
        checksums=FakeChecksums({"a.bin": GOOD}),
        state=state,
    )

    await run(d, [f])

    assert storage.finalized == [f.relative_path]
    assert state.statuses[f.file_id] is FileStatus.DONE
    proof = state.done[f.file_id]
    assert (proof.sha256, proof.size, proof.verified_by) == (GOOD, 100, "checksum")
    assert (d.stats.done, d.stats.verified_by_checksum, d.stats.bytes_received) == (1, 1, 100)


@pytest.mark.parametrize(
    ("size", "exact", "total", "verified_by"),
    [(100, True, 100, "size"), (None, False, 100, "size"), (None, False, None, "none")],
)
async def test_verified_by_records_what_was_actually_checked(
    size: int | None, exact: bool, total: int | None, verified_by: str
) -> None:
    f = remote("a.bin", size, exact=exact)
    state = MemoryDownloadState()
    result = TransferResult(100, total, 0, 100, None)

    await run(downloader(FakeTransfer({f.url: [result]}), state=state), [f])

    assert state.done[f.file_id].verified_by == verified_by


async def test_a_fresh_download_with_the_wrong_checksum_fails_without_retrying() -> None:
    f = remote("a.bin", sums=True)
    transfer, storage = FakeTransfer({f.url: [ok()]}), FakeStorage({f.relative_path: [BAD]})
    state = MemoryDownloadState()
    d = downloader(transfer, storage=storage, checksums=FakeChecksums({"a.bin": GOOD}), state=state)

    await run(d, [f])

    assert transfer.calls[f.url] == 1
    assert storage.discarded == [f.relative_path]
    assert state.statuses[f.file_id] is FileStatus.FAILED
    assert "is not the published" in (state.errors[f.file_id] or "")


async def test_a_resumed_copy_that_fails_verification_is_downloaded_once_more_from_zero() -> None:
    f = remote("a.bin", sums=True)
    transfer = FakeTransfer({f.url: [ok(resumed_from=40), ok()]})
    storage = FakeStorage({f.relative_path: [BAD, GOOD]})
    state = MemoryDownloadState()
    d = downloader(transfer, storage=storage, checksums=FakeChecksums({"a.bin": GOOD}), state=state)

    await run(d, [f])

    assert transfer.calls[f.url] == 2
    assert storage.discarded == [f.relative_path]
    assert state.statuses[f.file_id] is FileStatus.DONE


async def test_a_size_that_differs_from_the_listing_fails_the_file() -> None:
    f = remote("a.bin", 100)
    state = MemoryDownloadState()

    await run(downloader(FakeTransfer({f.url: [ok(size=90)]}), state=state), [f])

    assert state.statuses[f.file_id] is FileStatus.FAILED
    assert "does not match the listing" in (state.errors[f.file_id] or "")


async def test_fetch_and_local_errors_fail_one_file_and_the_rest_continue() -> None:
    gone, local, fine = remote("gone"), remote("local"), remote("fine")
    transfer = FakeTransfer(
        {
            gone.url: [FetchFailed(gone.url, "HTTP 404", 1)],
            local.url: [OSError("name too long")],
            fine.url: [ok()],
        }
    )
    state = MemoryDownloadState()

    await run(downloader(transfer, state=state), [gone, local, fine])

    assert state.errors[gone.file_id] == "HTTP 404"
    assert state.errors[local.file_id] == "OSError: name too long"
    assert state.statuses[fine.file_id] is FileStatus.DONE
    assert (state.statuses[gone.file_id], state.statuses[local.file_id]) == (
        FileStatus.FAILED,
        FileStatus.FAILED,
    )


async def test_the_byte_budget_is_checked_before_each_request() -> None:
    files = [remote(f"f{i}", 100) for i in range(5)]
    transfer = FakeTransfer({f.url: [ok()] for f in files})
    state = MemoryDownloadState()
    d = downloader(transfer, state=state, max_bytes=250, workers=1)

    await run(d, files)

    assert sum(transfer.calls.values()) == 2
    assert (d.stats.done, d.stats.skipped) == (2, 3)
    assert sum(s is FileStatus.DONE for s in state.statuses.values()) == 2  # rest stay listed


async def test_downloads_stop_before_the_disk_would_fill() -> None:
    small, big = remote("small", 100), remote("big", 10_000)
    transfer = FakeTransfer({small.url: [ok()], big.url: [ok(10_000)]})
    d = downloader(transfer, storage=FakeStorage(free=5_000), min_free=1_000)

    await run(d, [small, big])

    assert (d.stats.done, d.stats.skipped) == (1, 1)
    assert big.url not in transfer.calls


async def test_a_file_whose_status_moved_on_is_not_downloaded() -> None:
    f = remote("a.bin")
    transfer = FakeTransfer({f.url: [ok()]})
    state = MemoryDownloadState({f.file_id: FileStatus.DONE})

    await run(downloader(transfer, state=state), [f])

    assert transfer.calls[f.url] == 0


async def test_submit_blocks_while_the_queue_is_full() -> None:
    files = [remote(f"f{i}") for i in range(10)]
    transfer = FakeTransfer({f.url: [ok()] for f in files}, delay=0.02)
    d = downloader(transfer, workers=2, queue_size=1)
    submitted: list[int] = []

    async def feed() -> None:
        for index, f in enumerate(files):
            await d.submit([PendingFile(f, FileStatus.LISTED)])
            submitted.append(index)
            # never more than workers + queue + 1 ahead of finished downloads
            assert index - d.stats.done <= 2 + 1 + 1

    await d.run(feed)

    assert d.stats.done == 10
    assert transfer.peak_active == 2


async def test_a_bug_in_the_feed_cancels_the_workers() -> None:
    d = downloader(FakeTransfer({}))

    async def feed() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("listing writer crashed")

    with pytest.raises(ExceptionGroup) as caught:
        await d.run(feed)
    assert caught.group_contains(RuntimeError, match="listing writer crashed")

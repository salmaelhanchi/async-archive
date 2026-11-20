"""What the services need from the outside world. Adapters implement these; tests fake them."""

from collections.abc import Sequence
from typing import Protocol

from archive_mirror.domain.extraction import ExtractResult
from archive_mirror.domain.models import (
    ChecksumRef,
    DownloadedFile,
    FetchedPage,
    Listing,
    ListingOutcome,
    PendingFile,
    RemoteFile,
    TransferResult,
)
from archive_mirror.domain.status import ExtractStatus, FileStatus


class PageFetcher(Protocol):
    async def fetch(self, url: str) -> FetchedPage: ...


class ListingParserFn(Protocol):
    async def __call__(self, html: str, listing_url: str) -> Listing:
        """Async so a large page can be parsed in another process (ADR 006)."""
        ...


class FetchPolicy(Protocol):
    async def allowed(self, url: str) -> bool: ...


class CrawlStateWriter(Protocol):
    async def record_listing(
        self, outcome: ListingOutcome, files: Sequence[RemoteFile]
    ) -> Sequence[PendingFile]:
        """Persist one directory and its files in one transaction; return those not done."""
        ...


class FileTransfer(Protocol):
    async def download(self, url: str, relative_path: str) -> TransferResult:
        """Bring the part file for ``url`` up to the full length, resuming what is on disk."""
        ...


class PartStorage(Protocol):
    async def digest_part(
        self, relative_path: str, algorithms: frozenset[str]
    ) -> dict[str, str]: ...

    async def finalize(self, relative_path: str) -> None: ...

    def discard_part(self, relative_path: str) -> None: ...

    def free_bytes(self) -> int: ...


class ChecksumSource(Protocol):
    async def expected(self, ref: ChecksumRef, file_name: str) -> str | None: ...


class DownloadStateWriter(Protocol):
    async def transition(
        self, file_id: str, current: FileStatus, target: FileStatus, *, error: str | None = None
    ) -> None: ...

    async def mark_done(self, file_id: str, proof: DownloadedFile) -> None: ...


class ArchiveExtractor(Protocol):
    async def extract(self, relative_path: str, name: str) -> ExtractResult:
        """Unpack the mirrored archive at ``relative_path``. Raises ``UnsupportedArchive``
        or ``ExtractionFailed`` for archives that cannot be unpacked."""
        ...


class ExtractStateWriter(Protocol):
    async def extract_transition(
        self,
        file_id: str,
        current: ExtractStatus,
        target: ExtractStatus,
        *,
        error: str | None = None,
    ) -> None: ...

    async def mark_extracted(self, file_id: str, result: ExtractResult) -> None: ...

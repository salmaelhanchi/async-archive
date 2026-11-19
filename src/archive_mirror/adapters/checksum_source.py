"""Fetch published checksum files and look up one file's expected digest."""

import asyncio

import structlog

from archive_mirror.domain.checksums import find_digest
from archive_mirror.domain.errors import FetchFailed
from archive_mirror.domain.models import ChecksumRef
from archive_mirror.services.ports import PageFetcher


class HttpChecksumSource:
    """A ``SHA256SUMS``-style file covers a whole directory, so it is fetched once per run and
    shared (one lock per URL, like robots.txt). Per-file sidecars are fetched each time and
    not kept, so memory does not grow with the archive."""

    def __init__(self, fetcher: PageFetcher) -> None:
        self._fetcher = fetcher
        self._shared: dict[str, str | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._log = structlog.get_logger(__name__)

    async def expected(self, ref: ChecksumRef, file_name: str) -> str | None:
        text = await self._text(ref)
        if text is None:
            return None
        digest = find_digest(text, file_name, ref.algorithm)
        if digest is None:
            self._log.warning("checksum_entry_missing", checksum_url=ref.url, name=file_name)
        return digest

    async def _text(self, ref: ChecksumRef) -> str | None:
        if not ref.shared:
            return await self._load(ref.url)
        if ref.url not in self._shared:
            async with self._locks.setdefault(ref.url, asyncio.Lock()):
                if ref.url not in self._shared:
                    self._shared[ref.url] = await self._load(ref.url)
        return self._shared[ref.url]

    async def _load(self, url: str) -> str | None:
        try:
            page = await self._fetcher.fetch(url)
        except FetchFailed as exc:
            self._log.warning("checksum_unreachable", checksum_url=url, reason=exc.reason)
            return None
        if page.status != 200:
            self._log.warning("checksum_unreachable", checksum_url=url, reason=page.status)
            return None
        return page.text

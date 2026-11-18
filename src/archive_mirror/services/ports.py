"""What the services need from the outside world. Adapters implement these; tests fake them."""

from collections.abc import Sequence
from typing import Protocol

from archive_mirror.domain.models import FetchedPage, Listing, ListingOutcome, RemoteFile


class PageFetcher(Protocol):
    async def fetch(self, url: str) -> FetchedPage: ...


class ListingParserFn(Protocol):
    def __call__(self, html: str, listing_url: str) -> Listing: ...


class FetchPolicy(Protocol):
    async def allowed(self, url: str) -> bool: ...


class CrawlStateWriter(Protocol):
    async def record_listing(self, outcome: ListingOutcome, files: Sequence[RemoteFile]) -> None:
        """Persist one directory and its files in a single transaction."""
        ...

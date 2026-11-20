"""Parse a listing page on the event loop when it is small, in a process pool when it is not.

Measured (``bench/parse_cost.py``, ADR 006): parsing costs about 1.5 ms per KiB of nginx or
IIS HTML and 2.6 ms per KiB of Apache HTML. A 25-entry page takes 4 to 14 ms; a
1,000-entry page 150 to 625 ms, and while it runs on the loop every download and every
SQLite write waits. A pool round trip adds about 2 ms for pickling, which is not worth it for a
small page, so the size of the page decides.
"""

import asyncio
from concurrent.futures import Executor
from concurrent.futures.process import BrokenProcessPool

from archive_mirror.adapters.listing import parse_listing
from archive_mirror.domain.errors import UnrecognizedListing
from archive_mirror.domain.models import Listing, ListingFormat


class PooledListingParser:
    def __init__(
        self, pool: Executor, *, forced: ListingFormat | None, min_pool_bytes: int
    ) -> None:
        self._pool = pool
        self._forced = forced
        self._min_pool_bytes = min_pool_bytes
        self.pages_in_pool = 0

    async def __call__(self, html: str, listing_url: str) -> Listing:
        if len(html) < self._min_pool_bytes:
            return parse_listing(html, listing_url, self._forced)
        loop = asyncio.get_running_loop()
        self.pages_in_pool += 1
        try:
            return await loop.run_in_executor(
                self._pool, parse_listing, html, listing_url, self._forced
            )
        except BrokenProcessPool:
            raise UnrecognizedListing("the parser process died on this page") from None

"""Test doubles for the service ports and for time."""

import asyncio
from collections import Counter
from collections.abc import Sequence

from archive_mirror.domain.errors import FetchFailed
from archive_mirror.domain.models import FetchedPage, ListingOutcome, RemoteFile


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

    async def record_listing(self, outcome: ListingOutcome, files: Sequence[RemoteFile]) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if outcome.url == self.fail_on:
            raise RuntimeError("disk full")
        self.outcomes.append(outcome)
        self.files.extend(files)

    def by_url(self) -> dict[str, ListingOutcome]:
        return {o.url: o for o in self.outcomes}

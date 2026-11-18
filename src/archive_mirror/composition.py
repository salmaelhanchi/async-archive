"""Wires adapters into services. The only place that knows every concrete class."""

import asyncio
from collections.abc import Sequence

import httpx

from archive_mirror import __version__
from archive_mirror.adapters.http_fetcher import HttpFetcher, RetryPolicy
from archive_mirror.adapters.listing import parse_listing
from archive_mirror.adapters.politeness import HostLimiter
from archive_mirror.adapters.robots import RobotsPolicy
from archive_mirror.adapters.state_store import SqliteStateStore
from archive_mirror.domain.models import Listing, RunSummary
from archive_mirror.domain.status import RunOutcome
from archive_mirror.services.lister import CrawlLimits, Lister
from archive_mirror.settings import PRODUCT_NAME, Settings


def build_http_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=settings.http2,
        headers={"User-Agent": settings.user_agent},
        timeout=httpx.Timeout(settings.request_timeout_s),
        follow_redirects=True,
        limits=httpx.Limits(
            max_connections=settings.max_in_flight,
            max_keepalive_connections=settings.max_in_flight,
        ),
    )


async def run_dry_run(settings: Settings, start_urls: Sequence[str], run_id: str) -> RunSummary:
    """List everything under ``start_urls``, record it, and return the totals. No downloads."""
    store = await SqliteStateStore.open(settings.state_db, run_id)
    try:
        await store.start_run(
            mode="dry_run",
            start_urls=start_urls,
            config=settings.snapshot(),
            tool_version=__version__,
        )
        outcome = RunOutcome.FAILED
        try:
            async with build_http_client(settings) as client:
                limiter = HostLimiter(
                    rate=settings.per_host_rate,
                    burst=settings.per_host_burst,
                    max_in_flight=settings.max_in_flight,
                )
                retry = RetryPolicy(
                    max_attempts=settings.max_attempts,
                    backoff_base=settings.backoff_base_s,
                    backoff_cap=settings.backoff_cap_s,
                    max_retry_after=settings.max_retry_after_s,
                )
                fetcher = HttpFetcher(
                    client, limiter, retry, max_body_bytes=settings.max_listing_bytes
                )
                policy = RobotsPolicy(
                    fetcher, limiter, PRODUCT_NAME, enabled=settings.respect_robots
                )
                forced = settings.forced_format

                def parse(html: str, listing_url: str) -> Listing:
                    return parse_listing(html, listing_url, forced)

                lister = Lister(
                    fetcher=fetcher,
                    parse=parse,
                    policy=policy,
                    writer=store,
                    limits=CrawlLimits(
                        workers=settings.listing_workers,
                        max_depth=settings.max_depth,
                        max_listing_pages=settings.max_listing_pages,
                        write_queue_size=settings.write_queue_size,
                    ),
                )
                await lister.crawl(start_urls)
            outcome = RunOutcome.COMPLETED
        except asyncio.CancelledError:
            outcome = RunOutcome.INTERRUPTED
            raise
        finally:
            await store.finish_run(outcome)
        return await store.run_summary()
    finally:
        await store.close()

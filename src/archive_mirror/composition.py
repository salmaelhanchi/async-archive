"""Wires adapters into services. The only place that knows every concrete class."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
import structlog

from archive_mirror import __version__
from archive_mirror.adapters.checksum_source import HttpChecksumSource
from archive_mirror.adapters.http_download import HttpFileTransfer
from archive_mirror.adapters.http_fetcher import HttpFetcher, RetryPolicy
from archive_mirror.adapters.listing import parse_listing
from archive_mirror.adapters.politeness import HostLimiter
from archive_mirror.adapters.robots import RobotsPolicy
from archive_mirror.adapters.state_lock import exclusive_state
from archive_mirror.adapters.state_store import SqliteStateStore
from archive_mirror.adapters.storage import MirrorStorage
from archive_mirror.domain.models import Listing, MirrorSummary, RunSummary
from archive_mirror.domain.status import RunOutcome
from archive_mirror.services.downloader import Downloader, DownloadLimits
from archive_mirror.services.lister import CrawlLimits, Lister, OnRecorded
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


@asynccontextmanager
async def _state(
    settings: Settings, run_id: str, mode: str, start_urls: Sequence[str]
) -> AsyncIterator[SqliteStateStore]:
    """Lock the state file, open it, record the run, recover from any earlier kill."""
    with exclusive_state(settings.state_db):
        store = await SqliteStateStore.open(settings.state_db, run_id)
        try:
            await store.start_run(
                mode=mode,
                start_urls=start_urls,
                config=settings.snapshot(),
                tool_version=__version__,
            )
            runs, files = await store.recover_interrupted()
            if runs or files:
                structlog.get_logger(__name__).warning(
                    "recovered_after_interruption", runs=runs, files_to_resume=files
                )
            outcome = RunOutcome.FAILED
            try:
                yield store
                outcome = RunOutcome.COMPLETED
            except asyncio.CancelledError:
                outcome = RunOutcome.INTERRUPTED
                raise
            finally:
                await store.finish_run(outcome)
        finally:
            await store.close()


@asynccontextmanager
async def _http(settings: Settings) -> AsyncIterator[tuple[HttpFetcher, RobotsPolicy]]:
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
        fetcher = HttpFetcher(client, limiter, retry, max_body_bytes=settings.max_listing_bytes)
        policy = RobotsPolicy(fetcher, limiter, PRODUCT_NAME, enabled=settings.respect_robots)
        yield fetcher, policy


def _lister(
    settings: Settings,
    fetcher: HttpFetcher,
    policy: RobotsPolicy,
    store: SqliteStateStore,
    on_recorded: OnRecorded | None = None,
) -> Lister:
    forced = settings.forced_format

    def parse(html: str, listing_url: str) -> Listing:
        return parse_listing(html, listing_url, forced)

    return Lister(
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
        on_recorded=on_recorded,
    )


async def run_dry_run(settings: Settings, start_urls: Sequence[str], run_id: str) -> RunSummary:
    """List everything under ``start_urls``, record it, and return the totals. No downloads."""
    async with _state(settings, run_id, "dry_run", start_urls) as store:
        async with _http(settings) as (fetcher, policy):
            await _lister(settings, fetcher, policy, store).crawl(start_urls)
        return await store.run_summary()


async def run_mirror(settings: Settings, start_urls: Sequence[str], run_id: str) -> MirrorSummary:
    """Mirror ``start_urls``. With no URLs, download what the state file still has pending
    (``resume``) without listing anything again."""
    async with _state(settings, run_id, "mirror", start_urls) as store:
        async with _http(settings) as (fetcher, policy):
            storage = MirrorStorage(settings.mirror_root)
            downloader = Downloader(
                transfer=HttpFileTransfer(fetcher, storage),
                storage=storage,
                checksums=HttpChecksumSource(fetcher),
                state=store,
                limits=DownloadLimits(
                    workers=settings.download_workers,
                    queue_size=settings.download_queue_size,
                    max_bytes=settings.max_download_bytes,
                    min_free_bytes=settings.min_free_bytes,
                ),
            )
            if start_urls:
                lister = _lister(settings, fetcher, policy, store, on_recorded=downloader.submit)
                stats = await downloader.run(lambda: lister.crawl(start_urls))
            else:
                stats = await downloader.run(lambda: downloader.submit_all(store.pending_files()))
        crawl = await store.run_summary() if start_urls else None
        by_status, bytes_done = await store.file_totals()
        return MirrorSummary(
            run_id=run_id,
            crawl=crawl,
            downloads=stats.report(),
            files_by_status=by_status,
            bytes_done=bytes_done,
        )

"""Retries, Retry-After and host pauses, with respx standing in for the network."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
import respx

from archive_mirror.adapters.http_fetcher import HttpFetcher, RetryPolicy
from archive_mirror.adapters.politeness import HostLimiter
from archive_mirror.domain.errors import FetchFailed
from tests.fakes import FakeClock

URL = "https://archive.example/pub/"
HTML = {"content-type": "text/html; charset=utf-8"}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def fetcher(clock: FakeClock) -> AsyncIterator[HttpFetcher]:
    limiter = HostLimiter(rate=100, burst=10, max_in_flight=4, clock=clock, sleep=clock.sleep)
    retry = RetryPolicy(max_attempts=3, backoff_base=1.0, backoff_cap=8.0, max_retry_after=60)
    async with httpx.AsyncClient() as client:
        yield HttpFetcher(
            client,
            limiter,
            retry,
            max_body_bytes=1000,
            sleep=clock.sleep,
            jitter=lambda: 0.5,
            now=lambda: datetime(2024, 1, 1, tzinfo=UTC),
        )


async def test_success_on_first_try(fetcher: HttpFetcher, respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).respond(200, text="<pre></pre>", headers=HTML)

    page = await fetcher.fetch(URL)

    assert (page.status, page.text, page.attempts) == (200, "<pre></pre>", 1)
    assert page.content_type.startswith("text/html")


async def test_503_with_retry_after_pauses_the_host(
    fetcher: HttpFetcher, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    respx_mock.get(URL).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "7"}),
            httpx.Response(200, text="ok", headers=HTML),
        ]
    )

    page = await fetcher.fetch(URL)

    assert page.attempts == 2
    assert clock.now >= 7.0  # waited out Retry-After through the host's bucket


async def test_server_errors_back_off_exponentially(
    fetcher: HttpFetcher, respx_mock: respx.MockRouter, clock: FakeClock
) -> None:
    respx_mock.get(URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(502), httpx.Response(200, text="ok")]
    )

    page = await fetcher.fetch(URL)

    assert page.attempts == 3
    assert clock.sleeps == [0.5, 1.0]  # jitter 0.5 x (1, 2)


async def test_connection_errors_are_retried(
    fetcher: HttpFetcher, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(URL).mock(side_effect=[httpx.ConnectError("refused"), httpx.Response(200)])

    assert (await fetcher.fetch(URL)).attempts == 2


async def test_gives_up_after_max_attempts(
    fetcher: HttpFetcher, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(URL).respond(503)

    with pytest.raises(FetchFailed) as info:
        await fetcher.fetch(URL)

    assert route.call_count == 3
    assert (info.value.reason, info.value.attempts) == ("HTTP 503", 3)


async def test_retry_after_beyond_the_limit_fails_at_once(
    fetcher: HttpFetcher, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(URL).respond(429, headers={"Retry-After": "3600"})

    with pytest.raises(FetchFailed, match="over limit"):
        await fetcher.fetch(URL)

    assert route.call_count == 1


async def test_client_errors_are_returned_not_retried(
    fetcher: HttpFetcher, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(URL).respond(404)

    page = await fetcher.fetch(URL)

    assert (page.status, route.call_count) == (404, 1)


async def test_body_over_the_limit_is_refused(
    fetcher: HttpFetcher, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(URL).respond(200, content=b"x" * 5000)

    with pytest.raises(FetchFailed, match="body over 1000 bytes"):
        await fetcher.fetch(URL)

"""robots.txt policy, following RFC 9309 for missing and failing robots files."""

import asyncio

from archive_mirror.adapters.politeness import HostLimiter
from archive_mirror.adapters.robots import RobotsPolicy
from archive_mirror.domain.errors import FetchFailed
from tests.fakes import FakeFetcher, html_page

ROBOTS = "https://archive.example/robots.txt"
TOKEN = "async-archive-mirror"


def policy_for(fetcher: FakeFetcher, *, enabled: bool = True) -> tuple[RobotsPolicy, HostLimiter]:
    limiter = HostLimiter(rate=10, burst=1, max_in_flight=4)
    return RobotsPolicy(fetcher, limiter, TOKEN, enabled=enabled), limiter


async def test_rules_are_applied_and_fetched_once_per_origin() -> None:
    rules = "User-agent: *\nDisallow: /private/\n"
    fetcher = FakeFetcher({ROBOTS: html_page(ROBOTS, rules)})
    policy, _ = policy_for(fetcher)

    results = await asyncio.gather(
        *(policy.allowed(f"https://archive.example/pub/{i}") for i in range(10)),
        policy.allowed("https://archive.example/private/x"),
    )

    assert results == [True] * 10 + [False]
    assert fetcher.calls[ROBOTS] == 1


async def test_rules_for_our_token_win_over_the_wildcard() -> None:
    rules = f"User-agent: *\nDisallow: /\n\nUser-agent: {TOKEN}\nAllow: /\n"
    policy, _ = policy_for(FakeFetcher({ROBOTS: html_page(ROBOTS, rules)}))

    assert await policy.allowed("https://archive.example/pub/")


async def test_crawl_delay_slows_the_host_down() -> None:
    rules = "User-agent: *\nCrawl-delay: 5\n"
    policy, limiter = policy_for(FakeFetcher({ROBOTS: html_page(ROBOTS, rules)}))

    await policy.allowed("https://archive.example/pub/")

    assert limiter.bucket("archive.example").interval == 5.0


async def test_missing_robots_allows_everything() -> None:
    policy, _ = policy_for(FakeFetcher({}))  # FakeFetcher answers 404

    assert await policy.allowed("https://archive.example/anything")


async def test_server_error_disallows_the_origin() -> None:
    fetcher = FakeFetcher({ROBOTS: html_page(ROBOTS, "oops", status=500)})
    policy, _ = policy_for(fetcher)

    assert not await policy.allowed("https://archive.example/pub/")


async def test_unreachable_robots_disallows_the_origin() -> None:
    fetcher = FakeFetcher({ROBOTS: FetchFailed(ROBOTS, "ConnectError", 5)})
    policy, _ = policy_for(fetcher)

    assert not await policy.allowed("https://archive.example/pub/")


async def test_disabled_policy_never_fetches() -> None:
    fetcher = FakeFetcher({})
    policy, _ = policy_for(fetcher, enabled=False)

    assert await policy.allowed("https://archive.example/pub/")
    assert not fetcher.calls

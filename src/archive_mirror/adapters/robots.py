"""robots.txt, fetched once per origin and shared by every worker."""

import asyncio
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import structlog

from archive_mirror.adapters.politeness import HostLimiter
from archive_mirror.domain.errors import FetchFailed
from archive_mirror.services.ports import PageFetcher


class RobotsPolicy:
    """Answers "may we fetch this URL?" following RFC 9309.

    * 2xx: parse the rules. A ``Crawl-delay`` slows the host's token bucket down.
    * 4xx: no rules, everything allowed.
    * 5xx or unreachable: treat the whole origin as disallowed for this run.
    """

    def __init__(
        self, fetcher: PageFetcher, limiter: HostLimiter, robots_token: str, *, enabled: bool
    ) -> None:
        self._fetcher = fetcher
        self._limiter = limiter
        self._token = robots_token
        self._enabled = enabled
        self._rules: dict[str, RobotFileParser] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._log = structlog.get_logger(__name__)

    async def allowed(self, url: str) -> bool:
        if not self._enabled:
            return True
        parts = urlsplit(url)
        rules = await self._rules_for(parts.scheme, parts.netloc)
        return rules.can_fetch(self._token, url)

    async def _rules_for(self, scheme: str, netloc: str) -> RobotFileParser:
        origin = f"{scheme}://{netloc}"
        if origin in self._rules:
            return self._rules[origin]
        async with self._locks.setdefault(origin, asyncio.Lock()):
            if origin not in self._rules:
                self._rules[origin] = await self._load(origin, netloc)
        return self._rules[origin]

    async def _load(self, origin: str, netloc: str) -> RobotFileParser:
        try:
            page = await self._fetcher.fetch(origin + "/robots.txt")
        except FetchFailed as exc:
            self._log.warning("robots_unreachable", origin=origin, reason=exc.reason)
            return _rules(_DISALLOW_ALL)
        if 200 <= page.status < 300:
            rules = _rules(page.text.splitlines())
            delay = rules.crawl_delay(self._token)
            if delay:
                self._limiter.slow_down(netloc, float(delay))
                self._log.info("robots_crawl_delay", origin=origin, seconds=float(delay))
            return rules
        if 400 <= page.status < 500:
            return _rules([])
        self._log.warning("robots_server_error", origin=origin, status=page.status)
        return _rules(_DISALLOW_ALL)


_DISALLOW_ALL = ["User-agent: *", "Disallow: /"]


def _rules(lines: list[str]) -> RobotFileParser:
    rules = RobotFileParser()
    rules.parse(lines)
    rules.modified()  # can_fetch refuses everything until the rules are marked as read
    return rules

"""HTTP GET with politeness and retries. Every request in the program goes through here."""

import asyncio
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
import structlog

from archive_mirror import metrics
from archive_mirror.adapters.politeness import HostLimiter, Sleep
from archive_mirror.domain.backoff import backoff_delay, parse_retry_after
from archive_mirror.domain.errors import FetchFailed
from archive_mirror.domain.models import FetchedPage
from archive_mirror.domain.urls import host_of

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
PAUSE_HOST_STATUSES = frozenset({429, 503})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    backoff_base: float
    backoff_cap: float
    max_retry_after: float


class _BodyTooLarge(Exception):
    pass


class HttpFetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        limiter: HostLimiter,
        retry: RetryPolicy,
        *,
        max_body_bytes: int,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._limiter = limiter
        self._retry = retry
        self._max_body = max_body_bytes
        self._sleep, self._jitter, self._now = sleep, jitter, now
        self._log = structlog.get_logger(__name__)

    async def fetch(self, url: str) -> FetchedPage:
        """GET ``url``. Returns any final status; raises ``FetchFailed`` when retries run out.

        Retried: connection errors, timeouts, 429 and 5xx gateway errors. A 429 or 503 pauses
        the whole host, honouring ``Retry-After`` up to ``max_retry_after`` seconds.
        """
        host = host_of(url)
        policy = self._retry
        reason = "no attempt made"
        for attempt in range(policy.max_attempts):
            delay = backoff_delay(
                attempt, base=policy.backoff_base, cap=policy.backoff_cap, jitter=self._jitter()
            )
            host_paused = False
            try:
                async with self._limiter.slot(host) as waited:
                    metrics.RATE_LIMIT_WAIT.observe(waited)
                    response, body = await self._get(url)
            except httpx.TransportError as exc:
                metrics.HTTP_REQUESTS.labels(host=host, outcome="error").inc()
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            except _BodyTooLarge:
                raise FetchFailed(url, f"body over {self._max_body} bytes", attempt + 1) from None
            else:
                metrics.HTTP_REQUESTS.labels(host=host, outcome=str(response.status_code)).inc()
                if response.status_code not in RETRY_STATUSES:
                    return FetchedPage(
                        url=str(response.url),
                        status=response.status_code,
                        text=body.decode(response.charset_encoding or "utf-8", errors="replace"),
                        content_type=response.headers.get("content-type", ""),
                        attempts=attempt + 1,
                    )
                reason = f"HTTP {response.status_code}"
                retry_after = parse_retry_after(response.headers.get("retry-after"), self._now())
                if retry_after is not None:
                    if retry_after > policy.max_retry_after:
                        raise FetchFailed(
                            url, f"{reason}, Retry-After {retry_after:.0f}s over limit", attempt + 1
                        )
                    delay = retry_after
                if response.status_code in PAUSE_HOST_STATUSES:
                    self._limiter.pause(host, delay)
                    host_paused = True
            if attempt + 1 == policy.max_attempts:
                break
            metrics.HTTP_RETRIES.labels(host=host).inc()
            self._log.warning(
                "http_retry", url=url, reason=reason, attempt=attempt + 1, delay=delay
            )
            if not host_paused:  # a paused host already makes the next slot wait
                await self._sleep(delay)
        raise FetchFailed(url, reason, policy.max_attempts)

    async def _get(self, url: str) -> tuple[httpx.Response, bytes]:
        async with self._client.stream("GET", url) as response:
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self._max_body:
                    raise _BodyTooLarge
                chunks.append(chunk)
            return response, b"".join(chunks)

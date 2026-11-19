"""HTTP GET with politeness and retries. Every request in the program goes through here."""

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
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

# Called with an open response whose status is final (not retried), and the attempt number.
type ResponseHandler[T] = Callable[[httpx.Response, int], Awaitable[T]]
type HeaderFactory = Callable[[], Mapping[str, str]]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int
    backoff_base: float
    backoff_cap: float
    max_retry_after: float


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
        """GET ``url`` into memory, capped at ``max_body_bytes``. Returns any final status."""

        async def read(response: httpx.Response, attempt: int) -> FetchedPage:
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self._max_body:
                    raise FetchFailed(url, f"body over {self._max_body} bytes", attempt)
                chunks.append(chunk)
            body = b"".join(chunks)
            return FetchedPage(
                url=str(response.url),
                status=response.status_code,
                text=body.decode(response.charset_encoding or "utf-8", errors="replace"),
                content_type=response.headers.get("content-type", ""),
                attempts=attempt,
            )

        return await self.request(url, read)

    async def request[T](
        self, url: str, handle: ResponseHandler[T], *, headers: HeaderFactory | None = None
    ) -> T:
        """Stream a GET of ``url`` and pass the response to ``handle``, with retries.

        Retried: connection errors, timeouts, a body cut off mid-stream (any
        ``httpx.TransportError``, also when raised inside ``handle``), 429 and 5xx gateway
        errors. A 429 or 503 pauses the whole host, honouring ``Retry-After`` up to
        ``max_retry_after`` seconds. ``headers`` is called once per attempt, so a resumed
        download can ask for a new range each time. Raises ``FetchFailed`` when retries run out.

        The in-flight slot is held until ``handle`` returns: a download counts as in flight
        for as long as its body streams.
        """
        host = host_of(url)
        policy = self._retry
        reason = "no attempt made"
        for attempt in range(1, policy.max_attempts + 1):
            delay = backoff_delay(
                attempt - 1, base=policy.backoff_base, cap=policy.backoff_cap, jitter=self._jitter()
            )
            retry_after: str | None = None
            status: int | None = None
            try:
                async with self._limiter.slot(host) as waited:
                    metrics.RATE_LIMIT_WAIT.observe(waited)
                    request_headers = headers() if headers else None
                    async with self._client.stream("GET", url, headers=request_headers) as response:
                        status = response.status_code
                        metrics.HTTP_REQUESTS.labels(host=host, outcome=str(status)).inc()
                        if status not in RETRY_STATUSES:
                            return await handle(response, attempt)
                        retry_after = response.headers.get("retry-after")
            except httpx.TransportError as exc:
                metrics.HTTP_REQUESTS.labels(host=host, outcome="error").inc()
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                status = None
            host_paused = False
            if status is not None:
                reason = f"HTTP {status}"
                seconds = parse_retry_after(retry_after, self._now())
                if seconds is not None:
                    if seconds > policy.max_retry_after:
                        raise FetchFailed(
                            url, f"{reason}, Retry-After {seconds:.0f}s over limit", attempt
                        )
                    delay = seconds
                if status in PAUSE_HOST_STATUSES:
                    self._limiter.pause(host, delay)
                    host_paused = True
            if attempt == policy.max_attempts:
                break
            metrics.HTTP_RETRIES.labels(host=host).inc()
            self._log.warning("http_retry", url=url, reason=reason, attempt=attempt, delay=delay)
            if not host_paused:  # a paused host already makes the next slot wait
                await self._sleep(delay)
        raise FetchFailed(url, reason, policy.max_attempts)

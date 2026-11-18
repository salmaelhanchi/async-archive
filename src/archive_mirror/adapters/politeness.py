"""Rate limiting (requests per second per host) and concurrency limiting (requests in flight).

They answer different questions. The semaphore protects *our* resources (sockets, memory);
the token bucket protects *the server*. A run needs both.
"""

import asyncio
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class TokenBucket:
    """Token bucket expressed as GCRA (one "theoretical arrival time" instead of a token count).

    ``reserve`` books the next slot and returns how long the caller must wait for it. It does
    not await, so concurrent callers on one event loop can never book the same slot.
    """

    def __init__(self, rate: float, burst: int, clock: Clock) -> None:
        if rate <= 0 or burst < 1:
            raise ValueError("rate must be > 0 and burst >= 1")
        self._clock = clock
        self._interval = 1.0 / rate
        self._tolerance = (burst - 1) * self._interval
        self._tat = clock()
        self._paused_until = -math.inf

    @property
    def interval(self) -> float:
        return self._interval

    def reserve(self) -> float:
        now = self._clock()
        tat = max(self._tat, now)
        wait = max(0.0, tat - self._tolerance - now)
        self._tat = tat + self._interval
        return wait

    def pause_for(self, seconds: float) -> None:
        """Stop issuing slots for ``seconds`` (after a 429 or 503). Later slots queue behind it."""
        until = self._clock() + seconds
        self._paused_until = max(self._paused_until, until)
        self._tat = max(self._tat, until)

    def pause_remaining(self) -> float:
        return max(0.0, self._paused_until - self._clock())

    def slow_down(self, min_interval: float) -> None:
        """Never go faster than one request per ``min_interval`` seconds (robots.txt delay)."""
        if min_interval > self._interval:
            self._interval = min_interval
            self._tolerance = 0.0


class HostLimiter:
    """A global cap on requests in flight plus one token bucket per host."""

    def __init__(
        self,
        *,
        rate: float,
        burst: int,
        max_in_flight: int,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._rate, self._burst = rate, burst
        self._clock, self._sleep = clock, sleep
        self._semaphore = asyncio.Semaphore(max_in_flight)
        self._buckets: dict[str, TokenBucket] = {}

    def bucket(self, host: str) -> TokenBucket:
        if host not in self._buckets:
            self._buckets[host] = TokenBucket(self._rate, self._burst, self._clock)
        return self._buckets[host]

    def pause(self, host: str, seconds: float) -> None:
        self.bucket(host).pause_for(seconds)

    def slow_down(self, host: str, min_interval: float) -> None:
        self.bucket(host).slow_down(min_interval)

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[float]:
        """Hold one in-flight slot and one rate token for ``host``. Yields seconds waited.

        The semaphore is taken first so the request starts right after its token is due;
        the cost is that a slow host can hold slots other hosts could use (see ADR 003).
        """
        async with self._semaphore:
            bucket = self.bucket(host)
            waited = 0.0
            while True:
                wait = bucket.reserve()
                if wait > 0:
                    await self._sleep(wait)
                    waited += wait
                paused = bucket.pause_remaining()
                if paused <= 0:
                    break
                await self._sleep(paused)
                waited += paused
            yield waited

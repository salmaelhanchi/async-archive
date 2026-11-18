"""Token bucket and host limiter, on a fake clock."""

import asyncio

import pytest

from archive_mirror.adapters.politeness import HostLimiter, TokenBucket
from tests.fakes import FakeClock


def test_bucket_allows_a_burst_then_spaces_requests() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=1.0, burst=2, clock=clock)

    assert [bucket.reserve() for _ in range(5)] == [0.0, 0.0, 1.0, 2.0, 3.0]


def test_bucket_refills_while_idle() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=2.0, burst=1, clock=clock)
    bucket.reserve()

    clock.now = 10.0

    assert bucket.reserve() == 0.0
    assert bucket.reserve() == pytest.approx(0.5)


def test_pause_pushes_every_later_slot_behind_it() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=10.0, burst=1, clock=clock)

    bucket.pause_for(5.0)

    assert bucket.pause_remaining() == 5.0
    assert bucket.reserve() == pytest.approx(5.0)
    assert bucket.reserve() == pytest.approx(5.1)


def test_slow_down_only_ever_slows() -> None:
    bucket = TokenBucket(rate=10.0, burst=3, clock=FakeClock())

    bucket.slow_down(2.0)
    bucket.slow_down(0.01)

    assert bucket.interval == 2.0
    assert [bucket.reserve() for _ in range(3)] == [0.0, 2.0, 4.0]


def test_bucket_rejects_bad_config() -> None:
    with pytest.raises(ValueError, match="rate"):
        TokenBucket(rate=0, burst=1, clock=FakeClock())


async def test_limiter_caps_requests_in_flight() -> None:
    limiter = HostLimiter(rate=1000, burst=1000, max_in_flight=3)
    active = peak = 0

    async def request(host: str) -> None:
        nonlocal active, peak
        async with limiter.slot(host):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(request(f"h{i % 2}") for i in range(12)))

    assert peak == 3


async def test_limiter_rate_is_per_host() -> None:
    clock = FakeClock()
    limiter = HostLimiter(rate=1.0, burst=1, max_in_flight=10, clock=clock, sleep=clock.sleep)

    waits = []
    for host in ("a", "b", "a", "b"):
        async with limiter.slot(host) as waited:
            waits.append(waited)

    assert waits == [0.0, 0.0, 1.0, 0.0]


async def test_limiter_waits_out_a_pause() -> None:
    clock = FakeClock()
    limiter = HostLimiter(rate=100.0, burst=5, max_in_flight=10, clock=clock, sleep=clock.sleep)
    limiter.pause("a", 30.0)

    async with limiter.slot("a") as waited:
        pass

    assert waited == pytest.approx(30.0)
    assert clock.now >= 30.0

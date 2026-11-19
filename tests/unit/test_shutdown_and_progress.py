"""The two-step shutdown and the progress rate meter."""

import asyncio
import signal

import pytest

from archive_mirror.progress import RateMeter
from archive_mirror.shutdown import Interrupted, run_interruptible, shutdown_signals


async def _interrupted_twice(forced: list[str]) -> str:
    signal.raise_signal(signal.SIGINT)
    try:
        await asyncio.sleep(5)
    except asyncio.CancelledError:
        # Still cleaning up when the second press arrives.
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(0)
        assert forced == ["forced"]
        raise
    return "finished"


def test_the_first_signal_cancels_the_run_cleanly() -> None:
    cleaned: list[str] = []

    async def main() -> str:
        signal.raise_signal(signal.SIGINT)
        try:
            await asyncio.sleep(5)
        finally:
            cleaned.append("rolled back")
        return "finished"

    with pytest.raises(Interrupted):
        run_interruptible(main, force=lambda: pytest.fail("forced on the first press"))

    assert cleaned == ["rolled back"]


def test_the_second_signal_forces_the_exit() -> None:
    forced: list[str] = []

    with pytest.raises(Interrupted):
        run_interruptible(lambda: _interrupted_twice(forced), force=lambda: forced.append("forced"))

    assert forced == ["forced"]


def test_a_run_that_is_not_interrupted_returns_and_restores_the_handlers() -> None:
    def marker(_signum: int, _frame: object) -> None: ...

    # signal.signal returns the handler it replaces: install a marker, run, read it back.
    original = {sig: signal.signal(sig, marker) for sig in shutdown_signals()}

    async def main() -> int:
        return 7

    try:
        assert run_interruptible(main) == 7
    finally:
        after = {sig: signal.signal(sig, original[sig]) for sig in shutdown_signals()}
    assert all(handler is marker for handler in after.values())


def test_rate_meter_measures_over_a_sliding_window() -> None:
    now = [0.0]
    meter = RateMeter(window_s=2.0, clock=lambda: now[0])

    assert meter.add(0) == 0.0
    now[0] = 1.0
    assert meter.add(1_000) == 1_000.0
    now[0] = 2.0
    assert meter.add(2_000) == 1_000.0
    now[0] = 4.0  # 10 kB in the last two seconds: the old samples no longer count
    assert meter.add(12_000) == pytest.approx(5_000.0)

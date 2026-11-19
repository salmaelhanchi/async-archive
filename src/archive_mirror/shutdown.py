"""Ctrl+C in two steps: the first stops the run cleanly, the second stops it now.

First signal (Ctrl+C, SIGTERM from ``docker stop``, or Ctrl+Break on Windows): the main task
is cancelled, and the cancellation runs through every ``TaskGroup``:

* listers stop; a listing that was not committed is fetched again next time;
* downloads in flight roll their row back to ``listed`` and keep their ``.part`` for the
  range resume; a file already verified is finished and recorded first;
* extractions roll back to ``pending``; worker processes finish the archive they hold (into a
  temporary directory) while the queue behind them is dropped;
* the run is recorded as ``interrupted`` and the CLI exits with 130.

Second signal: worker processes are killed and the process exits at once. That is a hard
kill, and the state stays consistent then too: ``bench/kill_resume.py`` checks it.
"""

import asyncio
import multiprocessing
import os
import signal
import sys
from collections.abc import Callable, Coroutine
from types import FrameType
from typing import Any

import structlog

EXIT_INTERRUPTED = 130


class Interrupted(Exception):
    """The run stopped after a shutdown signal. Its state is saved; rerun to continue."""


def shutdown_signals() -> list[signal.Signals]:
    names = ("SIGINT", "SIGTERM", "SIGBREAK")  # SIGBREAK exists on Windows only
    return [getattr(signal, name) for name in names if hasattr(signal, name)]


def force_exit() -> None:
    """Kill worker processes and leave without cleanup, like a hard kill."""
    for child in multiprocessing.active_children():
        child.kill()
    sys.stderr.write("stopped immediately; the next run resumes from the saved state\n")
    sys.stderr.flush()
    os._exit(EXIT_INTERRUPTED)


def run_interruptible[T](
    main: Callable[[], Coroutine[Any, Any, T]],
    *,
    force: Callable[[], None] = force_exit,
) -> T:
    """``asyncio.run(main())`` with the two-step shutdown. Raises ``Interrupted`` after a
    graceful stop."""

    async def guarded() -> T:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - asyncio.run always runs a task
            raise RuntimeError("no running task")
        presses = 0

        def first_press() -> None:
            structlog.get_logger(__name__).warning(
                "shutdown_requested",
                hint="finishing what is in flight; press Ctrl+C again to stop now",
            )
            task.cancel()

        def on_signal(_signum: int, _frame: FrameType | None) -> None:
            # Runs between bytecodes of the main thread: hand the work to the loop.
            nonlocal presses
            presses += 1
            if presses == 1:
                loop.call_soon_threadsafe(first_press)
            else:
                force()

        previous = {sig: signal.signal(sig, on_signal) for sig in shutdown_signals()}
        try:
            return await main()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    try:
        return asyncio.run(guarded())
    except asyncio.CancelledError:
        raise Interrupted from None

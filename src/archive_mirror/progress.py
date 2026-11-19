"""Live progress on stderr: listing, downloads with bytes/s, extraction.

The services know nothing about it. ``composition.py`` reads their counters a few times a
second into a ``ProgressSnapshot`` and hands it to a ``ProgressSink``; this module draws it.
"""

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self

from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskID, TextColumn

from archive_mirror.report import human_bytes


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    directories: int  # listings recorded
    listing_finished: bool
    files_found: int
    files_queued: int
    files_finished: int  # done + failed + skipped
    files_failed: int
    bytes_received: int  # live: counted as chunks arrive
    archives_queued: int | None  # None: extraction is off
    archives_finished: int
    archives_failed: int


class ProgressSink(Protocol):
    def update(self, snapshot: ProgressSnapshot) -> None: ...


class RateMeter:
    """Bytes per second over a sliding window of recent samples."""

    def __init__(self, window_s: float = 3.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._window = window_s
        self._clock = clock
        self._samples: deque[tuple[float, int]] = deque()

    def add(self, total: int) -> float:
        now = self._clock()
        self._samples.append((now, total))
        while len(self._samples) > 2 and now - self._samples[1][0] >= self._window:
            self._samples.popleft()
        first_time, first_total = self._samples[0]
        elapsed = now - first_time
        return (total - first_total) / elapsed if elapsed > 0 else 0.0


class LiveProgress:
    """A ``rich`` progress display. Use as a context manager around the run."""

    def __init__(self, console: Console) -> None:
        self._progress = Progress(
            TextColumn("{task.description:<9}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("{task.fields[detail]}"),
            console=console,
            redirect_stderr=True,  # log lines print above the bars instead of through them
        )
        self._rate = RateMeter()
        self._listing = self._progress.add_task("Listing", total=None, detail="")
        self._files = self._progress.add_task("Download", total=0, detail="")
        self._archives: TaskID | None = None

    def __enter__(self) -> Self:
        self._progress.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._progress.stop()

    def update(self, s: ProgressSnapshot) -> None:
        speed = self._rate.add(s.bytes_received)
        self._progress.update(
            self._listing,
            total=s.directories if s.listing_finished else None,
            completed=s.directories,
            detail=f"{s.directories:,} directories · {s.files_found:,} files found",
        )
        failed = f" · {s.files_failed:,} failed" if s.files_failed else ""
        self._progress.update(
            self._files,
            total=s.files_queued,
            completed=s.files_finished,
            detail=f"{human_bytes(s.bytes_received)} · {human_bytes(int(speed))}/s{failed}",
        )
        if s.archives_queued is None:
            return
        if self._archives is None:
            self._archives = self._progress.add_task("Extract", total=0, detail="")
        failed = f"{s.archives_failed:,} failed" if s.archives_failed else ""
        self._progress.update(
            self._archives, total=s.archives_queued, completed=s.archives_finished, detail=failed
        )

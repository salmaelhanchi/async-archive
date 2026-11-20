"""A process pool for CPU-bound work, and the archive extractor that runs in it.

* ``spawn`` on every OS: the parent has threads (aiosqlite, ``to_thread``), and forking a
  threaded process can deadlock. It also makes Linux behave like Windows.
* Workers ignore Ctrl+C and Ctrl+Break. In a console those reach every process in the
  group, and a worker that died mid-archive would break the whole pool. The parent decides
  when to stop: it stops handing out work and waits for running jobs (see ``shutdown.py``).
* The pool is shut down from a thread, so the event loop stays responsive while it waits,
  and a second Ctrl+C can still force the exit.
"""

import asyncio
import multiprocessing
import signal
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import asynccontextmanager
from pathlib import Path

from archive_mirror.adapters.archive_extract import extract_archive
from archive_mirror.adapters.storage import MirrorStorage, safe_join
from archive_mirror.domain.errors import ExtractionFailed
from archive_mirror.domain.extraction import ExtractLimits, ExtractResult


def _ignore_interrupts() -> None:
    for name in ("SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_IGN)


@asynccontextmanager
async def worker_pool(workers: int) -> AsyncIterator[ProcessPoolExecutor]:
    pool = ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_ignore_interrupts,
    )
    try:
        yield pool
    finally:
        await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)


class PoolArchiveExtractor:
    """Unpacks ``<mirror root>/<path>`` into ``<extract root>/<path>/`` in a worker process."""

    def __init__(
        self,
        pool: ProcessPoolExecutor,
        storage: MirrorStorage,
        extract_root: Path,
        limits: ExtractLimits,
    ) -> None:
        self._pool = pool
        self._storage = storage
        self._root = extract_root.resolve()
        self._limits = limits

    def target(self, relative_path: str) -> Path:
        return safe_join(self._root, relative_path)

    async def extract(self, relative_path: str, name: str) -> ExtractResult:
        source = str(self._storage.path(relative_path))
        target = str(self.target(relative_path))
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                self._pool, extract_archive, source, target, name, self._limits
            )
        except BrokenProcessPool:
            raise ExtractionFailed("the worker process died") from None

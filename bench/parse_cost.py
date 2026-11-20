"""Should listing parsing leave the event loop? Measure both before moving it.

    uv run python -m bench.parse_cost

For each format and page size it times, on one CPU-quiet machine:

* parse on the loop: ``parse_listing`` called directly (what the lister does now);
* parse in a warm process pool: ``run_in_executor`` round trip, which adds pickling the
  page in and the ``Listing`` out;
* a burst of pages: all on the loop one after another, against 4 pool workers at once;
* end to end: a real dry run (``run_dry_run``) of 9 Apache directories of 1,000 entries,
  parsing on the loop and in the pool, with a probe that records how long the event loop
  was stalled. The archive server runs in its own process so it cannot be stalled with us.

Writes ``docs/benchmark/parse_cost.json``.
"""

import asyncio
import json
import multiprocessing
import os
import shutil
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from archive_mirror.adapters.listing import parse_listing
from archive_mirror.composition import run_dry_run
from archive_mirror.log_setup import configure_logging
from archive_mirror.settings import Settings
from bench.archive_server import RENDERERS
from bench.benchmark import RESULTS
from bench.tree import Node

OUT = RESULTS.with_name("parse_cost.json")
URL = "http://127.0.0.1:8080/pub/"
SIZES = (25, 1000)
REPEATS = 41
BURST = 200
POOL = 4
E2E_PORT = 8473
E2E_TREE = ["--style", "apache", "--depth", "1", "--dirs", "8", "--files", "1000"]


def page(style: str, entries: int) -> str:
    when = datetime(2024, 1, 1, 12, 0)
    nodes = [
        Node(f"file-{i:05d}.tar.gz", False, 1_000 + i * 7919, when + timedelta(minutes=i))
        for i in range(entries)
    ]
    return RENDERERS[style]("/pub/", nodes)


def _median_ms(samples: list[float]) -> float:
    return round(statistics.median(samples) * 1000, 3)


async def measure(pool: ProcessPoolExecutor, html: str) -> dict[str, float]:
    loop = asyncio.get_running_loop()
    on_loop: list[float] = []
    in_pool: list[float] = []
    for _ in range(REPEATS):
        started = time.perf_counter()
        parse_listing(html, URL)
        on_loop.append(time.perf_counter() - started)
        started = time.perf_counter()
        await loop.run_in_executor(pool, parse_listing, html, URL)
        in_pool.append(time.perf_counter() - started)

    started = time.perf_counter()
    for _ in range(BURST):
        parse_listing(html, URL)
    burst_loop = time.perf_counter() - started
    started = time.perf_counter()
    await asyncio.gather(
        *(loop.run_in_executor(pool, parse_listing, html, URL) for _ in range(BURST))
    )
    burst_pool = time.perf_counter() - started
    return {
        "page_kib": round(len(html.encode()) / 1024, 1),
        "loop_ms": _median_ms(on_loop),
        "pool_round_trip_ms": _median_ms(in_pool),
        f"burst_{BURST}_loop_s": round(burst_loop, 3),
        f"burst_{BURST}_pool{POOL}_s": round(burst_pool, 3),
    }


async def _loop_lag(stop: asyncio.Event, lags: list[float]) -> None:
    """Ask for a 5 ms sleep over and over; anything beyond 5 ms is time the loop was busy."""
    while not stop.is_set():
        started = time.perf_counter()
        await asyncio.sleep(0.005)
        lags.append(time.perf_counter() - started - 0.005)


async def _dry_run(url: str, work: Path, min_pool_bytes: int) -> dict[str, float]:
    await asyncio.to_thread(shutil.rmtree, work, ignore_errors=True)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        state_db=work / "state.sqlite",
        per_host_rate=10_000,
        per_host_burst=64,
        listing_workers=4,
        max_in_flight=4,
        parse_workers=POOL,
        parse_in_pool_min_bytes=min_pool_bytes,
        http2=False,
    )
    stop, lags = asyncio.Event(), []
    probe = asyncio.create_task(_loop_lag(stop, lags))
    started = time.perf_counter()
    summary = await run_dry_run(settings, [url], "parse-cost")
    seconds = time.perf_counter() - started
    stop.set()
    await probe
    await asyncio.to_thread(shutil.rmtree, work, ignore_errors=True)
    return {
        "files": summary.files,
        "seconds": round(seconds, 3),
        "max_loop_stall_ms": round(max(lags) * 1000, 1),
        "p99_loop_stall_ms": round(statistics.quantiles(lags, n=100)[98] * 1000, 1),
    }


async def end_to_end(work: Path) -> dict[str, dict[str, float]]:
    server = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "bench.archive_server",
        *E2E_TREE,
        "--latency-ms",
        "0",
        "--port",
        str(E2E_PORT),
        stdout=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(100):  # wait until it accepts connections
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", E2E_PORT)
            except OSError:
                await asyncio.sleep(0.1)
                continue
            writer.close()
            await writer.wait_closed()
            break
        url = f"http://127.0.0.1:{E2E_PORT}/"
        results = {}
        for label, min_bytes in (("on_loop", 2**40), ("in_pool", 8 * 1024)):
            results[label] = await _dry_run(url, work / label, min_bytes)
            print(f"end to end, parse {label}: {results[label]}", flush=True)
        return results
    finally:
        server.terminate()
        await server.wait()


async def main_async(work: Path) -> dict[str, object]:
    started = time.perf_counter()
    pool = ProcessPoolExecutor(POOL, mp_context=multiprocessing.get_context("spawn"))
    loop = asyncio.get_running_loop()
    warm = page("nginx", 5)
    await asyncio.gather(
        *(loop.run_in_executor(pool, parse_listing, warm, URL) for _ in range(POOL * 2))
    )
    pool_start_s = round(time.perf_counter() - started, 3)
    results: dict[str, dict[str, float]] = {}
    try:
        for style in RENDERERS:
            for entries in SIZES:
                results[f"{style}-{entries}"] = await measure(pool, page(style, entries))
                print(f"{style:6} {entries:5} entries: {results[f'{style}-{entries}']}", flush=True)
    finally:
        await asyncio.to_thread(pool.shutdown)
    configure_logging(level="WARNING", json_output=False, run_id="parse-cost")
    e2e = await end_to_end(work)
    return {
        "pool_workers": POOL,
        "pool_start_s": pool_start_s,
        "cpus": os.cpu_count(),
        "pages": results,
        "end_to_end_apache_9x1000": e2e,
    }


def main() -> None:
    report = asyncio.run(main_async(Path("data/benchmark/parse-cost").resolve()))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8", newline="\n")
    print(f"pool start {report['pool_start_s']} s; wrote {OUT}")


if __name__ == "__main__":
    main()

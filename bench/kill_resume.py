"""Kill the mirror at random moments, resume it, and check every byte.

    uv run python -m bench.kill_resume --trials 20

Each trial starts from an empty state file and mirror directory. It runs the real CLI
(``archive-mirror mirror <url>``) as a child process and kills it hard (SIGKILL on POSIX,
TerminateProcess on Windows) after a random delay, one to three times. Then it runs the CLI
to completion and checks that:

* every file's SHA-256 equals the bytes the server holds,
* no ``.part`` file is left behind,
* the state database records every file as ``done``.

A hard kill gives the program no chance to clean up, so this tests what is on disk and in
SQLite at every instant, not the Ctrl+C path.
"""

import argparse
import asyncio
import hashlib
import logging
import os
import random
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from archive_mirror.domain.urls import local_relative_path
from bench.archive_server import RANGE_HITS, ServerConfig, make_app
from bench.tree import Archive, TreeSpec

# 4 directories, 16 data files of 0.1-1.2 MB each with a .sha256 sidecar: 32 files, ~10 MB
SPEC = TreeSpec(
    depth=1, dirs_per_dir=3, files_per_dir=4, min_size=100_000, max_size=1_200_000, checksums=True
)
BODY_RATE = 3_000_000  # bytes/s per response, so transfers are in flight when the kill lands


@dataclass
class Trial:
    number: int
    kill_after_s: list[float] = field(default_factory=list)
    killed: int = 0  # kills that landed before the run finished on its own
    parts_left: int = 0  # .part files found right after a kill: interrupted mid-file
    range_requests: int = 0
    problems: list[str] = field(default_factory=list)


def check_mirror(root: Path, state_db: Path, base_url: str, archive: Archive) -> list[str]:
    problems: list[str] = []
    for path in archive.files:
        local = root / local_relative_path(base_url.rstrip("/") + path)
        if not local.is_file():
            problems.append(f"missing {path}")
        elif hashlib.sha256(local.read_bytes()).hexdigest() != archive.digest(path):
            problems.append(f"differs {path}")
    problems += [f"leftover {p.name}" for p in root.rglob("*.part*")]
    with sqlite3.connect(state_db) as db:
        rows = dict(db.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())
    if rows != {"done": len(archive.files)}:
        problems.append(f"state is {rows}, expected {len(archive.files)} done")
    return problems


def _env(work: Path) -> dict[str, str]:
    return os.environ | {
        "ARCHIVE_MIRROR_STATE_DB": str(work / "state.sqlite"),
        "ARCHIVE_MIRROR_MIRROR_ROOT": str(work / "mirror"),
        "ARCHIVE_MIRROR_DOWNLOAD_WORKERS": "4",
        "ARCHIVE_MIRROR_MAX_IN_FLIGHT": "6",
        "ARCHIVE_MIRROR_PER_HOST_RATE": "1000",
        "ARCHIVE_MIRROR_PER_HOST_BURST": "50",
        "ARCHIVE_MIRROR_MIN_FREE_BYTES": "0",
        "ARCHIVE_MIRROR_LOG_LEVEL": "ERROR",
    }


async def run_cli(url: str, work: Path, *, kill_after: float | None) -> bool:
    """Run the CLI once. Returns True if it was killed, False if it finished on its own."""
    await asyncio.to_thread(work.mkdir, parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "archive_mirror",
        "mirror",
        url,
        cwd=work,  # so no project .env is picked up
        env=_env(work),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if kill_after is None:
        code = await process.wait()
        if code != 0:
            raise RuntimeError(f"the final run exited with {code}")
        return False
    try:
        await asyncio.wait_for(process.wait(), kill_after)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:  # it exited on its own as the timer fired
            await process.wait()
            return False
        await process.wait()
        return True
    return False


async def run_trials(
    url: str, work: Path, archive: Archive, range_hits: Counter[str], *, trials: int, seed: int
) -> list[Trial]:
    """Calibrate on one clean run, then kill at uniform points across that duration."""
    started = time.perf_counter()
    await run_cli(url, work / "calibration", kill_after=None)
    duration = time.perf_counter() - started
    rng = random.Random(seed)
    results: list[Trial] = []
    for number in range(1, trials + 1):
        trial = Trial(number)
        trial_dir = work / f"trial-{number:02d}"
        ranges_before = sum(range_hits.values())
        for _ in range(rng.randint(1, 3)):
            delay = rng.uniform(0.05, 1.0) * duration
            trial.kill_after_s.append(round(delay, 2))
            if await run_cli(url, trial_dir, kill_after=delay):
                trial.killed += 1
                trial.parts_left += len(list((trial_dir / "mirror").rglob("*.part")))
        await run_cli(url, trial_dir, kill_after=None)
        trial.range_requests = sum(range_hits.values()) - ranges_before
        trial.problems = check_mirror(
            trial_dir / "mirror", trial_dir / "state.sqlite", url, archive
        )
        results.append(trial)
    return results


def quiet_server_logs() -> None:
    """A killed client makes aiohttp log a traceback per open response; that is expected."""
    logging.getLogger("aiohttp.server").setLevel(logging.CRITICAL)


async def main_async(args: argparse.Namespace) -> int:
    quiet_server_logs()
    archive = Archive(SPEC)
    app = make_app(ServerConfig(tree=SPEC, body_rate=BODY_RATE))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}/"
    print(f"{len(archive.files)} files, {archive.total_bytes:,} bytes; {args.trials} trials")
    try:
        results = await run_trials(
            url, Path(args.work), archive, app[RANGE_HITS], trials=args.trials, seed=args.seed
        )
    finally:
        await runner.cleanup()
    for t in results:
        verdict = "ok" if not t.problems else "FAIL " + "; ".join(t.problems[:3])
        print(
            f"trial {t.number:2d}: kills at {t.kill_after_s} s, landed {t.killed},"
            f" parts left {t.parts_left}, range requests {t.range_requests}: {verdict}"
        )
    passed = sum(not t.problems for t in results)
    print(
        f"\n{passed}/{len(results)} trials byte-identical;"
        f" {sum(t.killed for t in results)} kills landed,"
        f" {sum(t.parts_left for t in results)} partial files resumed,"
        f" {sum(t.range_requests for t in results)} range requests"
    )
    return 0 if passed == len(results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Kill-and-resume correctness check.")
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--seed", type=int, default=2024)
    parser.add_argument("--work", default="data/kill-resume")
    sys.exit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()

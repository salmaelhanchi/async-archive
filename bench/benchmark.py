"""The sequential baseline against the asyncio mirror, under the same politeness cap.

    uv run python -m bench.benchmark --rounds 3

One local archive server (IIS listings, the format the baseline reads) with 20 ms of
latency per request and each response held to ``BODY_RATE`` bytes/s, like a remote
mirror whose per-connection bandwidth is the limit. Every configuration runs in a child
process (``bench.measure``) from an empty directory, and every mirrored file is checked
against the server's bytes before a run counts.

The cap: the baseline sleeps 0.5 s before each file, so it never requests more than two
files a second. The mirror gets the same ceiling, 2 requests/s per host with no burst, for
listings and robots.txt too. Extraction is off in the mirror runs: the baseline unpacked in
a separate script, so both sides only list and download.

Writes ``docs/benchmark/results.json``; ``python -m bench.chart`` draws the chart from it.
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import shutil
import statistics
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from aiohttp import web

from archive_mirror.domain.urls import local_relative_path
from bench.archive_server import ServerConfig, make_app
from bench.tree import Archive, TreeSpec
from bench.v1_sequential import SequentialMirror

PROJECT = Path(__file__).resolve().parent.parent
RESULTS = PROJECT / "docs" / "benchmark" / "results.json"

# 4 directories, 40 files of 0.5-1.5 MB (about 40 MB)
SPEC = TreeSpec(
    depth=1, dirs_per_dir=3, files_per_dir=10, min_size=500_000, max_size=1_500_000, seed=11
)
LATENCY_MS = 20
BODY_RATE = 1_000_000  # bytes/s per response
CAP = 2.0  # requests/s per host: the baseline's own ceiling


@dataclass(frozen=True)
class Config:
    label: str
    kind: str  # "v1" or "v2"
    workers: int
    rate: float | None  # None: no cap (for scale only)


CONFIGS = (
    Config("v1 sequential (requests)", "v1", 1, CAP),
    Config("v2, 1 worker", "v2", 1, CAP),
    Config("v2, 4 workers", "v2", 4, CAP),
    Config("v2, 8 workers", "v2", 8, CAP),
    Config("v2, 16 workers", "v2", 16, CAP),
    Config("v2, 16 workers, no cap", "v2", 16, None),
)


def _v2_env(config: Config, work: Path) -> dict[str, str]:
    return {
        "ARCHIVE_MIRROR_STATE_DB": str(work / "state.sqlite"),
        "ARCHIVE_MIRROR_MIRROR_ROOT": str(work / "out"),
        "ARCHIVE_MIRROR_EXTRACT": "false",
        "ARCHIVE_MIRROR_LISTING_WORKERS": str(min(config.workers, 4)),
        "ARCHIVE_MIRROR_DOWNLOAD_WORKERS": str(config.workers),
        "ARCHIVE_MIRROR_MAX_IN_FLIGHT": str(config.workers),
        "ARCHIVE_MIRROR_PER_HOST_RATE": str(config.rate or 10_000),
        "ARCHIVE_MIRROR_PER_HOST_BURST": "1" if config.rate else "64",
        "ARCHIVE_MIRROR_MIN_FREE_BYTES": "0",
        "ARCHIVE_MIRROR_LOG_LEVEL": "WARNING",
        "ARCHIVE_MIRROR_CONTACT_URL": "https://github.com/",
    }


def _local(config: Config, work: Path, url: str) -> Path:
    if config.kind == "v1":
        return SequentialMirror(work / "out").local_path(url)
    return work / "out" / local_relative_path(url)


def _fresh(work: Path) -> None:
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)


def _collect(config: Config, base_url: str, work: Path, archive: Archive) -> dict[str, float]:
    """Read the measurement, check every file against the server's bytes, free the disk."""
    measured = json.loads((work / "result.json").read_text(encoding="utf-8"))
    wrong = [
        path
        for path in archive.files
        if not (local := _local(config, work, base_url.rstrip("/") + path)).is_file()
        or hashlib.sha256(local.read_bytes()).hexdigest() != archive.digest(path)
    ]
    shutil.rmtree(work, ignore_errors=True)  # the disk is small; keep one run at a time
    if measured["exit"] != 0 or wrong:
        raise RuntimeError(f"{config.label}: exit {measured['exit']}, {len(wrong)} files wrong")
    seconds = measured["seconds"]
    return {
        "seconds": seconds,
        "files_per_s": len(archive.files) / seconds,
        "mb_per_s": archive.total_bytes / seconds / 1e6,
        "peak_rss_mb": measured["peak_rss"] / 2**20,
    }


async def run_one(config: Config, base_url: str, work: Path, archive: Archive) -> dict[str, float]:
    await asyncio.to_thread(_fresh, work)
    if config.kind == "v1":
        target = ["bench.v1_sequential", base_url, "--out", str(work / "out")]
        extra: dict[str, str] = {}
    else:
        target = ["archive_mirror", "mirror", base_url, "--no-progress"]
        extra = _v2_env(config, work)
    env = os.environ | extra | {"PYTHONPATH": str(PROJECT)}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "bench.measure",
        str(work / "result.json"),
        "--",
        *target,
        cwd=work,  # no project .env
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()
    # Hashing ~40 MB happens while the server is idle, so it cannot skew the next run.
    return await asyncio.to_thread(_collect, config, base_url, work, archive)


async def main_async(rounds: int, work: Path) -> int:
    archive = Archive(SPEC)
    app = make_app(ServerConfig(style="iis", tree=SPEC, latency_ms=LATENCY_MS, body_rate=BODY_RATE))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}/"
    print(f"{len(archive.files)} files, {archive.total_bytes:,} bytes, {rounds} rounds", flush=True)
    raw: dict[str, list[dict[str, float]]] = {c.label: [] for c in CONFIGS}
    try:
        for number in range(1, rounds + 1):
            for config in CONFIGS:
                row = await run_one(config, url, work / "run", archive)
                raw[config.label].append(row)
                print(f"round {number} {config.label}: {row['seconds']:.2f} s", flush=True)
    finally:
        await runner.cleanup()

    medians = {
        label: {key: statistics.median(r[key] for r in rows) for key in rows[0]}
        for label, rows in raw.items()
    }
    baseline = medians[CONFIGS[0].label]["seconds"]
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(
        json.dumps(
            {
                "setup": {
                    "files": len(archive.files),
                    "bytes": archive.total_bytes,
                    "listing_format": "iis",
                    "latency_ms": LATENCY_MS,
                    "body_rate_bytes_per_s": BODY_RATE,
                    "cap_requests_per_s": CAP,
                    "rounds": rounds,
                    "python": platform.python_version(),
                    "os": platform.system(),
                    "cpus": os.cpu_count(),
                },
                "configs": [asdict(c) for c in CONFIGS],
                "median": medians,
                "rounds": raw,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("\n| Run | Wall time | Files/s | MB/s | Peak RSS | vs v1 |")
    print("|---|---:|---:|---:|---:|---:|")
    for config in CONFIGS:
        m = medians[config.label]
        print(
            f"| {config.label} | {m['seconds']:.1f} s | {m['files_per_s']:.2f} |"
            f" {m['mb_per_s']:.2f} | {m['peak_rss_mb']:.0f} MiB |"
            f" {baseline / m['seconds']:.1f}x |"
        )
    print(f"\nwrote {RESULTS.relative_to(PROJECT)}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Sequential baseline vs asyncio mirror.")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--work", default="data/benchmark")
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args.rounds, Path(args.work).resolve())))


if __name__ == "__main__":
    main()

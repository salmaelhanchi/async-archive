"""Run the real CLI against the local archive server, end to end, on any OS.

    uv run python -m bench.demo --style iis --workers 8              # dry run
    uv run python -m bench.demo --mirror --max-size 2000000 --checksums  # download everything

Starts the server on a free port, runs ``archive-mirror mirror <url>`` in a child process
with its own state database and mirror directory, prints the CLI's summary, stops the server.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from aiohttp import web

from bench.archive_server import STYLES, ServerConfig, make_app
from bench.tree import Archive, TreeSpec


async def run(args: argparse.Namespace) -> int:
    spec = TreeSpec(
        depth=args.depth,
        dirs_per_dir=args.dirs,
        files_per_dir=args.files,
        max_size=args.max_size,
        checksums=args.checksums,
    )
    archive = Archive(spec)
    config = ServerConfig(args.style, spec, args.latency_ms, body_rate=args.body_rate)
    runner = web.AppRunner(make_app(config))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", args.port)
    await site.start()
    port = runner.addresses[0][1]
    print(
        f"archive server: {len(archive.tree)} {args.style} listings, {len(archive.files):,}"
        f" files, {archive.total_bytes:,} bytes on port {port}",
        flush=True,
    )

    state_dir = Path(args.state_dir)
    mode = "mirror" if args.mirror else "dry-run"
    env = os.environ | {
        "ARCHIVE_MIRROR_STATE_DB": str(state_dir / f"demo-{mode}-{args.style}.sqlite"),
        "ARCHIVE_MIRROR_MIRROR_ROOT": str(state_dir / f"demo-mirror-{args.style}"),
        "ARCHIVE_MIRROR_LISTING_WORKERS": str(args.workers),
        "ARCHIVE_MIRROR_DOWNLOAD_WORKERS": str(args.workers),
        "ARCHIVE_MIRROR_MAX_IN_FLIGHT": str(args.workers),
        "ARCHIVE_MIRROR_PER_HOST_RATE": str(args.rate),
        "ARCHIVE_MIRROR_PER_HOST_BURST": str(args.workers),
        "ARCHIVE_MIRROR_LOG_LEVEL": os.environ.get("ARCHIVE_MIRROR_LOG_LEVEL", "WARNING"),
    }
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "archive_mirror",
            "mirror",
            f"http://127.0.0.1:{port}/",
            *([] if args.mirror else ["--dry-run"]),
            *(["--json"] if args.json else []),
            env=env,
        )
        return await process.wait()
    finally:
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the CLI against the local archive server.")
    parser.add_argument("--style", choices=STYLES, default="nginx")
    parser.add_argument("--mirror", action="store_true", help="download, not just list")
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--dirs", type=int, default=4)
    parser.add_argument("--files", type=int, default=25)
    parser.add_argument("--max-size", type=int, default=5_000_000)
    parser.add_argument("--checksums", action="store_true", help="publish .sha256 sidecars")
    parser.add_argument("--latency-ms", type=int, default=20)
    parser.add_argument("--body-rate", type=int, default=None, help="bytes/s per response")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--rate", type=float, default=100.0, help="requests/s per host")
    parser.add_argument(
        "--port", type=int, default=0, help="fixed port, so a rerun sees the same URLs"
    )
    parser.add_argument("--state-dir", default="data")
    parser.add_argument("--json", action="store_true")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()

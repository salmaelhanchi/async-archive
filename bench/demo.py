"""Run the real CLI against the local archive server, end to end, on any OS.

    uv run python -m bench.demo --style iis --workers 8

Starts the server on a free port, runs ``archive-mirror mirror <url> --dry-run`` in a child
process with its own state database, prints the CLI's summary and stops the server.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from aiohttp import web

from bench.archive_server import STYLES, ServerConfig, TreeSpec, build_tree, make_app


async def run(args: argparse.Namespace) -> int:
    spec = TreeSpec(depth=args.depth, dirs_per_dir=args.dirs, files_per_dir=args.files)
    runner = web.AppRunner(make_app(ServerConfig(args.style, spec, args.latency_ms)))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    print(f"archive server: {len(build_tree(spec))} {args.style} listings on port {port}")

    state_db = Path(args.state_dir) / f"demo-{args.style}.sqlite"
    env = os.environ | {
        "ARCHIVE_MIRROR_STATE_DB": str(state_db),
        "ARCHIVE_MIRROR_LISTING_WORKERS": str(args.workers),
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
            "--dry-run",
            *(["--json"] if args.json else []),
            env=env,
        )
        return await process.wait()
    finally:
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Dry run against the local archive server.")
    parser.add_argument("--style", choices=STYLES, default="nginx")
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--dirs", type=int, default=4)
    parser.add_argument("--files", type=int, default=25)
    parser.add_argument("--latency-ms", type=int, default=20)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--rate", type=float, default=100.0, help="requests/s per host")
    parser.add_argument("--state-dir", default="data")
    parser.add_argument("--json", action="store_true")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()

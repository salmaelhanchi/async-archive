"""Command line entry point: ``archive-mirror``."""

import asyncio
import dataclasses
import json
import time
from pathlib import Path
from typing import Annotated

import structlog
import typer
from rich.console import Console
from rich.table import Table

from archive_mirror import __version__, metrics
from archive_mirror.composition import run_dry_run
from archive_mirror.domain.models import RunSummary
from archive_mirror.domain.status import ListingStatus
from archive_mirror.domain.urls import as_directory_url
from archive_mirror.log_setup import configure_logging, new_run_id
from archive_mirror.settings import Settings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Mirror public archives published as nested directory listings.",
)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_show_version, is_eager=True)
    ] = False,
) -> None:
    """Mirror public archives published as nested directory listings."""


def _start_urls(urls: list[str] | None, urls_file: Path | None) -> list[str]:
    raw = list(urls or [])
    if urls_file is not None:
        lines = urls_file.read_text(encoding="utf-8").splitlines()
        raw += [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    start: list[str] = []
    for url in raw:
        try:
            start.append(as_directory_url(url))
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
    if not start:
        raise typer.BadParameter("give at least one URL or --urls-file")
    return list(dict.fromkeys(start))


def human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _render(summary: RunSummary, elapsed: float, console: Console) -> None:
    listed = summary.listings[ListingStatus.LISTED]
    table = Table(title=f"Dry run {summary.run_id}", show_header=False)
    table.add_column(style="bold")
    table.add_column(justify="right")
    table.add_row("Directories listed", f"{listed:,}")
    table.add_row("Directories failed", f"{summary.listings[ListingStatus.FAILED]:,}")
    table.add_row("Directories skipped", f"{summary.listings[ListingStatus.SKIPPED]:,}")
    formats = ", ".join(f"{k} {v:,}" for k, v in sorted(summary.listings_by_format.items()))
    table.add_row("Listing formats", formats or "-")
    table.add_row("Files", f"{summary.files:,}")
    table.add_row("Total size", f"{human_bytes(summary.bytes_listed)} ({summary.bytes_listed:,} B)")
    table.add_row("  of which rounded by server", f"{summary.files_size_approximate:,} files")
    table.add_row("  size unknown", f"{summary.files_size_unknown:,} files")
    table.add_row("Archives", f"{summary.archives:,}")
    table.add_row("Already mirrored", f"{summary.files_already_done:,}")
    rate = listed / elapsed if elapsed > 0 else 0.0
    table.add_row("Elapsed", f"{elapsed:.2f} s ({rate:.1f} listings/s)")
    console.print(table)


@app.command()
def mirror(
    urls: Annotated[
        list[str] | None, typer.Argument(help="Directory listing URLs to start from.")
    ] = None,
    urls_file: Annotated[
        Path | None,
        typer.Option("--urls-file", exists=True, dir_okay=False, help="One start URL per line."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="List and record everything, report count and size."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print the summary as JSON.")] = False,
) -> None:
    """Crawl the listings under each URL and record every file in the state database."""
    start = _start_urls(urls, urls_file)
    if not dry_run:
        typer.echo("Downloading is not implemented yet; run with --dry-run.", err=True)
        raise typer.Exit(2)

    settings = Settings()
    run_id = new_run_id()
    configure_logging(level=settings.log_level, json_output=settings.log_json, run_id=run_id)
    log = structlog.get_logger(__name__)
    if settings.metrics_port:
        metrics.serve(settings.metrics_port)
    if not settings.contact_url:
        log.warning("no_contact_url", hint="set ARCHIVE_MIRROR_CONTACT_URL for the User-Agent")
    log.info("run_started", mode="dry_run", start_urls=start, user_agent=settings.user_agent)

    started = time.perf_counter()
    try:
        summary = asyncio.run(run_dry_run(settings, start, run_id))
    except KeyboardInterrupt:
        log.warning("run_interrupted", hint="state so far is saved; rerun to continue")
        raise typer.Exit(130) from None
    elapsed = time.perf_counter() - started
    log.info("run_finished", seconds=round(elapsed, 3), files=summary.files)

    if json_output:
        payload = dataclasses.asdict(summary) | {"elapsed_s": round(elapsed, 3)}
        payload["listings"] = {k.value: v for k, v in summary.listings.items()}
        typer.echo(json.dumps(payload, indent=2))
    else:
        _render(summary, elapsed, Console())

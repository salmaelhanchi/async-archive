"""Command line entry point: ``archive-mirror``."""

import sys
import time
from collections.abc import Callable, Coroutine
from contextlib import nullcontext
from pathlib import Path
from typing import Annotated, Any

import structlog
import typer
from rich.console import Console

from archive_mirror import __version__, metrics
from archive_mirror.composition import run_dry_run, run_mirror
from archive_mirror.domain.errors import StateLocked
from archive_mirror.domain.models import MirrorSummary, RunSummary
from archive_mirror.domain.urls import as_directory_url
from archive_mirror.log_setup import configure_logging, new_run_id
from archive_mirror.progress import LiveProgress, ProgressSink
from archive_mirror.report import render_dry_run, render_mirror, to_json
from archive_mirror.settings import Settings
from archive_mirror.shutdown import EXIT_INTERRUPTED, Interrupted, run_interruptible

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Mirror public archives published as nested directory listings.",
)

EXIT_LOCKED = 3


ProgressOption = Annotated[
    bool | None,
    typer.Option(
        "--progress/--no-progress",
        help="Live progress bars on stderr. Default: on when stderr is a terminal.",
        show_default=False,
    ),
]


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


def _run[S: (RunSummary, MirrorSummary)](
    mode: str,
    start: list[str],
    job: Callable[[Settings, str, ProgressSink | None], Coroutine[Any, Any, S]],
    *,
    progress: bool | None = None,
) -> tuple[S, float]:
    """Shared setup for every command: settings, logging, metrics, Ctrl+C and the lock.

    ``progress`` None means: show it when stderr is a terminal and logs are not JSON. While
    it is shown, only warnings and errors are logged; the bars carry the rest.
    """
    settings = Settings()
    run_id = new_run_id()
    if progress is None:
        progress = sys.stderr.isatty() and not settings.log_json
    level = settings.log_level
    if progress and level in ("DEBUG", "INFO"):
        level = "WARNING"
    configure_logging(level=level, json_output=settings.log_json, run_id=run_id)
    log = structlog.get_logger(__name__)
    if settings.metrics_port:
        metrics.serve(settings.metrics_port)
    if not settings.contact_url:
        log.warning("no_contact_url", hint="set ARCHIVE_MIRROR_CONTACT_URL for the User-Agent")
    log.info("run_started", mode=mode, start_urls=start, user_agent=settings.user_agent)

    started = time.perf_counter()
    display = LiveProgress(Console(stderr=True)) if progress else None
    try:
        with display or nullcontext():
            summary = run_interruptible(lambda: job(settings, run_id, display))
    except StateLocked as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_LOCKED) from None
    except Interrupted:
        log.warning("run_interrupted", hint="state so far is saved; rerun to continue")
        raise typer.Exit(EXIT_INTERRUPTED) from None
    elapsed = time.perf_counter() - started
    log.info("run_finished", seconds=round(elapsed, 3))
    return summary, elapsed


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
    progress: ProgressOption = None,
) -> None:
    """List everything under each URL, download what is new or changed, unpack archives.

    Safe to rerun after a crash or Ctrl+C: finished files are skipped and partial ones
    resume where they stopped. Ctrl+C once stops cleanly; twice stops at once.
    """
    start = _start_urls(urls, urls_file)
    if dry_run:
        crawl, elapsed = _run(
            "dry_run",
            start,
            lambda settings, run_id, _sink: run_dry_run(settings, start, run_id),
            progress=False,
        )
        if json_output:
            typer.echo(to_json(crawl, elapsed))
        else:
            render_dry_run(crawl, elapsed, Console())
        return
    summary, elapsed = _run(
        "mirror",
        start,
        lambda settings, run_id, sink: run_mirror(settings, start, run_id, sink),
        progress=False if json_output else progress,
    )
    _print_mirror(summary, elapsed, json_output=json_output)


@app.command()
def resume(
    json_output: Annotated[bool, typer.Option("--json", help="Print the summary as JSON.")] = False,
    progress: ProgressOption = None,
) -> None:
    """Download every file the state database still has pending, without listing again,
    and unpack archives that are still waiting."""
    summary, elapsed = _run(
        "resume",
        [],
        lambda settings, run_id, sink: run_mirror(settings, [], run_id, sink),
        progress=False if json_output else progress,
    )
    _print_mirror(summary, elapsed, json_output=json_output)


def _print_mirror(summary: MirrorSummary, elapsed: float, *, json_output: bool) -> None:
    if json_output:
        typer.echo(to_json(summary, elapsed))
    else:
        render_mirror(summary, elapsed, Console())
    if summary.downloads.files_failed:
        raise typer.Exit(1)

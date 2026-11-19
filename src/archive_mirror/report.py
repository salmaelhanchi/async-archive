"""Run summaries for people (rich tables) and for scripts (JSON)."""

import dataclasses
import json
from typing import Any

from rich.console import Console
from rich.table import Table

from archive_mirror.domain.models import MirrorSummary, RunSummary
from archive_mirror.domain.status import ExtractStatus, FileStatus, ListingStatus


def human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _table(title: str) -> Table:
    table = Table(title=title, show_header=False)
    table.add_column(style="bold")
    table.add_column(justify="right")
    return table


def _crawl_rows(table: Table, summary: RunSummary) -> None:
    listed = summary.listings[ListingStatus.LISTED]
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


def render_dry_run(summary: RunSummary, elapsed: float, console: Console) -> None:
    table = _table(f"Dry run {summary.run_id}")
    _crawl_rows(table, summary)
    table.add_row("Already mirrored", f"{summary.files_already_done:,}")
    listed = summary.listings[ListingStatus.LISTED]
    rate = listed / elapsed if elapsed > 0 else 0.0
    table.add_row("Elapsed", f"{elapsed:.2f} s ({rate:.1f} listings/s)")
    console.print(table)


def render_mirror(summary: MirrorSummary, elapsed: float, console: Console) -> None:
    title = "Mirror" if summary.crawl else "Resume"
    table = _table(f"{title} {summary.run_id}")
    if summary.crawl is not None:
        _crawl_rows(table, summary.crawl)
        table.add_section()
    d = summary.downloads
    table.add_row("Downloaded this run", f"{d.files_done:,} files")
    table.add_row("  resumed from a .part", f"{d.files_resumed:,}")
    table.add_row("  verified by published checksum", f"{d.files_verified_by_checksum:,}")
    table.add_row("Failed this run", f"{d.files_failed:,}")
    table.add_row("Skipped (budget or disk)", f"{d.files_skipped:,}")
    speed = d.bytes_received / elapsed if elapsed > 0 else 0.0
    table.add_row("Received", f"{human_bytes(d.bytes_received)} ({human_bytes(int(speed))}/s)")
    table.add_section()
    by_status = summary.files_by_status
    done = by_status[FileStatus.DONE]
    table.add_row("State: done", f"{done:,} ({human_bytes(summary.bytes_done)})")
    table.add_row("State: failed", f"{by_status[FileStatus.FAILED]:,}")
    pending = by_status[FileStatus.LISTED] + by_status[FileStatus.DOWNLOADING]
    table.add_row("State: still to download", f"{pending:,}")
    _extraction_rows(table, summary)
    rate = d.files_done / elapsed if elapsed > 0 else 0.0
    table.add_row("Elapsed", f"{elapsed:.2f} s ({rate:.1f} files/s)")
    console.print(table)


def _extraction_rows(table: Table, summary: MirrorSummary) -> None:
    archives = summary.archives_by_status
    if summary.extraction is None and not any(archives.values()):
        return
    table.add_section()
    if (e := summary.extraction) is not None:
        table.add_row("Unpacked this run", f"{e.archives_done:,} archives")
        table.add_row("  files written", f"{e.files_written:,} ({human_bytes(e.bytes_written)})")
        table.add_row("  failed", f"{e.archives_failed:,}")
        table.add_row("  format not supported", f"{e.archives_unsupported:,}")
    else:
        table.add_row("Unpacking", "off (ARCHIVE_MIRROR_EXTRACT=false)")
    waiting = archives[ExtractStatus.PENDING] + archives[ExtractStatus.EXTRACTING]
    table.add_row("State: archives unpacked", f"{archives[ExtractStatus.DONE]:,}")
    table.add_row("State: archives to unpack", f"{waiting + archives[ExtractStatus.FAILED]:,}")


def _plain(value: Any) -> Any:
    """Enum keys and values as plain strings, for JSON."""
    if isinstance(value, dict):
        return {str(getattr(k, "value", k)): _plain(v) for k, v in value.items()}
    return value


def to_json(summary: RunSummary | MirrorSummary, elapsed: float) -> str:
    payload = _plain(dataclasses.asdict(summary)) | {"elapsed_s": round(elapsed, 3)}
    return json.dumps(payload, indent=2)

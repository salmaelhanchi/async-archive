"""CLI argument handling and report output. Full runs are covered by the integration tests."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from archive_mirror import __version__
from archive_mirror.adapters.state_lock import exclusive_state
from archive_mirror.cli import EXIT_LOCKED, app
from archive_mirror.domain.models import DownloadReport, ExtractReport, MirrorSummary
from archive_mirror.domain.status import ExtractStatus, FileStatus
from archive_mirror.report import human_bytes, to_json

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_mirror_needs_a_url() -> None:
    result = runner.invoke(app, ["mirror", "--dry-run"])

    assert result.exit_code == 2
    assert "at least one URL" in result.output


def test_mirror_rejects_non_http_urls(tmp_path: Path) -> None:
    urls = tmp_path / "urls.txt"
    urls.write_text("# comment\nftp://archive.example/pub/\n", encoding="utf-8")

    result = runner.invoke(app, ["mirror", "--dry-run", "--urls-file", str(urls)])

    assert result.exit_code == 2
    assert "not an http(s) URL" in result.output


def test_a_second_process_on_the_same_state_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite"
    env = {"ARCHIVE_MIRROR_STATE_DB": str(state), "ARCHIVE_MIRROR_LOG_LEVEL": "ERROR"}

    with exclusive_state(state):
        result = runner.invoke(app, ["resume"], env=env)

    assert result.exit_code == EXIT_LOCKED
    assert "in use by another" in result.output


@pytest.mark.parametrize(
    ("count", "text"),
    [(0, "0 B"), (1023, "1023 B"), (1536, "1.50 KiB"), (5 * 1024**3, "5.00 GiB")],
)
def test_human_bytes(count: int, text: str) -> None:
    assert human_bytes(count) == text


def test_json_summary_uses_plain_keys() -> None:
    summary = MirrorSummary(
        run_id="r",
        crawl=None,
        downloads=DownloadReport(1, 0, 0, 0, 1, 10),
        extraction=ExtractReport(1, 0, 0, 3, 30),
        files_by_status=dict.fromkeys(FileStatus, 0) | {FileStatus.DONE: 1},
        bytes_done=10,
        archives_by_status=dict.fromkeys(ExtractStatus, 0) | {ExtractStatus.DONE: 1},
    )

    payload = json.loads(to_json(summary, 1.23456))

    assert payload["files_by_status"]["done"] == 1
    assert payload["downloads"]["bytes_received"] == 10
    assert payload["archives_by_status"]["done"] == 1
    assert payload["extraction"]["files_written"] == 3
    assert payload["elapsed_s"] == 1.235

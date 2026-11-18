"""CLI argument handling. Full runs are covered by the integration tests."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from archive_mirror import __version__
from archive_mirror.cli import app, human_bytes

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_mirror_without_dry_run_is_refused_for_now() -> None:
    result = runner.invoke(app, ["mirror", "https://archive.example/pub/"])

    assert result.exit_code == 2
    assert "--dry-run" in result.output


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


@pytest.mark.parametrize(
    ("count", "text"),
    [(0, "0 B"), (1023, "1023 B"), (1536, "1.50 KiB"), (5 * 1024**3, "5.00 GiB")],
)
def test_human_bytes(count: int, text: str) -> None:
    assert human_bytes(count) == text

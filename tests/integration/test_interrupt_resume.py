"""Ctrl+C mid-run, then resume: the M2 "done when" check, on the real CLI in a child process.

The signal is the one a person or ``docker stop`` sends: SIGINT on POSIX, Ctrl+Break on
Windows (a child in its own process group cannot be sent Ctrl+C there). After the first
signal the CLI must exit 130 on its own, leaving no row in ``downloading`` or ``extracting``
and the run marked ``interrupted``. A second, normal run then finishes the mirror, and every
file and every unpacked archive is checked byte for byte.
"""

import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from bench.archive_server import ServerConfig
from bench.tree import Archive, TreeSpec

from tests.integration.serving import archive, mismatches
from tests.integration.test_extraction import unpacked_mismatches

pytestmark = pytest.mark.integration

# 4 directories, 24 data files with sidecars, 8 of them real archives: ~5 MB at 1 MB/s each
SPEC = TreeSpec(
    depth=1,
    dirs_per_dir=3,
    files_per_dir=6,
    min_size=100_000,
    max_size=400_000,
    checksums=True,
    real_archives=True,
)
BODY_RATE = 1_000_000


def _env(work: Path) -> dict[str, str]:
    return os.environ | {
        "ARCHIVE_MIRROR_STATE_DB": str(work / "state.sqlite"),
        "ARCHIVE_MIRROR_MIRROR_ROOT": str(work / "mirror"),
        "ARCHIVE_MIRROR_EXTRACT_ROOT": str(work / "extracted"),
        "ARCHIVE_MIRROR_DOWNLOAD_WORKERS": "4",
        "ARCHIVE_MIRROR_MAX_IN_FLIGHT": "4",
        "ARCHIVE_MIRROR_PER_HOST_RATE": "1000",
        "ARCHIVE_MIRROR_PER_HOST_BURST": "50",
        "ARCHIVE_MIRROR_MIN_FREE_BYTES": "0",
        "ARCHIVE_MIRROR_LOG_LEVEL": "WARNING",
    }


async def _start(url: str, work: Path) -> asyncio.subprocess.Process:
    windows = sys.platform == "win32"
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "archive_mirror",
        "mirror",
        url,
        cwd=work,
        env=_env(work),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0,
    )


def _query(db: Path, sql: str) -> list[tuple[object, ...]]:
    try:
        with sqlite3.connect(db, timeout=5) as conn:
            return conn.execute(sql).fetchall()
    except sqlite3.OperationalError:  # not created yet
        return []


async def _wait_for_done_files(db: Path, count: int, within_s: float) -> None:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        rows = await asyncio.to_thread(
            _query, db, "SELECT COUNT(*) FROM files WHERE status = 'done'"
        )
        if rows and int(rows[0][0]) >= count:  # type: ignore[call-overload]
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"fewer than {count} files done after {within_s} s")


async def test_ctrl_c_mid_run_then_resume_finishes_cleanly(tmp_path: Path) -> None:
    expected = Archive(SPEC)
    async with archive(ServerConfig(tree=SPEC, body_rate=BODY_RATE)) as server:
        url = str(server.make_url("/"))
        first = await _start(url, tmp_path)
        await _wait_for_done_files(tmp_path / "state.sqlite", 4, within_s=60)
        first.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        _, stderr = await asyncio.wait_for(first.communicate(), timeout=60)

        db = tmp_path / "state.sqlite"
        statuses = dict(_query(db, "SELECT status, COUNT(*) FROM files GROUP BY status"))
        extracting = _query(db, "SELECT COUNT(*) FROM files WHERE extract_status = 'extracting'")
        outcome = _query(db, "SELECT outcome FROM runs")

        second = await _start(url, tmp_path)
        _, stderr_second = await asyncio.wait_for(second.communicate(), timeout=120)

    log = stderr.decode(errors="replace")
    assert first.returncode == 130, log
    assert "shutdown_requested" in log
    assert "downloading" not in statuses, statuses  # rolled back, not left for recovery
    assert extracting == [(0,)]
    assert outcome == [("interrupted",)]
    assert statuses.get("done", 0) < len(expected.files)  # it really stopped early

    assert second.returncode == 0, stderr_second.decode(errors="replace")
    assert "recovered_after_interruption" not in stderr_second.decode(errors="replace")
    assert mismatches(tmp_path / "mirror", url, expected) == []
    assert unpacked_mismatches(tmp_path / "extracted", url, expected) == []

"""M1's proof: kill the real CLI at random moments 20 times over, resume, compare every byte.

The harness lives in ``bench/kill_resume.py`` so the same check runs from the command line
(``make kill-resume``) with a printed report.
"""

from pathlib import Path

import pytest
from bench.archive_server import RANGE_HITS, ServerConfig
from bench.kill_resume import BODY_RATE, SPEC, quiet_server_logs, run_trials
from bench.tree import Archive

from tests.integration.serving import archive

pytestmark = [pytest.mark.integration, pytest.mark.slow]

TRIALS = 20


async def test_twenty_kills_and_resumes_leave_byte_identical_files(tmp_path: Path) -> None:
    quiet_server_logs()
    async with archive(ServerConfig(tree=SPEC, body_rate=BODY_RATE)) as server:
        url = str(server.make_url("/"))
        results = await run_trials(
            url, tmp_path, Archive(SPEC), server.app[RANGE_HITS], trials=TRIALS, seed=2024
        )

    failed = {t.number: t.problems[:3] for t in results if t.problems}
    assert failed == {}, f"{len(failed)}/{TRIALS} trials failed: {failed}"
    assert len(results) == TRIALS
    # the kills must have hit transfers in flight, or this proves nothing about resuming
    assert sum(t.parts_left for t in results) > 0
    assert sum(t.range_requests for t in results) > 0

"""End to end extraction: real archives from the local server, unpacked in worker processes."""

from pathlib import Path

import pytest
from bench.archive_server import ServerConfig
from bench.tree import Archive, TreeSpec

from archive_mirror.composition import run_mirror
from archive_mirror.domain.status import ExtractStatus
from archive_mirror.domain.urls import local_relative_path
from tests.integration.serving import SettingsFactory, archive, mismatches

pytestmark = pytest.mark.integration

# 3 directories, 6 files each; every third file is a real .tar.gz: 6 archives
SPEC = TreeSpec(depth=1, dirs_per_dir=2, files_per_dir=6, max_size=150_000, real_archives=True)
ARCHIVE = Archive(SPEC)


def unpacked_mismatches(root: Path, base_url: str, expected: Archive) -> list[str]:
    """Archives whose unpacked directory is missing or differs from the members served."""
    problems: list[str] = []
    for path in expected.real_archives:
        target = root / local_relative_path(base_url.rstrip("/") + path)
        found = (
            {
                p.relative_to(target).as_posix(): p.read_bytes()
                for p in target.rglob("*")
                if p.is_file()
            }
            if target.is_dir()
            else None
        )
        if found != expected.members(path):
            problems.append(f"unpacked differs {path}")
    problems += [f"leftover {p.name}" for p in root.rglob("*.extracting")]
    return problems


async def test_archives_are_unpacked_as_they_arrive_and_not_again(
    settings: SettingsFactory,
) -> None:
    config = settings(extract_workers=2)
    async with archive(ServerConfig(tree=SPEC)) as server:
        url = str(server.make_url("/"))
        first = await run_mirror(config, [url], "run-1")
        second = await run_mirror(config, [url], "run-2")

    assert len(ARCHIVE.real_archives) == 6
    assert mismatches(config.mirror_root, url, ARCHIVE) == []
    assert unpacked_mismatches(config.extract_root, url, ARCHIVE) == []
    assert first.extraction is not None
    assert first.extraction.archives_done == 6
    assert first.extraction.files_written == 12
    assert second.extraction is not None
    assert second.extraction.archives_done == 0
    assert second.archives_by_status[ExtractStatus.DONE] == 6


async def test_archives_left_by_a_run_without_extraction_are_unpacked_later(
    settings: SettingsFactory,
) -> None:
    async with archive(ServerConfig(tree=SPEC)) as server:
        url = str(server.make_url("/"))
        off = await run_mirror(settings(extract=False), [url], "run-1")
        on = await run_mirror(settings(), [], "run-2")  # resume: nothing left to download

    assert off.extraction is None
    assert off.archives_by_status[ExtractStatus.PENDING] == 6
    assert on.downloads.files_done == 0
    assert on.extraction is not None
    assert on.extraction.archives_done == 6
    assert unpacked_mismatches(settings().extract_root, url, ARCHIVE) == []

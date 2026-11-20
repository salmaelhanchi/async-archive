"""The benchmark's own parts: the sequential baseline must mirror correctly, the chart must
be valid SVG, and the memory reading must work on this OS. A benchmark built on a broken
baseline would prove nothing."""

import asyncio
import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from bench import v1_sequential
from bench.archive_server import ServerConfig
from bench.benchmark import CONFIGS
from bench.chart import render
from bench.measure import peak_rss_bytes
from bench.tree import Archive, TreeSpec

from tests.integration.serving import archive

pytestmark = pytest.mark.integration

SPEC = TreeSpec(depth=1, dirs_per_dir=2, files_per_dir=3, max_size=50_000)


async def test_the_baseline_mirrors_an_iis_tree_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(v1_sequential, "LISTING_DELAY_S", 0)
    monkeypatch.setattr(v1_sequential, "FILE_DELAY_S", 0)
    expected = Archive(SPEC)
    async with archive(ServerConfig(style="iis", tree=SPEC)) as server:
        url = str(server.make_url("/"))
        mirror = v1_sequential.SequentialMirror(tmp_path / "out")
        done, received = await asyncio.to_thread(mirror.run, [url])

    assert (done, received) == (len(expected.files), expected.total_bytes)
    for path in expected.files:
        local = mirror.local_path(url.rstrip("/") + path)
        assert hashlib.sha256(local.read_bytes()).hexdigest() == expected.digest(path)
    assert (tmp_path / "out" / "files_metadata.csv").is_file()


def test_the_chart_is_valid_svg_with_one_bar_per_capped_run() -> None:
    capped = [c for c in CONFIGS if c.rate is not None]
    results = {
        "setup": {
            "files": 40,
            "bytes": 38_000_000,
            "latency_ms": 20,
            "body_rate_bytes_per_s": 1_000_000,
            "cap_requests_per_s": 2.0,
            "rounds": 3,
        },
        "configs": [{"label": c.label, "kind": c.kind, "rate": c.rate} for c in CONFIGS],
        "median": {
            c.label: {"seconds": 60.0 / (i + 1), "files_per_s": 40 / (60.0 / (i + 1))}
            for i, c in enumerate(CONFIGS)
        },
    }

    svg = ET.fromstring(render(results))  # noqa: S314 - our own output, not untrusted input

    paths = svg.findall("{http://www.w3.org/2000/svg}path")
    assert len(paths) == len(capped)
    assert {p.get("class") for p in paths} == {"v1", "v2"}


def test_peak_memory_can_be_read_on_this_os() -> None:
    assert 10 * 2**20 < peak_rss_bytes() < 64 * 2**30

"""Lister service with fake fetcher, policy and writer."""

import asyncio
from collections.abc import Sequence

import pytest
from bench.archive_server import render_nginx
from bench.tree import TreeSpec, build_tree, expected_files

from archive_mirror.adapters.listing import parse_listing
from archive_mirror.domain.errors import FetchFailed
from archive_mirror.domain.models import FetchedPage, PendingFile
from archive_mirror.domain.status import ListingStatus
from archive_mirror.services.lister import CrawlLimits, Lister, OnRecorded
from tests.fakes import AllowPolicy, FakeFetcher, MemoryWriter, html_page

ROOT = "https://archive.example/"
SPEC = TreeSpec(depth=2, dirs_per_dir=2, files_per_dir=3)  # 7 directories, 21 files
TREE = build_tree(SPEC)


def site(tree: dict = TREE) -> dict[str, FetchedPage | FetchFailed]:
    return {
        ROOT + path.lstrip("/"): html_page(ROOT + path.lstrip("/"), render_nginx(path, kids))
        for path, kids in tree.items()
    }


def make_lister(
    fetcher: FakeFetcher,
    writer: MemoryWriter,
    *,
    policy: AllowPolicy | None = None,
    workers: int = 3,
    max_depth: int = 10,
    max_pages: int = 1000,
    queue: int = 8,
    on_recorded: OnRecorded | None = None,
) -> Lister:
    return Lister(
        fetcher=fetcher,
        parse=parse_listing,
        policy=policy or AllowPolicy(),
        writer=writer,
        limits=CrawlLimits(
            workers=workers,
            max_depth=max_depth,
            max_listing_pages=max_pages,
            write_queue_size=queue,
        ),
        on_recorded=on_recorded,
    )


async def test_crawl_lists_every_directory_and_records_every_file_once() -> None:
    fetcher, writer = FakeFetcher(site()), MemoryWriter()

    stats = await make_lister(fetcher, writer).crawl([ROOT, ROOT])

    expected = expected_files(TREE)
    assert sorted(f.url for f in writer.files) == sorted(ROOT + p.lstrip("/") for p in expected)
    assert {f.url: f.size_bytes for f in writer.files} == {
        ROOT + p.lstrip("/"): s for p, s in expected.items()
    }
    assert set(fetcher.calls.values()) == {1}
    assert stats.listings[ListingStatus.LISTED] == len(TREE) == 7
    assert stats.files == 21


async def test_files_carry_provenance() -> None:
    writer = MemoryWriter()

    await make_lister(FakeFetcher(site()), writer).crawl([ROOT])

    nested = next(f for f in writer.files if "/dir-1/dir-0/" in f.url)
    assert nested.listing_url == ROOT + "dir-1/dir-0/"
    assert nested.listing_format == "nginx"
    assert nested.relative_path.startswith("archive.example/dir-1/dir-0/")
    depths = {o.url: (o.depth, o.parent_url) for o in writer.outcomes}
    assert depths[ROOT + "dir-1/dir-0/"] == (2, ROOT + "dir-1/")


async def test_committed_files_go_to_the_download_queue_with_their_checksum_files() -> None:
    tree = build_tree(TreeSpec(depth=1, dirs_per_dir=1, files_per_dir=3, checksums=True))
    writer = MemoryWriter()
    handed_on: list[PendingFile] = []

    async def on_recorded(pending: Sequence[PendingFile]) -> None:
        assert all(p.file in writer.files for p in pending)  # durable before handed on
        handed_on.extend(pending)

    await make_lister(FakeFetcher(site(tree)), writer, on_recorded=on_recorded).crawl([ROOT])

    assert len(handed_on) == len(writer.files) == 12
    data = next(p.file for p in handed_on if p.file.name == "data-0.tar.gz")
    sidecar = next(p.file for p in handed_on if p.file.name == "data-0.tar.gz.sha256")
    assert data.checksum is not None
    assert (data.checksum.url, data.checksum.algorithm) == (sidecar.url, "sha256")
    assert sidecar.checksum is None


async def test_directories_below_max_depth_are_recorded_as_skipped() -> None:
    fetcher, writer = FakeFetcher(site()), MemoryWriter()

    await make_lister(fetcher, writer, max_depth=1).crawl([ROOT])

    deep = ROOT + "dir-0/dir-1/"
    assert writer.by_url()[deep].status is ListingStatus.SKIPPED
    assert deep not in fetcher.calls


async def test_listing_budget_is_checked_before_each_request() -> None:
    fetcher, writer = FakeFetcher(site()), MemoryWriter()

    stats = await make_lister(fetcher, writer, max_pages=2).crawl([ROOT])

    assert sum(fetcher.calls.values()) == 2
    assert stats.listings[ListingStatus.LISTED] == 2
    assert stats.listings[ListingStatus.SKIPPED] >= 1


async def test_failures_are_recorded_and_the_crawl_continues() -> None:
    pages = site()
    pages[ROOT + "dir-0/"] = FetchFailed(ROOT + "dir-0/", "ReadTimeout", 5)
    pages[ROOT + "dir-1/"] = html_page(ROOT + "dir-1/", "gone", status=410)
    pages[ROOT + "dir-1/dir-0/"] = html_page(ROOT + "dir-1/dir-0/", "<p>hello</p>")
    writer = MemoryWriter()

    await make_lister(FakeFetcher(pages), writer).crawl([ROOT])

    outcomes = writer.by_url()
    assert (outcomes[ROOT + "dir-0/"].detail, outcomes[ROOT + "dir-0/"].attempts) == (
        "ReadTimeout",
        5,
    )
    assert outcomes[ROOT + "dir-1/"].detail == "HTTP 410"
    assert outcomes[ROOT].status is ListingStatus.LISTED
    # dir-1/ failed, so its children were never discovered
    assert ROOT + "dir-1/dir-0/" not in outcomes


async def test_non_html_and_unrecognized_pages_fail() -> None:
    pages = site()
    pages[ROOT + "dir-0/"] = FetchedPage(ROOT + "dir-0/", 200, "{}", "application/json", 1)
    pages[ROOT + "dir-1/"] = html_page(ROOT + "dir-1/", "<html><p>nope</p></html>")
    writer = MemoryWriter()

    await make_lister(FakeFetcher(pages), writer).crawl([ROOT])

    outcomes = writer.by_url()
    assert outcomes[ROOT + "dir-0/"].detail == "not HTML (application/json)"
    assert "not an Apache, nginx or IIS listing" in (outcomes[ROOT + "dir-1/"].detail or "")


async def test_robots_disallowed_directories_and_files_are_left_out() -> None:
    fetcher, writer = FakeFetcher(site()), MemoryWriter()
    policy = AllowPolicy(denied_prefixes=[ROOT + "dir-0/", ROOT + "notes"])

    await make_lister(fetcher, writer, policy=policy).crawl([ROOT])

    assert writer.by_url()[ROOT + "dir-0/"].detail == "disallowed by robots.txt"
    assert ROOT + "dir-0/" not in fetcher.calls
    assert not any(f.url.startswith((ROOT + "dir-0/", ROOT + "notes")) for f in writer.files)


async def test_redirect_to_an_already_listed_directory_is_skipped() -> None:
    pages = site()
    alias = ROOT + "alias/"
    pages[alias] = html_page(ROOT + "dir-0/", "irrelevant")  # final URL after redirect
    root_html = render_nginx("/", TREE["/"]).replace(
        "</pre>", '<a href="alias/">alias/</a>\n</pre>'
    )
    pages[ROOT] = html_page(ROOT, root_html)
    writer = MemoryWriter()

    await make_lister(FakeFetcher(pages), writer, workers=1).crawl([ROOT])

    assert writer.by_url()[alias].status is ListingStatus.SKIPPED
    assert writer.by_url()[ROOT + "dir-0/"].status is ListingStatus.LISTED


async def test_slow_writer_applies_backpressure_to_listers() -> None:
    """With a write queue of 1, listers can run at most a few listings ahead of the DB."""
    writer = MemoryWriter(delay=0.01)
    workers, queue = 2, 1
    ahead: list[int] = []

    class WatchingFetcher(FakeFetcher):
        async def fetch(self, url: str) -> FetchedPage:
            ahead.append(sum(self.calls.values()) - len(writer.outcomes))
            return await super().fetch(url)

    await make_lister(WatchingFetcher(site()), writer, workers=workers, queue=queue).crawl([ROOT])

    assert len(writer.outcomes) == len(TREE)
    assert max(ahead) <= workers + queue + 1


async def test_a_writer_error_cancels_the_listers_and_surfaces() -> None:
    fetcher = FakeFetcher(site(), delay=0.001)
    writer = MemoryWriter(fail_on=ROOT + "dir-0/")

    with pytest.raises(ExceptionGroup) as info:
        await asyncio.wait_for(make_lister(fetcher, writer).crawl([ROOT]), timeout=5)

    assert info.group_contains(RuntimeError, match="disk full")

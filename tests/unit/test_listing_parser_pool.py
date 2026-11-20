"""Small pages are parsed on the loop, large ones in the pool (a counting thread pool here)."""

from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import pytest
from bench.archive_server import render_nginx
from bench.tree import TreeSpec, build_tree

from archive_mirror.adapters.listing import parse_listing
from archive_mirror.adapters.listing_parser import PooledListingParser
from archive_mirror.domain.errors import UnrecognizedListing

URL = "https://archive.example/"
TREE = build_tree(TreeSpec(depth=0, files_per_dir=40))
PAGE = render_nginx("/", TREE["/"])


class CountingPool(ThreadPoolExecutor):
    def __init__(self) -> None:
        super().__init__(max_workers=1)
        self.submitted = 0

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        self.submitted += 1
        return super().submit(fn, *args, **kwargs)


@pytest.fixture
def pool() -> Iterator[CountingPool]:
    with CountingPool() as executor:
        yield executor


@pytest.mark.parametrize(("threshold", "in_pool"), [(len(PAGE) + 1, 0), (len(PAGE), 1), (0, 1)])
async def test_the_page_size_decides_where_it_is_parsed(
    pool: CountingPool, threshold: int, in_pool: int
) -> None:
    parse = PooledListingParser(pool, forced=None, min_pool_bytes=threshold)

    listing = await parse(PAGE, URL)

    assert listing == parse_listing(PAGE, URL)
    assert (pool.submitted, parse.pages_in_pool) == (in_pool, in_pool)


async def test_an_unrecognized_page_fails_the_same_way_in_the_pool(pool: CountingPool) -> None:
    parse = PooledListingParser(pool, forced=None, min_pool_bytes=0)

    with pytest.raises(UnrecognizedListing):
        await parse("<html><body>" + "<p>no listing here</p>" * 500 + "</body></html>", URL)

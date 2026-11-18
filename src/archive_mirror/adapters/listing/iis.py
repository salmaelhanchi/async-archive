"""IIS directory browsing listings.

A ``<pre>`` block where each line is ``<date> <time>  <size or <dir>> <A HREF="...">``, lines
separated by ``<br>``. Metadata comes *before* the link, hrefs are usually absolute paths, and
the date format follows the server locale, so several are tried.
"""

import re

from bs4 import BeautifulSoup, Tag

from archive_mirror.adapters.listing._common import (
    build_listing,
    has_marker,
    href_of,
    make_entry,
    text_before,
)
from archive_mirror.domain.models import Listing, ListingEntry, ListingFormat
from archive_mirror.domain.urls import as_directory_url

FORMAT = ListingFormat.IIS

_META = re.compile(r"(?P<date>\S.*?)\s+(?P<size><dir>|[\d,]+)\s*$", re.IGNORECASE)
_DATE_FORMATS = (
    "%m/%d/%Y %I:%M %p",
    "%A, %B %d, %Y %I:%M %p",
    "%d/%m/%Y %H:%M",
    "%d.%m.%Y %H:%M",
    "%Y-%m-%d %H:%M",
)
_MARKERS = re.compile(
    r"\[To Parent Directory\]|&lt;dir&gt;\s*<A HREF=|(?:AM|PM)\s+[\d,]+\s*<A HREF=",
    re.IGNORECASE,
)


def detect(html: str) -> bool:
    return has_marker(html, _MARKERS)


def parse(html: str, listing_url: str) -> Listing:
    directory_url = as_directory_url(listing_url)
    soup = BeautifulSoup(html, "html.parser")
    pre = soup.find("pre")
    anchors = pre.find_all("a", href=True) if isinstance(pre, Tag) else []
    entries: list[ListingEntry | None] = []
    for anchor in anchors:
        match = _META.search(text_before(anchor))
        size_text = match["size"] if match else None
        is_dir = size_text is not None and size_text.lower() == "<dir>"
        entries.append(
            make_entry(
                directory_url,
                href_of(anchor),
                is_directory=is_dir,
                date_text=match["date"] if match else None,
                size_text=None if is_dir else size_text,
                date_formats=_DATE_FORMATS,
            )
        )
    return build_listing(directory_url, FORMAT, entries)

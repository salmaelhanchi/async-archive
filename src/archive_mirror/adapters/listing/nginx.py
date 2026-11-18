"""nginx ``autoindex`` listings.

A ``<pre>`` block with ``../`` first, then one line per entry: link, ``dd-Mon-yyyy HH:MM``,
and the size in bytes (or rounded with ``autoindex_exact_size off``). Long names are cut in
the link text (``name..>``), so names come from the ``href``.
"""

import re

from bs4 import BeautifulSoup, Tag

from archive_mirror.adapters.listing._common import (
    build_listing,
    has_marker,
    href_of,
    make_entry,
    text_after,
)
from archive_mirror.domain.models import Listing, ListingEntry, ListingFormat
from archive_mirror.domain.urls import as_directory_url

FORMAT = ListingFormat.NGINX

_META = re.compile(
    r"(?P<date>\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2})\s+(?P<size>-|\d+(?:\.\d+)?[KMGTP]?)\s*$"
)
_DATE_FORMATS = ("%d-%b-%Y %H:%M",)
_MARKERS = re.compile(
    r'<pre>\s*<a href="\.\./">\.\./</a>'
    r"|</a>\s+\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2}\s+(?:-|\d+[KMG]?)\s*$",
    re.IGNORECASE | re.MULTILINE,
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
        match = _META.search(text_after(anchor))
        size_text = match["size"] if match else None
        entries.append(
            make_entry(
                directory_url,
                href_of(anchor),
                is_directory=size_text == "-",
                date_text=match["date"] if match else None,
                size_text=size_text,
                date_formats=_DATE_FORMATS,
            )
        )
    return build_listing(directory_url, FORMAT, entries)

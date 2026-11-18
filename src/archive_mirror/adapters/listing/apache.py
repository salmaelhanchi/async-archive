"""Apache ``mod_autoindex`` listings, in all three layouts.

* ``HTMLTable`` (the Debian and Ubuntu default): one ``<tr>`` per entry.
* ``FancyIndexing`` without tables: a ``<pre>`` block, metadata after each link.
* Plain: a ``<ul>`` of links with no size or date.

Sizes are rounded (``1.2K``) unless the server was configured otherwise.
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

FORMAT = ListingFormat.APACHE

_META = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?|\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2})"
    r"\s+(?P<size>-|\d+(?:\.\d+)?[KMGTP]?)(?=\s|$)"
)
_DATE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M")
_MARKERS = re.compile(
    r'href="\?C=[NMSD](?:;|&amp;)O=[AD]"|>\s*Parent Directory\s*<|<address>Apache',
    re.IGNORECASE,
)


def detect(html: str) -> bool:
    return has_marker(html, _MARKERS)


def _entry(directory_url: str, anchor: Tag, meta_text: str, *, is_dir: bool) -> ListingEntry | None:
    match = _META.search(meta_text)
    return make_entry(
        directory_url,
        href_of(anchor),
        is_directory=is_dir,
        date_text=match["date"] if match else None,
        size_text=match["size"] if match else None,
        date_formats=_DATE_FORMATS,
    )


def _is_dir_icon(tag: Tag | None) -> bool:
    if tag is None:
        return False
    icon = tag.find("img")
    return isinstance(icon, Tag) and icon.get("alt") == "[DIR]"


def _from_table(directory_url: str, table: Tag) -> list[ListingEntry | None]:
    entries: list[ListingEntry | None] = []
    for row in table.find_all("tr"):
        anchor = row.find("a", href=True)
        if not isinstance(anchor, Tag):
            continue
        cells = row.find_all("td")
        name_cell = anchor.find_parent("td")
        # Identity, not ==: bs4 compares tags by content, and two empty cells are "equal".
        position = next((i for i, cell in enumerate(cells) if cell is name_cell), -1)
        after = cells[position + 1 :]
        meta = " ".join(cell.get_text(" ", strip=True) for cell in after)
        entries.append(_entry(directory_url, anchor, meta, is_dir=_is_dir_icon(row)))
    return entries


def _from_pre(directory_url: str, pre: Tag) -> list[ListingEntry | None]:
    entries: list[ListingEntry | None] = []
    for anchor in pre.find_all("a", href=True):
        # Only the tag right before the link belongs to this line; searching further back
        # would pick up the previous line's icon.
        before = anchor.find_previous_sibling()
        if isinstance(before, Tag) and before.name == "img":
            is_dir = before.get("alt") == "[DIR]"
        else:
            is_dir = (
                _is_dir_icon(before) if isinstance(before, Tag) and before.name == "a" else False
            )
        entries.append(_entry(directory_url, anchor, text_after(anchor), is_dir=is_dir))
    return entries


def parse(html: str, listing_url: str) -> Listing:
    directory_url = as_directory_url(listing_url)
    soup = BeautifulSoup(html, "html.parser")
    table, pre = soup.find("table"), soup.find("pre")
    if isinstance(table, Tag) and table.find("a", href=True):
        entries = _from_table(directory_url, table)
    elif isinstance(pre, Tag):
        entries = _from_pre(directory_url, pre)
    else:
        entries = [
            _entry(directory_url, anchor, "", is_dir=False)
            for anchor in soup.find_all("a", href=True)
        ]
    return build_listing(directory_url, FORMAT, entries)

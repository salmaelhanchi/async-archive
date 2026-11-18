"""Helpers shared by the listing parsers: link resolution, scope, metadata, dedupe."""

import re
from collections.abc import Iterable
from datetime import datetime
from urllib.parse import unquote, urljoin, urlsplit

from bs4.element import NavigableString, Tag

from archive_mirror.domain.models import EntryKind, Listing, ListingEntry, ListingFormat
from archive_mirror.domain.sizes import UNKNOWN_SIZE, parse_size
from archive_mirror.domain.urls import as_directory_url, normalize_url

_SKIPPED_SCHEMES = ("mailto:", "javascript:", "data:", "ftp:")


def resolve_child(directory_url: str, href: str | None) -> str | None:
    """Absolute canonical URL of a direct child of ``directory_url``, or ``None``.

    Drops sort links (``?C=N;O=D``), fragments, parent links, other hosts and anything deeper
    than one level, so a listing can only ever add its own children to the crawl.
    """
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("?", "#")) or href.lower().startswith(_SKIPPED_SCHEMES):
        return None
    try:
        url = normalize_url(urljoin(directory_url, href))
    except ValueError:
        return None
    child, parent = urlsplit(url), urlsplit(directory_url)
    if child.query or (child.scheme, child.netloc) != (parent.scheme, parent.netloc):
        return None
    if not child.path.startswith(parent.path):
        return None
    remainder = child.path[len(parent.path) :].rstrip("/")
    if not remainder or "/" in remainder:
        return None
    return url


def entry_name(url: str) -> str:
    """Decoded last path segment. Link text is not used: some servers truncate it."""
    return unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])


def parse_datetime(text: str | None, formats: Iterable[str]) -> datetime | None:
    if not text:
        return None
    cleaned = " ".join(text.split())
    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def make_entry(
    directory_url: str,
    href: str | None,
    *,
    is_directory: bool,
    date_text: str | None,
    size_text: str | None,
    date_formats: Iterable[str],
) -> ListingEntry | None:
    url = resolve_child(directory_url, href)
    if url is None:
        return None
    if is_directory or url.endswith("/"):
        return ListingEntry(
            url=as_directory_url(url),
            name=entry_name(url),
            kind=EntryKind.DIRECTORY,
            size_bytes=None,
            size_exact=False,
            modified=parse_datetime(date_text, date_formats),
        )
    size = parse_size(size_text) if size_text else UNKNOWN_SIZE
    return ListingEntry(
        url=url,
        name=entry_name(url),
        kind=EntryKind.FILE,
        size_bytes=size.size_bytes,
        size_exact=size.exact,
        modified=parse_datetime(date_text, date_formats),
    )


def build_listing(
    directory_url: str, fmt: ListingFormat, entries: Iterable[ListingEntry | None]
) -> Listing:
    """Drop ``None``s and duplicate URLs, keeping the copy that carries the most metadata."""
    best: dict[str, ListingEntry] = {}
    for entry in entries:
        if entry is None:
            continue
        current = best.get(entry.url)
        if current is None or _richness(entry) > _richness(current):
            best[entry.url] = entry
    return Listing(url=directory_url, format=fmt, entries=tuple(best.values()))


def _richness(entry: ListingEntry) -> int:
    return (entry.size_bytes is not None) + (entry.modified is not None)


def text_after(tag: Tag) -> str:
    """Text on the same line right after ``tag`` (Apache and nginx put metadata there)."""
    sibling = tag.next_sibling
    if isinstance(sibling, NavigableString):
        return str(sibling).split("\n", 1)[0]
    return ""


def text_before(tag: Tag) -> str:
    """Text on the same line right before ``tag`` (IIS puts metadata there)."""
    sibling = tag.previous_sibling
    if isinstance(sibling, NavigableString):
        return str(sibling).rsplit("\n", 1)[-1]
    return ""


def href_of(tag: Tag) -> str | None:
    value = tag.get("href")
    return value if isinstance(value, str) else None


def has_marker(text: str, pattern: re.Pattern[str]) -> bool:
    return pattern.search(text) is not None

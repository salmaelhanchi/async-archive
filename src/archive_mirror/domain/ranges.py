"""HTTP byte ranges (RFC 9110 section 14): the request header and the ``Content-Range`` reply."""

import re
from dataclasses import dataclass

_CONTENT_RANGE = re.compile(
    r"^bytes\s+(?:(?P<start>\d+)-(?P<end>\d+)|\*)/(?P<total>\d+|\*)$", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class ContentRange:
    """``bytes 100-199/1000`` or, on a 416, ``bytes */1000`` (``start`` and ``end`` None)."""

    start: int | None
    end: int | None
    total: int | None


def range_header(offset: int) -> str:
    """Ask for everything from ``offset`` to the end."""
    if offset < 1:
        raise ValueError("a range request needs a positive offset")
    return f"bytes={offset}-"


def parse_content_range(value: str | None) -> ContentRange | None:
    """Parse a ``Content-Range`` header. Returns ``None`` when absent or malformed."""
    if not value:
        return None
    match = _CONTENT_RANGE.match(value.strip())
    if not match:
        return None
    total = None if match["total"] == "*" else int(match["total"])
    if match["start"] is None:
        return ContentRange(None, None, total)
    start, end = int(match["start"]), int(match["end"])
    if end < start or (total is not None and end >= total):
        return None
    return ContentRange(start, end, total)


def if_range_value(etag: str | None, last_modified: str | None) -> str | None:
    """Validator for ``If-Range``. Weak ETags are not allowed there (RFC 9110 13.1.5)."""
    if etag and not etag.startswith("W/"):
        return etag
    return last_modified or None

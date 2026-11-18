"""Parse the size column of a directory listing.

Servers print sizes either as exact byte counts (``1234567`` or ``1,234,567``) or rounded
(``1.2K``, ``45M``). Rounded sizes are kept but flagged as approximate, because they cannot
be used to verify a download.
"""

import re
from dataclasses import dataclass

_EXACT = re.compile(r"^\d{1,3}(?:,\d{3})+$|^\d+$")
_HUMAN = re.compile(r"^(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>[KMGTP])i?B?$", re.IGNORECASE)
_UNITS = {"K": 1, "M": 2, "G": 3, "T": 4, "P": 5}


@dataclass(frozen=True, slots=True)
class ParsedSize:
    size_bytes: int | None
    exact: bool


UNKNOWN_SIZE = ParsedSize(size_bytes=None, exact=False)


def parse_size(text: str) -> ParsedSize:
    """Return the size in bytes, or ``UNKNOWN_SIZE`` for ``-`` and anything unreadable."""
    value = text.strip()
    if _EXACT.match(value):
        return ParsedSize(size_bytes=int(value.replace(",", "")), exact=True)
    match = _HUMAN.match(value)
    if match:
        power = _UNITS[match["unit"].upper()]
        return ParsedSize(size_bytes=round(float(match["num"]) * 1024**power), exact=False)
    return UNKNOWN_SIZE

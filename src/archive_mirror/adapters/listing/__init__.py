"""Directory listing parsers. ``parse_listing`` detects the format unless one is forced."""

from collections.abc import Callable
from dataclasses import dataclass

from archive_mirror.adapters.listing import apache, iis, nginx
from archive_mirror.domain.errors import UnrecognizedListing
from archive_mirror.domain.models import Listing, ListingFormat


@dataclass(frozen=True, slots=True)
class ListingParser:
    format: ListingFormat
    detect: Callable[[str], bool]
    parse: Callable[[str, str], Listing]


# Order matters: IIS and Apache have unambiguous markers; nginx is the most generic.
PARSERS: tuple[ListingParser, ...] = (
    ListingParser(iis.FORMAT, iis.detect, iis.parse),
    ListingParser(apache.FORMAT, apache.detect, apache.parse),
    ListingParser(nginx.FORMAT, nginx.detect, nginx.parse),
)
_BY_FORMAT = {p.format: p for p in PARSERS}


def detect_format(html: str) -> ListingFormat | None:
    for parser in PARSERS:
        if parser.detect(html):
            return parser.format
    return None


def parse_listing(html: str, listing_url: str, forced: ListingFormat | None = None) -> Listing:
    """Parse ``html`` served at ``listing_url``. Raises ``UnrecognizedListing``."""
    fmt = forced or detect_format(html)
    if fmt is None:
        raise UnrecognizedListing(f"{listing_url}: not an Apache, nginx or IIS listing")
    return _BY_FORMAT[fmt].parse(html, listing_url)


__all__ = ["PARSERS", "ListingParser", "detect_format", "parse_listing"]

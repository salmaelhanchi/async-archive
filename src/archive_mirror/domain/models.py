"""Value types shared by every layer."""

import enum
from dataclasses import dataclass
from datetime import datetime

from archive_mirror.domain.status import ListingStatus
from archive_mirror.domain.urls import file_id, local_relative_path

ARCHIVE_SUFFIXES = (
    ".zip",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
    ".gz",
    ".bz2",
    ".xz",
    ".7z",
    ".rar",
)


class EntryKind(enum.StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


class ListingFormat(enum.StrEnum):
    APACHE = "apache"
    NGINX = "nginx"
    IIS = "iis"


@dataclass(frozen=True, slots=True)
class ListingEntry:
    """One row of a directory listing."""

    url: str
    name: str
    kind: EntryKind
    size_bytes: int | None
    size_exact: bool
    modified: datetime | None


@dataclass(frozen=True, slots=True)
class Listing:
    """A parsed directory listing page."""

    url: str
    format: ListingFormat
    entries: tuple[ListingEntry, ...]

    @property
    def directories(self) -> tuple[ListingEntry, ...]:
        return tuple(e for e in self.entries if e.kind is EntryKind.DIRECTORY)

    @property
    def files(self) -> tuple[ListingEntry, ...]:
        return tuple(e for e in self.entries if e.kind is EntryKind.FILE)


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """A response body read as text, with the final URL after redirects."""

    url: str
    status: int
    text: str
    content_type: str
    attempts: int


def is_archive(name: str) -> bool:
    return name.lower().endswith(ARCHIVE_SUFFIXES)


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """A file found in a listing, with where it came from and where it will be stored."""

    file_id: str
    url: str
    relative_path: str
    name: str
    size_bytes: int | None
    size_exact: bool
    modified: datetime | None
    is_archive: bool
    listing_url: str
    listing_format: ListingFormat

    @classmethod
    def from_entry(cls, entry: ListingEntry, listing: Listing) -> "RemoteFile":
        return cls(
            file_id=file_id(entry.url),
            url=entry.url,
            relative_path=local_relative_path(entry.url),
            name=entry.name,
            size_bytes=entry.size_bytes,
            size_exact=entry.size_exact,
            modified=entry.modified,
            is_archive=is_archive(entry.name),
            listing_url=listing.url,
            listing_format=listing.format,
        )


@dataclass(frozen=True, slots=True)
class ListingOutcome:
    """What happened to one directory URL during a run."""

    url: str
    depth: int
    parent_url: str | None
    status: ListingStatus
    format: ListingFormat | None
    entries: int
    attempts: int
    detail: str | None


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Totals for one run, read back from the state database after commit."""

    run_id: str
    files: int
    files_size_unknown: int
    files_size_approximate: int
    bytes_listed: int
    archives: int
    files_already_done: int
    listings: dict[ListingStatus, int]
    listings_by_format: dict[str, int]

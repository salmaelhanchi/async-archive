"""Rules for unpacking downloaded archives. Pure: no I/O.

An archive is untrusted input. Member names can try to climb out of the target directory
(``../../etc/passwd``, ``C:\\Windows``, ``/abs``), and sizes can be tiny on the wire and huge
on disk. These rules decide what may be written; the adapter enforces them while it writes.
"""

import enum
from dataclasses import dataclass

from archive_mirror.domain.urls import safe_segment

_TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
_STREAM_SUFFIXES = (".gz", ".bz2", ".xz")


class ArchiveKind(enum.StrEnum):
    ZIP = "zip"
    TAR = "tar"  # plain or compressed with gzip, bzip2 or xz
    STREAM = "stream"  # one compressed file: .gz, .bz2 or .xz without tar inside
    SEVEN_ZIP = "7z"
    RAR = "rar"  # recognised, not extracted: no free decoder that needs no external tool


SUPPORTED_KINDS = frozenset(
    {ArchiveKind.ZIP, ArchiveKind.TAR, ArchiveKind.STREAM, ArchiveKind.SEVEN_ZIP}
)


def archive_kind(name: str) -> ArchiveKind | None:
    lower = name.lower()
    if lower.endswith(_TAR_SUFFIXES):
        return ArchiveKind.TAR
    if lower.endswith(".zip"):
        return ArchiveKind.ZIP
    if lower.endswith(".7z"):
        return ArchiveKind.SEVEN_ZIP
    if lower.endswith(".rar"):
        return ArchiveKind.RAR
    if lower.endswith(_STREAM_SUFFIXES):
        return ArchiveKind.STREAM
    return None


def stream_member_name(archive_name: str) -> str:
    """The single file inside ``notes.txt.gz`` is ``notes.txt``."""
    stem = archive_name.rsplit(".", 1)[0] if "." in archive_name else archive_name
    return safe_segment(stem) if stem else "data"


def safe_member_path(name: str) -> str | None:
    """Relative POSIX path to write a member to, or ``None`` when it must be skipped.

    Absolute paths, drive letters and ``..`` are refused outright rather than repaired: a
    member that tries to escape is hostile, and a repaired name could overwrite a sibling.
    """
    unified = name.replace("\\", "/")
    if unified.startswith("/") or (len(unified) > 1 and unified[1] == ":"):
        return None
    parts = [part for part in unified.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(safe_segment(part) for part in parts)


@dataclass(frozen=True, slots=True)
class ExtractLimits:
    """Caps per archive, counted on bytes actually written, not on what headers claim."""

    max_bytes: int
    max_members: int


@dataclass(frozen=True, slots=True)
class ExtractResult:
    files: int  # regular files written
    bytes: int  # bytes written
    skipped: int  # members not written: unsafe names, links, devices

"""Status state machines for files and listings.

The same values are enforced by SQL ``CHECK`` constraints in the state database, so a bad
status cannot be written even by code that skips these helpers.
"""

import enum
from collections.abc import Mapping

from archive_mirror.domain.errors import InvalidTransition


class FileStatus(enum.StrEnum):
    LISTED = "listed"
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"


class ListingStatus(enum.StrEnum):
    LISTED = "listed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunOutcome(enum.StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


FILE_TRANSITIONS: Mapping[FileStatus, frozenset[FileStatus]] = {
    FileStatus.LISTED: frozenset({FileStatus.DOWNLOADING}),
    # LISTED from DOWNLOADING: an interrupted download is rolled back to its .part file.
    FileStatus.DOWNLOADING: frozenset({FileStatus.DONE, FileStatus.FAILED, FileStatus.LISTED}),
    FileStatus.FAILED: frozenset({FileStatus.DOWNLOADING, FileStatus.LISTED}),
    # LISTED from DONE: the upstream size or date changed, so the copy is stale.
    FileStatus.DONE: frozenset({FileStatus.LISTED}),
}


class ExtractStatus(enum.StrEnum):
    """Second state machine, for archives only. It starts when the download is done."""

    PENDING = "pending"
    EXTRACTING = "extracting"
    DONE = "done"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


EXTRACT_TRANSITIONS: Mapping[ExtractStatus, frozenset[ExtractStatus]] = {
    ExtractStatus.PENDING: frozenset({ExtractStatus.EXTRACTING}),
    # PENDING from EXTRACTING: interrupted, the half-written directory is discarded next time.
    ExtractStatus.EXTRACTING: frozenset(
        {
            ExtractStatus.DONE,
            ExtractStatus.FAILED,
            ExtractStatus.UNSUPPORTED,
            ExtractStatus.PENDING,
        }
    ),
    # A failure is retried by a later run, like a failed download: it may have been local
    # (disk full). done and unsupported are left only when a new copy of the archive is
    # downloaded, which sets ``pending`` again together with ``done`` for the file.
    ExtractStatus.FAILED: frozenset({ExtractStatus.EXTRACTING}),
    ExtractStatus.DONE: frozenset(),
    ExtractStatus.UNSUPPORTED: frozenset(),
}


def check_transition(current: FileStatus, target: FileStatus) -> None:
    """Raise ``InvalidTransition`` unless ``current -> target`` is allowed."""
    if target not in FILE_TRANSITIONS[current]:
        raise InvalidTransition(f"{current} -> {target} is not allowed")


def check_extract_transition(current: ExtractStatus, target: ExtractStatus) -> None:
    if target not in EXTRACT_TRANSITIONS[current]:
        raise InvalidTransition(f"extract {current} -> {target} is not allowed")

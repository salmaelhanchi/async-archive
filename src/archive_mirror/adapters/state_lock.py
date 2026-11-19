"""One process per state database, enforced with an OS file lock.

Startup recovery (``downloading`` back to ``listed``) is only correct if no other process is
downloading those files right now. An OS lock fits: it is released when the process exits
for any reason, including a kill, so a stale lock can never block a resume.
"""

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from archive_mirror.domain.errors import StateLocked


def _try_lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def exclusive_state(state_db: Path) -> Iterator[None]:
    """Hold ``<state_db>.lock`` for the duration, or raise ``StateLocked`` at once."""
    lock_path = state_db.with_name(state_db.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            _try_lock(fd)
        except OSError:
            raise StateLocked(f"{state_db} is in use by another archive-mirror process") from None
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock on every platform

"""Retry timing: exponential backoff with full jitter, and ``Retry-After`` parsing."""

from datetime import datetime
from email.utils import parsedate_to_datetime


def backoff_delay(attempt: int, *, base: float, cap: float, jitter: float) -> float:
    """Delay before retry number ``attempt`` (0-based).

    ``jitter`` is a number in ``[0, 1)`` supplied by the caller, so the function stays pure.
    Full jitter spreads retries from many workers instead of synchronising them.
    """
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    ceiling = min(cap, base * 2.0 ** min(attempt, 62))
    return ceiling * jitter


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Seconds to wait from a ``Retry-After`` header, or ``None`` when absent or unreadable.

    The header is either a number of seconds or an HTTP date. ``now`` must be timezone-aware.
    """
    if value is None:
        return None
    text = value.strip()
    if text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - now).total_seconds())

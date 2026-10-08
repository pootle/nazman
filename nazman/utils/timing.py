"""Small helpers for measuring elapsed time between persisted timestamps."""

from datetime import datetime, timezone
from typing import Optional


def elapsed_ms(start: Optional[datetime], end: Optional[datetime]) -> Optional[int]:
    """Milliseconds between two datetimes, tolerating naive/aware mixtures.

    SQLite drops tzinfo on the round-trip, so a stored start time is often
    naive while a freshly computed end time is aware; normalize both to UTC
    and return ``None`` when a value is missing or uncomparable.
    """
    if start is None or end is None:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    try:
        return int((end - start).total_seconds() * 1000)
    except (TypeError, ValueError):
        return None
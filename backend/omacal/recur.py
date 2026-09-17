"""RRULE expansion helpers shared by sync and the API.

Kept in one place so the sync side (does this master still have occurrences
inside the cache window?) and the API side (which occurrences fall in this
query window?) agree on the same rules.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from dateutil.rrule import rrulestr

# dateutil refuses a DATE-valued UNTIL when DTSTART is timezone-aware
# ("RRULE UNTIL values must be specified in UTC when DTSTART is timezone-aware").
# Real calendars in the wild carry both forms, so normalise the date form.
_DATE_UNTIL_RE = re.compile(r"UNTIL=(\d{8})(?=[;,]|$)")


def normalise_rrule(rrule: str) -> str:
    return _DATE_UNTIL_RE.sub(lambda m: f"UNTIL={m.group(1)}T235959Z", rrule)


def parse_dt(value) -> datetime | None:
    """Parse an ISO string / datetime / date into an aware datetime (UTC if naive)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if hasattr(value, "year") and not hasattr(value, "hour"):  # datetime.date
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def build_rule(rrule: str, dtstart: datetime):
    """Return a dateutil rrule for the master, or None if it cannot be built."""
    try:
        return rrulestr(normalise_rrule(rrule), dtstart=dtstart)
    except Exception:
        return None


def occurrences_between(
    rrule: str,
    dtstart: datetime,
    window_start: datetime,
    window_end: datetime,
    duration: timedelta = timedelta(hours=1),
    exdates: list[str] | None = None,
    limit: int = 400,
) -> list[tuple[datetime, datetime]]:
    """Occurrences of a series overlapping [window_start, window_end).

    Returns [(start, end), ...] with EXDATEs removed. `limit` is a safety net
    against a pathologically old series with a huge window; the API windows are
    at most a month, so it never bites in practice.
    """
    rule = build_rule(rrule, dtstart)
    if rule is None:
        return []

    excluded = set()
    for raw in exdates or []:
        dt = parse_dt(raw)
        if dt is not None:
            excluded.add(dt.astimezone(timezone.utc))

    # an occurrence starting before the window can still overlap it
    search_from = window_start - duration - timedelta(days=1)
    out: list[tuple[datetime, datetime]] = []
    for start in rule.xafter(search_from, inc=True, count=limit):
        occ_end = start + duration
        if start >= window_end:
            break
        if occ_end > window_start and start.astimezone(timezone.utc) not in excluded:
            out.append((start, occ_end))
    return out


def has_occurrence_after(rrule: str, dtstart: datetime, after: datetime) -> bool | None:
    """True if the series has any occurrence at/after `after`.

    None means the rule could not be parsed, in which case callers should keep
    the master rather than silently dropping it.
    """
    rule = build_rule(rrule, dtstart)
    if rule is None:
        return None
    try:
        return rule.after(after, inc=True) is not None
    except Exception:
        return None

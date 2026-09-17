"""RRULE expansion helpers shared by sync and the API.

Kept in one place so the sync side (does this master still have occurrences
inside the cache window?) and the API side (which occurrences fall in this
query window?) agree on the same rules.
"""

from __future__ import annotations

import json
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


def _ts(value) -> datetime | None:
    dt = parse_dt(value)
    return dt.astimezone(timezone.utc) if dt is not None else None


def _load_exdates(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def expand_events(
    masters: list[dict],
    overrides: list[dict],
    window_start: datetime,
    window_end: datetime,
) -> list[dict]:
    """Turn cached rows into per-occurrence rows for [window_start, window_end).

    Rows without an rrule pass through untouched. A series is expanded into one
    row per occurrence, each carrying its master's fields with `start`/`end`
    moved to that occurrence, `recurrence_id` set to the occurrence start (the
    value a single-occurrence delete has to send back) and `is_recurring` set.

    A detached override replaces the occurrence it points at; an override with
    STATUS:CANCELLED removes it.
    """
    by_key: dict[tuple, dict] = {}
    for ov in overrides:
        by_key[(ov.get("calendar_id"), ov.get("uid"), _ts(ov.get("recurrence_id")))] = ov

    out: list[dict] = []
    for ev in masters:
        rrule = ev.get("rrule")
        if not rrule:
            out.append(ev)
            continue
        if str(ev.get("status") or "").upper() == "CANCELLED":
            continue

        start_dt = parse_dt(ev.get("start"))
        if start_dt is None:
            continue
        end_dt = parse_dt(ev.get("end"))
        if end_dt is not None and end_dt > start_dt:
            duration = end_dt - start_dt
        else:
            duration = timedelta(days=1) if ev.get("all_day") else timedelta(hours=1)

        for occ_start, occ_end in occurrences_between(
            rrule, start_dt, window_start, window_end, duration, _load_exdates(ev.get("exdates"))
        ):
            row = dict(ev)
            row["start"] = occ_start.isoformat()
            row["end"] = occ_end.isoformat()
            row["recurrence_id"] = occ_start.isoformat()
            row["is_recurring"] = 1

            ov = by_key.get((ev.get("calendar_id"), ev.get("uid"), _ts(occ_start)))
            if ov is not None:
                if str(ov.get("status") or "").upper() == "CANCELLED":
                    continue
                for field in ("summary", "description", "location", "all_day"):
                    if ov.get(field) not in (None, ""):
                        row[field] = ov[field]
                ov_start = parse_dt(ov.get("start"))
                if ov_start is not None:
                    row["start"] = ov_start.isoformat()
                    row["end"] = (parse_dt(ov.get("end")) or (ov_start + duration)).isoformat()
                row["is_override"] = 1
            out.append(row)

    out.sort(key=lambda e: str(e.get("start") or ""))
    return out

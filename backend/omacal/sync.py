"""CalDAV sync — pull events from remote calendars into the local SQLite cache."""

from __future__ import annotations

import json
import logging
import re
import threading
import traceback
from datetime import datetime, timedelta, timezone, date as date_type
from pathlib import Path
from typing import Any

import caldav
from icalendar import Calendar

from omacal.config import load_config
from omacal.db import add_calendar, replace_calendar_events
from omacal.recur import has_occurrence_after

logger = logging.getLogger("omacal.sync")

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?)?$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _unwrap_dt(value: Any) -> datetime | datetime.date | None:
    """Unwrap an icalendar vDDDTypes value (or raw string) into a datetime/date."""
    if value is None:
        return None
    # icalendar vDDDTypes — check by duck-typing the dt attribute
    if hasattr(value, "dt") and value.dt is not None:
        return value.dt
    if isinstance(value, (datetime, date_type)):
        return value
    # Already a string — parse it
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if _ISO_RE.match(text):
            if _ISO_DATE_RE.match(text):
                parts = text.split("-")
                return datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
            return datetime.fromisoformat(text)
        return None
    return None


def _exdate_values(component) -> list[str]:
    """ISO strings for every EXDATE on a VEVENT (icalendar yields vDDDLists)."""
    raw = component.get("EXDATE")
    if raw is None:
        return []
    out: list[str] = []
    for item in (raw if isinstance(raw, list) else [raw]):
        for d in getattr(item, "dts", [item]):
            dt = _unwrap_dt(d.dt if hasattr(d, "dt") else d)
            if dt is not None:
                out.append(dt.isoformat())
    return out


def _parse_ical_events(cal_data: bytes, calendar_id: int, cutoff: datetime) -> dict[str, list[dict[str, Any]]]:
    """Parse raw iCalendar bytes into {"events": [...], "overrides": [...]}.

    Masters are kept when the series still has occurrences overlapping
    [cutoff, cutoff+90d) -- judged by the occurrences, not by DTSTART.
    Detached overrides (RECURRENCE-ID components) come back separately.
    """
    calendar = Calendar.from_ical(cal_data)
    events: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    window_end = cutoff + timedelta(days=90)

    for component in calendar.walk():
        if component.name != "VEVENT":
            continue

        uid = str(component.get("UID", ""))
        summary = str(component.get("SUMMARY", "")) if component.get("SUMMARY") else ""
        description = str(component.get("DESCRIPTION", "")) if component.get("DESCRIPTION") else ""
        location = str(component.get("LOCATION", "")) if component.get("LOCATION") else ""

        dtstart = component.get("DTSTART")
        end_prop = component.get("DTEND")
        dtend = component.get("DTEND")

        if dtstart is None:
            continue

        start_dt = _unwrap_dt(dtstart.dt if hasattr(dtstart, "dt") else dtstart)
        if start_dt is None:
            continue

        all_day = isinstance(start_dt, date_type) and not isinstance(start_dt, datetime)

        if isinstance(start_dt, datetime):
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
        elif isinstance(start_dt, date_type):
            if all_day:
                start_dt = datetime(start_dt.year, start_dt.month, start_dt.day, tzinfo=timezone.utc)
            else:
                continue

        end_dt = None
        if dtend is not None:
            end_dt = _unwrap_dt(dtend.dt if hasattr(dtend, "dt") else dtend)
            if end_dt is not None:
                if isinstance(end_dt, datetime):
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                elif isinstance(end_dt, date_type):
                    end_dt = datetime(end_dt.year, end_dt.month, end_dt.day, tzinfo=timezone.utc)
                    all_day = True

        # recurrence_id for exceptions
        rid = None
        rid_prop = component.get("RECURRENCE-ID")
        if rid_prop is not None:
            rid = _unwrap_dt(rid_prop.dt if hasattr(rid_prop, "dt") else rid_prop)
            if rid is not None and isinstance(rid, datetime) and rid.tzinfo is None:
                rid = rid.replace(tzinfo=timezone.utc)

        status = str(component.get("STATUS", ""))
        transparency = str(component.get("TRANSP", "OPAQUE"))

        # Fill in missing end
        if end_dt is None:
            if all_day:
                end_dt = start_dt + timedelta(days=1)
            else:
                end_dt = start_dt + timedelta(hours=1)

        # A detached override (RECURRENCE-ID) belongs to one occurrence of its
        # master and shares the master's UID: caching it in `events` would
        # overwrite the master row on upsert. Keep it in its own list.
        if rid_prop is not None:
            if start_dt >= window_end or end_dt <= cutoff:
                continue
            overrides.append({
                "uid": uid,
                "recurrence_id": rid.isoformat() if rid else None,
                "summary": summary,
                "description": description,
                "location": location,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat() if end_dt else None,
                "all_day": 1 if all_day else 0,
                "status": status,
            })
            continue

        rrule_str = None
        rrule_prop = component.get("RRULE")
        if rrule_prop is not None:
            rrule_str = rrule_prop.to_ical().decode()
            # A recurring master's DTSTART can sit far outside the window while
            # occurrences still land inside it (why an old yearly birthday
            # never appeared): judge the series by its occurrences. None means
            # "could not parse" -- keep it rather than silently dropping a
            # series, so a broken rule is visible instead of invisible.
            if has_occurrence_after(rrule_str, start_dt, cutoff - timedelta(seconds=1)) is False:
                continue
        elif start_dt >= window_end or end_dt <= cutoff:
            # one-off event: plain window overlap
            continue

        events.append({
            "uid": uid,
            "summary": summary,
            "description": description,
            "location": location,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat() if end_dt else None,
            "all_day": 1 if all_day else 0,
            "recurrence_id": rid.isoformat() if rid else None,
            "status": status,
            "transparency": transparency,
            "rrule": rrule_str,
            "exdates": json.dumps(_exdate_values(component)) if rrule_str else None,
        })

    return {"events": events, "overrides": overrides}


def _check_writable(client: Any, cal_url: str) -> bool:
    """Check if the current user has write privileges on a CalDAV calendar.

    Uses PROPFIND to fetch current-user-privilege-set (RFC 3744).
    Returns True if the user has 'write', 'write-content', or 'all' privilege.
    Returns True if the check fails (conservative: don't filter on error).
    """
    import xml.etree.ElementTree as ET
    body = (
        '<?xml version="1.0" encoding="utf-8" ?>\n'
        '<d:propfind xmlns:d="DAV:">\n'
        '  <d:prop><d:current-user-privilege-set/></d:prop>\n'
        '</d:propfind>'
    )
    try:
        resp = client.propfind(cal_url, body, depth=0)
        if resp.results and resp.results[0].status == 200:
            props = resp.results[0].properties
            priv_elem = props.get("{DAV:}current-user-privilege-set")
            if priv_elem is not None:
                for elem in priv_elem.iter():
                    tag = elem.tag
                    localname = tag.split("}")[-1] if "}" in tag else tag
                    if localname in ("write", "write-content", "all"):
                        return True
                return False  # Privilege set present but no write privileges
    except Exception as e:
        logger.debug("Could not check ACL for %s: %s (assuming writable)", cal_url, e)
    return True  # Conservative: assume writable if we can't determine


def _discover_calendars(
    url: str,
    username: str | None,
    password: str | None,
) -> list[dict[str, Any]]:
    """Discover calendars at a CalDAV URL. Returns list of {uid, display_name, url, principal_url}."""
    try:
        client = caldav.DAVClient(url=url, username=username, password=password)
        principal = client.principal()
        calendars = principal.calendars()
    except Exception:
        try:
            client = caldav.DAVClient(url=url, username=username, password=password)
            calendars = client.calendars()
        except Exception as e:
            logger.error("Failed to discover calendars at %s: %s", url, e)
            return []

    result = []
    for cal in calendars:
        try:
            cal_url = str(cal.url)
            props = cal.get_properties()
            # Skip task-only calendars — they don't have VEVENTs
            comp_set = props.get("{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set")
            if comp_set and "VEVENT" not in comp_set:
                logger.info("Skipping non-event calendar: %s (%s)", cal_url, comp_set)
                continue
            result.append({
                "uid": cal_url.rsplit("/", 1)[-1] or cal_url,
                "display_name": cal.get_display_name() or "Calendar",
                "url": cal_url,
                "color": props.get("{http://apple.com/ns/ical/}calendar-color"),
                "principal_url": (str(principal.url) if principal.url else None),
                "writable": _check_writable(client, cal_url),
            })
        except Exception as e:
            logger.warning("Skipping calendar %s: %s", cal_url, e)

    return result


def sync_source(
    conn: Any,
    url: str,
    username: str | None,
    password: str | None,
    calendar_uid: str | None = None,
    display_name: str | None = None,
    color: str | None = None,
) -> tuple[int, list[str], list[str]]:
    """
    Sync one CalDAV source into the database.

    Returns (events stored, URLs whose cache was replaced, URLs that failed).
    Raises if the source itself failed (discovery/connect) -- the caller must
    not treat that as "nothing to sync" when pruning stale calendars.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    if calendar_uid:
        # Direct calendar URL — no discovery needed
        cals = [{"uid": calendar_uid, "display_name": display_name or "Calendar", "url": url, "principal_url": None}]
    else:
        discovered = _discover_calendars(url, username, password)
        if not discovered:
            # Do NOT return "0 events, nothing synced": sync_all used to treat a
            # failed discovery as a successful empty source and then delete every
            # cache row not in the (empty) synced set. A source-level failure has
            # to reach the caller as a failure.
            raise RuntimeError(f"no calendars discovered at {url}")
        cals = discovered

    try:
        client = caldav.DAVClient(url=url, username=username, password=password)
    except Exception as e:
        raise RuntimeError(f"failed to connect to CalDAV server {url}: {e}") from e

    total_events = 0
    urls_synced: list[str] = []
    urls_failed: list[str] = []
    for cal_info in cals:
        cal_uid = cal_info["uid"]
        display = cal_info["display_name"]
        prop_color = cal_info.get("color")
        is_writable = cal_info.get("writable", True)

        cal_id = add_calendar(
            conn, cal_uid, display, cal_info["url"],
            username if is_writable else None,
            password if is_writable else None,
            prop_color, cal_info.get("principal_url"),
        )

        # Fetch BEFORE touching the cache. The old order cleared this calendar's
        # rows and then fetched, so a GET /api/events during a sync saw it
        # mid-wipe (row count bouncing 30 -> 16 -> 30) and a failed fetch left
        # the calendar empty for the whole interval. A fetch that raises now
        # leaves the previous cache untouched.
        try:
            cal_obj = client.calendar(url=cal_info["url"])
            raw_events = cal_obj.events()
        except Exception as e:
            logger.error("Sync failed for %s: %s", display, e)
            urls_failed.append(cal_info["url"])
            continue

        if not raw_events:
            logger.warning("No events returned from %s -- clearing its cached rows", cal_info["url"])

        events = []
        overrides = []
        for ev in raw_events:
            try:
                if ev.data:
                    parsed = _parse_ical_events(ev.data.encode("utf-8"), cal_id, cutoff)
                    events.extend(parsed["events"])
                    overrides.extend(parsed["overrides"])
            except Exception as e:
                logger.warning("Skipping event in %s: %s", display, e)

        # One transaction: readers see the old set or the new set, never a gap.
        replace_calendar_events(conn, cal_id, events, overrides)
        total_events += len(events)
        urls_synced.append(cal_info["url"])
        logger.info("Synced %d events from %s", len(events), display)

    return total_events, urls_synced, urls_failed


def create_event(
    conn: Any,
    calendar_id: int,
    summary: str,
    start: datetime,
    end: datetime | None = None,
    all_day: bool = False,
    location: str | None = None,
    rrule: str | None = None,
) -> dict[str, Any]:
    """Create an event on the CalDAV server and cache it locally.

    all_day=True stores a DATE-valued event (DTSTART;VALUE=DATE), which is what
    every other calendar client expects -- a midnight datetime is a timed event
    to them. rrule makes it a repeating series (e.g. "FREQ=WEEKLY;INTERVAL=2").

    Pushes the event first; only writes to the local SQLite cache if the
    server accepts it.  Returns the inserted row as a dict (same shape as
    get_events rows) on success.
    """
    from uuid import uuid4

    from omacal.db import add_event

    # Look up calendar URL and credentials from the local cache
    cur = conn.execute(
        "SELECT url, username, password FROM calendars WHERE id = ?",
        (calendar_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Calendar id={calendar_id} not found in local cache")

    uid = str(uuid4())

    # The panel sends ISO datetimes; turn an all-day event into dates so the
    # server stores VALUE=DATE, with DTEND exclusive (iCalendar semantics).
    if all_day:
        start_date = start.date() if isinstance(start, datetime) else start
        end_date = None
        if end is not None:
            end_date = end.date() if isinstance(end, datetime) else end
        if end_date is None or end_date <= start_date:
            end_date = start_date + timedelta(days=1)
        push_kwargs: dict[str, Any] = {"dtstart": start_date, "dtend": end_date}
        start_iso = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc).isoformat()
        end_iso = datetime(end_date.year, end_date.month, end_date.day, tzinfo=timezone.utc).isoformat()
    else:
        if end is None:
            end = start + timedelta(hours=1)
        push_kwargs = {"dtstart": start, "dtend": end}
        start_iso, end_iso = start.isoformat(), end.isoformat()

    if rrule:
        push_kwargs["rrule"] = rrule

    # Push to CalDAV server
    client = caldav.DAVClient(
        url=row["url"],
        username=row["username"],
        password=row["password"],
    )
    cal_obj = client.calendar(url=row["url"])
    cal_obj.add_event(summary=summary, uid=uid, location=location, **push_kwargs)

    # Cache in local DB
    return add_event(
        conn,
        calendar_id,
        uid,
        summary,
        start_iso,
        end_iso,
        1 if all_day else 0,
        location,
        rrule=rrule,
    )


def delete_event(
    conn: Any,
    calendar_id: int,
    uid: str,
    occurrence: datetime | None = None,
    series: bool = False,
) -> dict[str, Any]:
    """Delete an event from the CalDAV server and local cache.

    occurrence=None -> remove the whole resource (a one-off event)
    occurrence=<dt> -> remove just that occurrence of a series by adding an
                       EXDATE, leaving the rest of the series untouched

    A recurring event with neither `occurrence` nor `series=True` raises: a
    right-click on one occurrence must never silently wipe the series.
    """
    from omacal.db import delete_event as db_delete_event, set_event_exdates
    from omacal.recur import parse_dt

    cur = conn.execute(
        "SELECT url, username, password FROM calendars WHERE id = ?",
        (calendar_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Calendar id={calendar_id} not found in local cache")

    client = caldav.DAVClient(
        url=row["url"],
        username=row["username"],
        password=row["password"],
    )
    cal_obj = client.calendar(url=row["url"])
    event = cal_obj.get_event_by_uid(uid)

    comp = Calendar.from_ical(event.data)
    masters = [c for c in comp.walk("VEVENT") if not c.get("RECURRENCE-ID")]
    master = masters[0] if masters else None
    master_rrule = master.get("RRULE") if master is not None else None

    if occurrence is None:
        if master_rrule is not None and not series:
            raise ValueError(
                "this is a repeating event: pass occurrence=<ISO datetime> to "
                "delete one occurrence, or series=true to delete the whole series"
            )
        event.delete()
        db_delete_event(conn, calendar_id, uid)
        return {"status": "deleted", "uid": uid, "scope": "series" if series else "event"}

    if master is None or master_rrule is None:
        raise ValueError("occurrence given but this event does not repeat")

    target = parse_dt(occurrence)
    if target is None:
        raise ValueError(f"invalid occurrence value: {occurrence!r}")

    # Mirror the master's DTSTART form so the EXDATE matches what other clients
    # (and this parser) compare against: DATE for all-day, the same tz otherwise.
    dtstart = master.get("DTSTART").dt
    if not hasattr(dtstart, "hour"):                      # all-day series
        exdate_value = target.date()
    elif dtstart.tzinfo is not None:
        exdate_value = target.astimezone(dtstart.tzinfo)
    else:
        exdate_value = target.replace(tzinfo=None)

    existing = [parse_dt(v) for v in _exdate_values(master)]
    existing = [d for d in existing if d is not None]
    if any(d == target or (d is not None and d.astimezone(timezone.utc) == target.astimezone(timezone.utc)) for d in existing):
        return {"status": "already-excluded", "uid": uid, "occurrence": target.isoformat()}

    master.add("EXDATE", exdate_value)
    event.data = comp.to_ical().decode()
    event.save()

    new_exdates = [d.isoformat() for d in existing] + [target.isoformat()]
    set_event_exdates(conn, calendar_id, uid, new_exdates)

    return {"status": "deleted", "scope": "occurrence", "uid": uid, "occurrence": target.isoformat()}


def update_event(
    conn: Any,
    calendar_id: int,
    uid: str,
    summary: str,
    start: datetime,
    end: datetime | None = None,
    all_day: bool = False,
    location: str | None = None,
) -> dict[str, Any]:
    """Update an existing event on the CalDAV server and local cache."""
    from omacal.db import update_event as db_update_event

    cur = conn.execute(
        "SELECT url, username, password FROM calendars WHERE id = ?",
        (calendar_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Calendar id={calendar_id} not found in local cache")

    client = caldav.DAVClient(
        url=row["url"],
        username=row["username"],
        password=row["password"],
    )
    cal_obj = client.calendar(url=row["url"])
    try:
        event = cal_obj.get_event_by_uid(uid)
    except caldav.error.NotFoundError:
        # Event not on the target calendar — search all calendars on this server
        principal = client.principal()
        event = None
        for cal in principal.calendars():
            try:
                event = cal.get_event_by_uid(uid)
                break
            except caldav.error.NotFoundError:
                continue
        if event is None:
            raise ValueError(f"Event uid={uid} not found on any calendar")
        # Found on a different calendar — move it: delete from old, create on new
        event.delete()
        cal_obj.add_event(
            uid=uid,
            dtstart=start,
            dtend=end,
            summary=summary,
            location=location,
        )
        from omacal.db import delete_event as db_delete_event, add_event as db_add_event
        # Delete old DB row (by old calendar_id), then insert fresh
        cur2 = conn.execute(
            "SELECT calendar_id FROM events WHERE uid = ? ORDER BY start DESC LIMIT 1",
            (uid,),
        )
        old_row = cur2.fetchone()
        cur2.close()
        old_cal = old_row["calendar_id"] if old_row else calendar_id
        db_delete_event(conn, old_cal, uid)
        return db_add_event(
            conn,
            calendar_id,
            uid,
            summary,
            start.isoformat(),
            end.isoformat() if end else None,
            1 if all_day else 0,
            location,
        )
    comp = event.icalendar_component
    comp["summary"] = summary
    from icalendar import vDatetime, vDate
    if all_day:
        comp["dtstart"] = vDate(start.date()) if isinstance(start, datetime) else vDate(start)
        if end:
            comp["dtend"] = vDate(end.date()) if isinstance(end, datetime) else vDate(end)
    else:
        comp["dtstart"] = vDatetime(start)
        if end:
            comp["dtend"] = vDatetime(end)
    if location:
        comp["location"] = location
    else:
        comp.pop("location", None)
    event.data = comp.to_ical().decode()
    event.save()

    from omacal.db import add_event as db_add_event
    # Delete stale rows (sync may have inserted duplicates) then insert fresh.
    # INSERT OR REPLACE in add_event handles sync race for same (calendar_id, uid).
    # Preserve recurrence state: the resource keeps its RRULE/EXDATE (this path
    # only rewrites summary/dtstart/dtend/location), so the cached row must keep
    # them too or the panel would stop expanding the series until the next sync.
    prev = conn.execute(
        "SELECT rrule, exdates FROM events WHERE uid = ? ORDER BY start DESC LIMIT 1", (uid,)
    ).fetchone()
    prev_rrule = prev["rrule"] if prev else None
    prev_exdates = prev["exdates"] if prev else None
    conn.execute("DELETE FROM events WHERE uid = ?", (uid,))
    conn.commit()
    return db_add_event(
        conn,
        calendar_id,
        uid,
        summary,
        start.isoformat(),
        end.isoformat() if end else None,
        1 if all_day else 0,
        location,
        rrule=prev_rrule,
        exdates=prev_exdates,
    )


_sync_lock = threading.Lock()


def sync_all(config_path: Path | None = None, *, raise_if_busy: bool = False) -> dict[str, Any]:
    """Sync all configured calendars. Returns summary.

    Serialized by a process-wide lock: the periodic timer thread and
    POST /api/sync used to run sync_all() concurrently on separate SQLite
    connections, double-fetching every calendar and fighting over the same rows
    ("database is locked"). An overlapping call returns a skipped summary (or
    raises, for the API's explicit-sync endpoint) instead.
    """
    if not _sync_lock.acquire(blocking=False):
        logger.warning("Sync already in progress; skipping this run")
        if raise_if_busy:
            raise RuntimeError("sync already in progress")
        return {"total_events": 0, "calendars": {}, "status": "skipped",
                "reason": "sync already in progress"}
    try:
        return _sync_all_locked(config_path)
    finally:
        _sync_lock.release()


def _sync_all_locked(config_path: Path | None = None) -> dict[str, Any]:
    cfg = load_config(config_path)
    db_path = Path(cfg["database"])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    import sqlite3
    from omacal.db import _migrate
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)

    results = {}
    total = 0

    # Track which calendar URLs this run actually replaced, and which sources
    # finished cleanly. The stale prune below must never run on partial
    # information -- it used to re-discover calendars itself (a second call,
    # ~30 extra requests), and a discovery failure there returned [] which
    # marked EVERY calendar stale and deleted it along with its events.
    synced_urls: set[str] = set()
    incomplete: list[str] = []
    for cal in cfg["calendars"]:
        if not cal.get("enabled", True):
            continue
        name = cal.get("display_name", cal.get("url", "unknown"))
        try:
            n, urls, failed = sync_source(
                conn,
                cal["url"],
                cal.get("username"),
                cal.get("password"),
                cal.get("calendar_uid"),
                cal.get("display_name"),
                cal.get("color"),
            )
            results[name] = {"status": "ok", "events": n}
            total += n
            synced_urls.update(urls)
            if failed:
                results[name]["status"] = "partial"
                results[name]["failed"] = failed
                incomplete.append(name)
                logger.warning("%s: %d calendar(s) failed to fetch", name, len(failed))
        except Exception as e:
            results[name] = {"status": "error", "error": str(e)}
            incomplete.append(name)
            logger.error("Sync failed for %s: %s", name, e)

    if incomplete:
        logger.warning(
            "Skipping stale-calendar cleanup: %d source(s) did not complete (%s)",
            len(incomplete), ", ".join(incomplete),
        )
    else:
        # Remove stale calendar entries that are no longer discovered.
        cur = conn.execute("SELECT id, url FROM calendars")
        stale = [row["id"] for row in cur.fetchall() if row["url"] not in synced_urls]
        if stale:
            placeholders = ",".join("?" for _ in stale)
            conn.execute(f"DELETE FROM calendars WHERE id IN ({placeholders})", stale)
            conn.execute(f"DELETE FROM events WHERE calendar_id IN ({placeholders})", stale)
            conn.execute(f"DELETE FROM event_overrides WHERE calendar_id IN ({placeholders})", stale)
            conn.commit()
            logger.info("Removed %d stale calendar(s) from cache", len(stale))

    conn.close()
    return {"total_events": total, "calendars": results}

"""CalDAV sync — pull events from remote calendars into the local SQLite cache."""

from __future__ import annotations

import logging
import re
import traceback
from datetime import datetime, timedelta, timezone, date as date_type
from pathlib import Path
from typing import Any

import caldav
from icalendar import Calendar

from omacal.config import load_config
from omacal.db import add_calendar, clear_calendar_events, upsert_events

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


def _parse_ical_events(cal_data: bytes, calendar_id: int, cutoff: datetime) -> list[dict[str, Any]]:
    """Parse raw iCalendar bytes and return events overlapping [cutoff, cutoff+90d)."""
    calendar = Calendar.from_ical(cal_data)
    events = []
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

        # Filter: event overlaps window if start < window_end and end > cutoff
        if start_dt >= window_end:
            continue
        if end_dt <= cutoff:
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
        })

    return events


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
) -> int:
    """
    Sync one CalDAV source into the database.
    Returns number of events stored.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    if calendar_uid:
        # Direct calendar URL — no discovery needed
        cals = [{"uid": calendar_uid, "display_name": display_name or "Calendar", "url": url, "principal_url": None}]
    else:
        discovered = _discover_calendars(url, username, password)
        if not discovered:
            logger.error("No calendars discovered at %s", url)
            return 0
        cals = discovered

    try:
        client = caldav.DAVClient(url=url, username=username, password=password)
    except Exception as e:
        logger.error("Failed to connect to CalDAV server %s: %s", url, e)
        return 0

    total_events = 0
    for cal_info in cals:
        cal_uid = cal_info["uid"]
        display = cal_info["display_name"]
        prop_color = cal_info.get("color")

        cal_id = add_calendar(
            conn, cal_uid, display, cal_info["url"],
            username, password, prop_color, cal_info.get("principal_url"),
        )
        clear_calendar_events(conn, cal_id)

        try:
            cal_obj = client.calendar(url=cal_info["url"])
            raw_events = cal_obj.events()
            if not raw_events:
                logger.info("No events from %s", cal_info["url"])
                continue

            events = []
            for ev in raw_events:
                try:
                    if ev.data:
                        parsed = _parse_ical_events(ev.data.encode("utf-8"), cal_id, cutoff)
                        events.extend(parsed)
                except Exception as e:
                    logger.warning("Skipping event in %s: %s", display, e)

            if events:
                upsert_events(conn, cal_id, events)
                total_events += len(events)
                logger.info("Synced %d events from %s", len(events), display)
        except Exception as e:
            logger.error("Sync failed for %s: %s", display, e)

    return total_events


def sync_all(config_path: Path | None = None) -> dict[str, Any]:
    """Sync all configured calendars. Returns summary."""
    cfg = load_config(config_path)
    db_path = Path(cfg["database"])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    import sqlite3
    from omacal.db import _migrate
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _migrate(conn)

    results = {}
    total = 0

    # Track which calendar URLs we're syncing this run.
    synced_urls: set[str] = set()
    for cal in cfg["calendars"]:
        if not cal.get("enabled", True):
            continue
        name = cal.get("display_name", cal.get("url", "unknown"))
        try:
            n = sync_source(
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
            for c in _discover_calendars(cal["url"], cal.get("username"), cal.get("password")):
                synced_urls.add(c["url"])
        except Exception as e:
            results[name] = {"status": "error", "error": str(e)}
            logger.error("Sync failed for %s: %s", name, e)

    # Remove stale calendar entries that are no longer discovered.
    cur = conn.execute("SELECT id, url FROM calendars")
    stale = [row["id"] for row in cur.fetchall() if row["url"] not in synced_urls]
    if stale:
        placeholders = ",".join("?" for _ in stale)
        conn.execute(f"DELETE FROM calendars WHERE id IN ({placeholders})", stale)
        conn.execute(f"DELETE FROM events WHERE calendar_id IN ({placeholders})", stale)
        conn.commit()
        logger.info("Removed %d stale calendar(s) from cache", len(stale))

    conn.close()
    return {"total_events": total, "calendars": results}

"""CalDAV sync — pull events from remote calendars into the local SQLite cache."""

from __future__ import annotations

import json
import logging
import re
import threading
import traceback
from datetime import datetime, time, timedelta, timezone, date as date_type
from pathlib import Path
from typing import Any

import caldav
from icalendar import Calendar, Alarm, vDatetime, vDate

from omacal.config import load_config
from omacal.db import (add_calendar, replace_calendar_events, replace_calendar_reminders,
                       upsert_events, upsert_overrides, upsert_reminders, upsert_reminder_overrides)
from omacal.recur import has_occurrence_after

logger = logging.getLogger("omacal.sync")

# caldav defaults to NO timeout (infinite), so a half-open connection hangs the
# sync thread forever -- and the old periodic timer re-armed only after a sync
# returned, so one hang stopped all future syncs. Bound every client.
_DAV_TIMEOUT = 30


def _dav_client(url: str, username: str | None, password: str | None) -> caldav.DAVClient:
    """Build a DAVClient with a bounded timeout."""
    return caldav.DAVClient(url=url, username=username, password=password, timeout=_DAV_TIMEOUT)


def _calendar_creds(conn, calendar_id: int) -> tuple[str, str | None, str | None]:
    """Return (url, username, password) for a cached calendar.

    The password is resolved from the system keyring, never from the DB. It is
    stored keyed by the principal URL (the URL the user configured, e.g.
    .../principals/users/<name>/), so look it up by principal_url and fall back
    to the calendar URL for a directly-configured calendar.
    """
    from omacal.keyring_store import get_password

    cur = conn.execute(
        "SELECT url, principal_url, username FROM calendars WHERE id = ?",
        (calendar_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"Calendar id={calendar_id} not found in local cache")
    url = row["url"]
    username = row["username"]
    key = row["principal_url"] or url
    password = get_password(key, username) if username else None
    return url, username, password

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


def _tzid_of(dtstart: Any, start_dt: Any) -> str | None:
    """IANA zone name for a DTSTART, or None if the event is all-day/naive/UTC."""
    pz = getattr(dtstart, "params", {})
    if isinstance(pz, dict):
        tz = pz.get("TZID")
        if tz:
            return str(tz)
    return getattr(getattr(start_dt, "tzinfo", None), "key", None)


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


def _parse_ical_events(cal_data: bytes, calendar_id: int, cutoff: datetime, href: str | None = None) -> dict[str, list[dict[str, Any]]]:
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

        # IANA zone name of this component's DTSTART. We cache `isoformat()`,
        # which bakes in a FIXED offset and drops the zone name (so a tz-aware
        # series expanded from cache drifts 1 h after DST, and a TZID-aware
        # EXDATE/override written by another client never matches). Persist the
        # TZID so expansion can re-attach a real (DST-aware) ZoneInfo at load.
        # All-day / floating / naive events carry no named zone -> None.
        tzid = _tzid_of(dtstart, start_dt)

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
                "href": href,
                "tzid": tzid,
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
            "href": href,
            "tzid": tzid,
        })

    return {"events": events, "overrides": overrides}


def _parse_ical_reminders(cal_data: bytes, calendar_id: int, cutoff: datetime, href: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Parse raw iCalendar bytes into {"reminders": [...], "overrides": [...]}.

    A reminder is a CalDAV VTODO: its DUE date is when it fires / needs doing.
    Masters are kept when the series still has occurrences overlapping
    [cutoff, cutoff+90d) (judged by the occurrences, not by a possibly-old
    DUE/DTSTART). Detached RECURRENCE-ID VTODOs come back as overrides.
    A VTODO that is definitely done (STATUS:COMPLETED/CANCELLED) is dropped.
    """
    calendar = Calendar.from_ical(cal_data)
    reminders: list[dict[str, Any]] = []
    overrides: list[dict[str, Any]] = []
    window_end = cutoff + timedelta(days=90)

    for component in calendar.walk():
        if component.name != "VTODO":
            continue

        uid = str(component.get("UID", ""))
        summary = str(component.get("SUMMARY", "")) if component.get("SUMMARY") else ""
        description = str(component.get("DESCRIPTION", "")) if component.get("DESCRIPTION") else ""
        location = str(component.get("LOCATION", "")) if component.get("LOCATION") else ""

        status = str(component.get("STATUS", ""))
        if status.upper() in ("COMPLETED", "CANCELLED"):
            continue

        priority = component.get("PRIORITY")
        priority_int = int(priority) if priority is not None and str(priority).isdigit() else None

        due_prop = component.get("DUE")
        start_prop = component.get("DTSTART")
        due_dt = _unwrap_dt(due_prop.dt if due_prop is not None and hasattr(due_prop, "dt") else due_prop) if due_prop is not None else None
        start_dt = _unwrap_dt(start_prop.dt if start_prop is not None and hasattr(start_prop, "dt") else start_prop) if start_prop is not None else None

        if due_dt is None and start_dt is None:
            continue

        # IANA zone name carried by DUE, else DTSTART.
        tzid = _tzid_of(due_prop if due_prop is not None else start_prop,
                        due_dt if due_dt is not None else start_dt)

        def _normalise(dt):
            if dt is None:
                return None
            if isinstance(dt, datetime):
                return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
            if isinstance(dt, date_type):
                return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
            return None

        due_dt = _normalise(due_dt)
        start_dt = _normalise(start_dt)
        # datetime is a subclass of date, so a TZID timed VALUE is a datetime
        # and must NOT count as all-day. All-day means a bare date (VALUE=DATE).
        all_day = (
            (due_prop is not None
                and isinstance(getattr(due_prop, "dt", None), date_type)
                and not isinstance(getattr(due_prop, "dt", None), datetime))
            or (start_prop is not None
                and isinstance(getattr(start_prop, "dt", None), date_type)
                and not isinstance(getattr(start_prop, "dt", None), datetime))
        )

        # Recurrence anchor: DUE preferred, else DTSTART.
        anchor = due_dt or start_dt
        rrule_str = None
        rrule_prop = component.get("RRULE")
        if rrule_prop is not None:
            rrule_str = rrule_prop.to_ical().decode()
            if has_occurrence_after(rrule_str, anchor, cutoff - timedelta(seconds=1)) is False:
                continue
        elif anchor is not None and (anchor >= window_end or anchor < cutoff):
            # one-off reminder outside the window
            continue

        rid = None
        rid_prop = component.get("RECURRENCE-ID")
        if rid_prop is not None:
            rid = _unwrap_dt(rid_prop.dt if hasattr(rid_prop, "dt") else rid_prop)
            if rid is not None and isinstance(rid, datetime) and rid.tzinfo is None:
                rid = rid.replace(tzinfo=timezone.utc)

        # VALARM read-back: a relative TRIGGER means a lead-time offset
        # before DUE (timed); an absolute TRIGGER means a time on the DUE
        # date (all-day). Persist as alarm_offset_seconds / alarm_time.
        alarm_offset_seconds = None
        alarm_time = None
        for _sub in component.subcomponents:
            if _sub.name != "VALARM":
                continue
            _trig = _sub.get("TRIGGER")
            if _trig is None:
                continue
            if isinstance(_trig, timedelta):
                alarm_offset_seconds = int(-_trig.total_seconds())
            elif isinstance(_trig, datetime):
                alarm_time = _trig.strftime("%H:%M")
            elif isinstance(_trig, date_type):
                alarm_time = "09:00"
            break
        rec = {
            "uid": uid,
            "summary": summary,
            "description": description,
            "location": location,
            "start": start_dt.isoformat() if start_dt else None,
            "due": due_dt.isoformat() if due_dt else None,
            "all_day": 1 if all_day else 0,
            "status": status,
            "priority": priority_int,
            "rrule": rrule_str,
            "exdates": json.dumps(_exdate_values(component)) if rrule_str else None,
            "href": href,
            "alarm_offset_seconds": alarm_offset_seconds,
            "alarm_time": alarm_time,
            "tzid": tzid,
        }
        if rid is not None:
            rec["recurrence_id"] = rid.isoformat()
            overrides.append(rec)
        else:
            reminders.append(rec)

    return {"reminders": reminders, "overrides": overrides}


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
        client = _dav_client(url, username, password)
        principal = client.principal()
        calendars = principal.calendars()
    except Exception:
        try:
            client = _dav_client(url, username, password)
            calendars = client.get_calendars()
        except Exception as e:
            logger.error("Failed to discover calendars at %s: %s", url, e)
            return []

    result = []
    for cal in calendars:
        try:
            cal_url = str(cal.url)
            props = cal.get_properties()
            # Only skip a calendar we cannot use at all -- one whose supported
            # component set advertises neither VEVENT (events) nor VTODO
            # (reminders). Task calendars (VTODO-only) must be included now that
            # the sync pulls reminders from them.
            comp_set = props.get("{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set")
            if comp_set and "VEVENT" not in comp_set and "VTODO" not in comp_set:
                logger.info("Skipping unsupported calendar: %s (%s)", cal_url, comp_set)
                continue
            result.append({
                "uid": cal_url.rsplit("/", 1)[-1] or cal_url,
                "display_name": cal.get_display_name() or "Calendar",
                "url": cal_url,
                "color": props.get("{http://apple.com/ns/ical/}calendar-color"),
                "principal_url": (str(principal.url) if principal.url else None),
                "writable": _check_writable(client, cal_url),
                "supports_todo": 1 if (comp_set and "VTODO" in comp_set) else 0,
            })
        except Exception as e:
            logger.warning("Skipping calendar %s: %s", cal_url, e)

    return result


def calendar_is_deletable(url: str | None, principal_url: str | None) -> bool:
    """Ownership gate for calendar deletion.

    A calendar may be deleted only when it lives in the signed-in user's own
    namespace: the /calendars/<owner>/<slug> path segment of the calendar URL
    must have the same <owner> as the principal URL, and the slug must not be
    a shared collection (our server names those *_shared_by_*). Shared
    calendars are read-only for us even though we hold credentials.
    """
    m = re.search(r"/calendars/([^/]+)/([^/]+)", (url or "").rstrip("/"))
    if not m:
        return False
    owner, slug = m.group(1), m.group(2)
    pm = re.search(r"/principals/(?:users/)?([^/]+)", principal_url or "")
    if pm:
        principal = pm.group(1)
    else:
        pm2 = re.search(r"/calendars/([^/]+)", (principal_url or "").rstrip("/"))
        principal = pm2.group(1) if pm2 else None
    if principal != owner:
        return False
    return "shared_by" not in slug.lower()


def delete_calendar(
    conn: Any,
    calendar_id: int,
) -> dict[str, Any]:
    """Server-side DELETE of a calendar collection (like event delete).

    Ownership-gated via calendar_is_deletable(); the server is the final
    arbiter. Removes the collection and all its events for everyone
    subscribed, then drops the DB cache row.

    Subscribed (URL-added) rows are the exception: the collection belongs to
    someone else, so "delete" means UNSUBSCRIBE — drop the local cache rows
    only, never touch the server.
    """
    row = conn.execute(
        "SELECT id, url, principal_url, uid, display_name, subscribed, provider FROM calendars WHERE id = ?",
        (calendar_id,),
    ).fetchone()
    if not row:
        raise ValueError("Calendar not found")
    if row["provider"] == "google":
        from omacal.oauth import oauth_token
        from omacal.google_cal import delete_calendar as gc_delete_cal, GoogleAPIError
        gcal_id = (row["url"] or "")[len("google:"):]
        token = oauth_token(load_config(), "google")
        if not token:
            raise ValueError('Google OAuth consent needed (run "omacal oauth login google")')
        try:
            gc_delete_cal(token, gcal_id)
        except GoogleAPIError as e:
            raise ValueError(f"Google refused to delete the calendar: {e}") from e
        conn.execute("DELETE FROM calendars WHERE id = ?", (calendar_id,))
        conn.execute("DELETE FROM events WHERE calendar_id = ?", (calendar_id,))
        conn.execute("DELETE FROM reminders WHERE calendar_id = ?", (calendar_id,))
        conn.commit()
        logger.info("Deleted Google calendar %r (%s)", row["display_name"], gcal_id)
        return {"ok": True, "calendar_id": calendar_id, "display_name": row["display_name"]}
    if row["subscribed"]:
        # Unsubscribe: local-only removal (see docstring).
        conn.execute("DELETE FROM calendars WHERE id = ?", (calendar_id,))
        conn.execute("DELETE FROM events WHERE calendar_id = ?", (calendar_id,))
        conn.execute("DELETE FROM reminders WHERE calendar_id = ?", (calendar_id,))
        conn.commit()
        return {"ok": True, "calendar_id": calendar_id, "display_name": row["display_name"], "unsubscribed": True}
    if not calendar_is_deletable(row["url"], row["principal_url"]):
        raise ValueError("Calendar is shared/read-only and cannot be deleted")

    cfg = load_config()
    entry = next((c for c in cfg["calendars"] if c.get("username")), None)
    if not entry:
        raise ValueError("No signed-in calendar account")
    url, username = entry["url"], entry["username"]
    from omacal.keyring_store import get_password
    password = get_password(entry.get("principal_url") or url, username)
    if not password:
        raise ValueError("No credentials on file for the signed-in account")

    client = _dav_client(url, username, password)
    principal = client.principal()
    target = None
    for cal in principal.calendars():
        if str(cal.url).rstrip("/") == str(row["url"]).rstrip("/"):
            target = cal
            break
    if target is None:
        # Not on the server (stale cache): just drop the DB row.
        conn.execute("DELETE FROM calendars WHERE id = ?", (calendar_id,))
        conn.commit()
        return {"ok": True, "calendar_id": calendar_id, "display_name": row["display_name"]}

    target.delete()   # CalendarSet.delete: DELETE on the collection URL
    conn.execute("DELETE FROM calendars WHERE id = ?", (calendar_id,))
    conn.execute("DELETE FROM events WHERE calendar_id = ?", (calendar_id,))
    conn.execute("DELETE FROM reminders WHERE calendar_id = ?", (calendar_id,))
    conn.commit()
    return {"ok": True, "calendar_id": calendar_id, "display_name": row["display_name"]}


def edit_calendar(
    conn: Any,
    calendar_id: int,
    name: str | None = None,
    color: str | None = None,
) -> dict[str, Any]:
    """PROPPATCH a calendar's display-name and/or color (owned calendars only).

    Ownership-gated like delete_calendar(): subscribed (URL-added) rows and
    shared collections are refused — renaming/recolouring someone else's
    collection is not ours to do. Writes {DAV:}displayname and the Apple/IIS
    calendar-color property via set_properties (PROPPATCH), then refreshes the
    DB cache row so the panel sees the change without waiting for discovery.
    Raises ValueError for user-facing failures.
    """
    if not name and not color:
        raise ValueError("Nothing to change")
    row = conn.execute(
        "SELECT id, url, principal_url, display_name, subscribed, provider FROM calendars WHERE id = ?",
        (calendar_id,),
    ).fetchone()
    if not row:
        raise ValueError("Calendar not found")
    if row["provider"] == "google":
        # Google rows: PATCH calendarList (summary/colorRgbHex), no keyring.
        from omacal.google_cal import patch_calendar, GoogleAPIError
        from omacal.oauth import oauth_token
        gcal_id = (row["url"] or "")[len("google:"):]
        token = oauth_token(load_config(), "google")
        if not token:
            raise ValueError('Google OAuth consent needed (run "omacal oauth login google")')
        try:
            patch_calendar(token, gcal_id, name, color)
        except GoogleAPIError as e:
            raise ValueError(f"Could not update the calendar: {e}") from e
        if name:
            conn.execute("UPDATE calendars SET display_name = ? WHERE id = ?", (name, calendar_id))
        if color:
            conn.execute("UPDATE calendars SET color = ? WHERE id = ?", (color, calendar_id))
        conn.commit()
        return {"ok": True, "calendar_id": calendar_id,
                "display_name": name or row["display_name"], "color": color}
    if row["subscribed"]:
        raise ValueError("Subscribed calendars can't be edited")
    if not calendar_is_deletable(row["url"], row["principal_url"]):
        raise ValueError("Shared calendar \u2014 can't edit")

    cfg = load_config()
    entry = next((c for c in cfg["calendars"] if c.get("username")), None)
    if not entry:
        raise ValueError("No signed-in calendar account")
    url, username = entry["url"], entry["username"]
    from omacal.keyring_store import get_password
    password = get_password(entry.get("principal_url") or url, username)
    if not password:
        raise ValueError("No credentials on file for the signed-in account")

    client = _dav_client(url, username, password)
    principal = client.principal()
    target = None
    for cal in principal.calendars():
        if str(cal.url).rstrip("/") == str(row["url"]).rstrip("/"):
            target = cal
            break
    if target is None:
        raise ValueError("Calendar not found on the server")

    from caldav.elements import dav

    props = []
    if name:
        props.append(dav.DisplayName(name))
    if color:
        class _CalendarColor(dav.BaseElement):
            tag = "{http://apple.com/ns/ical/}calendar-color"

        props.append(_CalendarColor(value=color))
    try:
        target.set_properties(props)
    except Exception as e:
        raise ValueError(f"Could not update the calendar: {e}") from e

    # Refresh the cache row so the panel sees the change immediately;
    # discovery re-reads the same properties later and agrees.
    if name:
        conn.execute("UPDATE calendars SET display_name = ? WHERE id = ?", (name, calendar_id))
    if color:
        conn.execute("UPDATE calendars SET color = ? WHERE id = ?", (color, calendar_id))
    conn.commit()
    return {
        "ok": True,
        "calendar_id": calendar_id,
        "display_name": name or row["display_name"],
        "color": color,
    }


def add_calendar_named(
    conn: Any,
    name: str,
    color: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Create a NEW named calendar for the signed-in user and cache it.

    Reuses the credentials the user already configured (config's first
    calendar entry: principal URL + username, password from the keyring) —
    no new login. Creates the collection server-side via Principal.make_calendar
    (PUT, RFC 5995), then registers it in the DB cache (enabled=1) so the
    panel's next /api/calendars fetch lists it. `color` (optional, #RRGGBB
    [AA]) is written to the server via PROPPATCH (Apple/IIS calendar-color
    property, the de-facto CalDAV color convention) best-effort — a server
    that rejects the property still gets the calendar, and the chosen color
    is cached in the DB either way so the panel renders it. Returns {ok,
    calendar_id, display_name}; raises ValueError for user-facing failures.

    `provider` ('google' | 'caldav' | None) picks the account explicitly:
    'google' creates on the consented Google account; 'caldav' requires the
    configured CalDAV account; None keeps the presence heuristic (CalDAV if
    an account exists, else Google).
    """
    from omacal.config import load_config
    from omacal.keyring_store import get_password

    cfg = load_config()
    entries = [c for c in cfg.get("calendars", []) if c.get("username")]
    if provider == "google" or not entries:
        # Google path: explicit provider request, or Google-only install.
        if provider == "google" or cfg.get("oauth", {}).get("google"):
            from omacal.oauth import oauth_token
            from omacal.google_cal import create_calendar, GoogleAPIError
            token = oauth_token(cfg, "google")
            if not token:
                raise ValueError('Google OAuth consent needed (run "omacal oauth login google")')
            try:
                cal = create_calendar(token, name, color)
            except GoogleAPIError as e:
                raise ValueError(f"Could not create the calendar: {e}") from e
            gcal_id = cal.get("id") or ""
            cal_id = add_calendar(conn, uid=gcal_id, display_name=cal.get("summary") or name,
                                  url=f"google:{gcal_id}", color=color, provider="google")
            logger.info("Created Google calendar %r (id=%d)", name, cal_id)
            return {"ok": True, "calendar_id": cal_id, "display_name": name}
        raise ValueError("No signed-in calendar server configured")
    entry = entries[0]
    url, username = entry["url"], entry["username"]
    password = get_password(url, username)
    if not password:
        raise ValueError("No credentials on file for the signed-in user")

    client = _dav_client(url, username, password)
    principal = client.principal()
    try:
        cal_obj = principal.make_calendar(name=name)
    except Exception as e:
        raise ValueError(f"Could not create the calendar: {e}") from e

    # Optional color: PROPPATCH the Apple/IIS calendar-color property (the
    # de-facto CalDAV color convention; Thunderbird/Radicale/ICS all honour
    # it). Best-effort: a server that rejects the property still has the
    # calendar, and the DB cache below carries the color for the panel.
    if color:
        try:
            from caldav.elements import dav

            class _CalendarColor(dav.BaseElement):
                tag = "{http://apple.com/ns/ical/}calendar-color"

            cal_obj.set_properties([_CalendarColor(value=color)])
        except Exception as e:
            logger.warning("Could not PROPPATCH calendar color %r: %s", color, e)

    cal_url = str(cal_obj.url)
    props = {}
    try:
        props = cal_obj.get_properties()
    except Exception:
        pass
    comp_set = props.get("{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set")
    uid = cal_url.rsplit("/", 1)[-1] or cal_url
    cal_id = add_calendar(
        conn,
        uid=uid,
        display_name=cal_obj.get_display_name() or name,
        url=cal_url,
        username=username,
        color=props.get("{http://apple.com/ns/ical/}calendar-color") or color,
        principal_url=str(principal.url) if principal.url else url,
        supports_todo=1 if (comp_set and "VTODO" in comp_set) else 0,
    )
    logger.info("Created calendar %r (id=%d) at %s", name, cal_id, cal_url)
    return {"ok": True, "calendar_id": cal_id, "display_name": name}


def subscribe_calendar(
    conn: Any,
    url: str,
    name: str | None = None,
    color: str | None = None,
) -> dict[str, Any]:
    """Subscribe to an EXISTING calendar by its collection URL — no new login.

    The URL is the user's calendar's collection URL (the same kind of URL the
    panel shows in Thunderbird's client, or a public .ics). Auth: if the URL
    lives on the signed-in user's server (same origin as the configured
    account), reuse the keyring credentials; otherwise fetch unauthenticated
    (public calendars). The collection is PROPFINDed for display-name/color/
    component-set, then cached with subscribed=1 so the stale prune never
    removes it and the periodic sync refreshes it directly (sync_subscribed).
    Events are fetched once here via the incremental sync path (no token →
    full replace) so the calendar is populated immediately, not after the
    next poll. Returns {ok, calendar_id, display_name}; raises ValueError
    for user-facing failures.
    """
    from omacal.config import load_config
    from omacal.keyring_store import get_password
    from urllib.parse import urlparse

    url = (url or "").strip()
    if not re.fullmatch(r"https?://\S+", url):
        raise ValueError("Calendar URL must start with http:// or https://")

    # Same-origin as the signed-in account → reuse keyring creds; otherwise
    # unauthenticated (public subscription).
    cfg = load_config()
    username: str | None = None
    password: str | None = None
    account = next((c for c in cfg.get("calendars", []) if c.get("username")), None)
    if account:
        def _origin(u: str) -> tuple:
            p = urlparse(u)
            return (p.scheme.lower(), p.netloc.lower())
        if _origin(url) == _origin(account["url"]):
            username = account["username"]
            password = get_password(account["url"], username)
            if not password:
                username = password = None  # no creds on file: try public

    try:
        client = _dav_client(url, username, password)
        cal_obj = client.calendar(url=url)
    except Exception as e:
        raise ValueError(f"Could not open the calendar at that URL: {e}") from e

    cal_url = str(cal_obj.url)
    props: dict = {}
    try:
        props = cal_obj.get_properties()
    except Exception:
        pass
    display = (name or "").strip() or cal_obj.get_display_name() or cal_url.rsplit("/", 1)[-1] or "Calendar"
    comp_set = props.get("{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set")
    cal_color = props.get("{http://apple.com/ns/ical/}calendar-color") or color

    uid = cal_url.rsplit("/", 1)[-1] or cal_url
    cal_id = add_calendar(
        conn,
        uid=uid,
        display_name=display,
        url=cal_url,
        username=username,
        color=cal_color,
        principal_url=None,
        supports_todo=1 if (comp_set and "VTODO" in comp_set) else 0,
        subscribed=1,
    )

    # Populate the cache now (not after the next poll): incremental sync with
    # no token = full replace of this calendar's rows.
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    try:
        n, dels = _sync_calendar_incremental(conn, client, cal_id, cal_url, display, cutoff)
    except Exception as e:
        logger.warning("Subscribe %r: initial fetch failed (%s); row cached, periodic sync retries", display, e)
        n = 0
    logger.info("Subscribed calendar %r (id=%d) at %s (%d events)", display, cal_id, cal_url, n)
    return {"ok": True, "calendar_id": cal_id, "display_name": display}


def sync_subscribed(conn: Any) -> None:
    """Refresh subscribed (URL-added) calendars directly.

    They are not in config's sources, so sync_all's per-source loop never
    reaches them; this closes that gap on every periodic sync. A failure is
    logged and swallowed — a dead subscription must not fail the whole sync.
    """
    from omacal.keyring_store import get_password

    rows = conn.execute(
        "SELECT id, url, username, display_name FROM calendars WHERE subscribed = 1 AND enabled = 1"
    ).fetchall()
    if not rows:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    for row in rows:
        username = row["username"]
        password = get_password(row["url"], username) if username else None
        try:
            client = _dav_client(row["url"], username, password)
            _sync_calendar_incremental(conn, client, row["id"], row["url"], row["display_name"], cutoff)
        except Exception as e:
            logger.warning("Refresh of subscribed calendar %r failed: %s", row["display_name"], e)


def _sync_calendar_incremental(
    conn: Any,
    client: Any,
    cal_id: int,
    cal_url: str,
    display: str,
    cutoff: datetime,
) -> tuple[int, int]:
    """Incremental sync of one calendar via RFC 6578 sync-collection.

    Returns (events stored, deletions applied). Uses the stored sync token; an
    unknown/invalid token makes the server return everything (safe full
    fallback). Deletions come back as hrefs, which are matched to cached rows
    by the events.href column (the filename UID differs from the iCal UID).
    """
    from omacal.db import get_sync_token, set_sync_token, set_last_sync

    cal_obj = client.calendar(url=cal_url)
    token = get_sync_token(conn, cal_id)

    # Build the sync-collection REPORT (getetag + calendardata inline). With no
    # token the server returns everything (full) and hands back a token; with a
    # valid token it returns only what changed since it.
    body = client._build_sync_collection_body(sync_token=token, props=None)
    resp, _ = cal_obj._request_report_build_resultlist(body, None, None, no_calendardata=False)
    res = resp.parse_sync_collection()

    events = []
    overrides = []
    reminders = []
    reminder_overrides = []
    for ch in res.changed:
        if not ch.calendar_data:
            continue
        data = ch.calendar_data.encode("utf-8")
        try:
            # Events and reminders (VTODO) live in the same change stream; a
            # resource is one component type, so each parser sees its own.
            ep = _parse_ical_events(data, cal_id, cutoff, href=ch.href)
            events.extend(ep["events"])
            overrides.extend(ep["overrides"])
        except Exception as e:
            logger.warning("Skipping changed event in %s: %s", display, e)
        try:
            rp = _parse_ical_reminders(data, cal_id, cutoff, href=ch.href)
            reminders.extend(rp["reminders"])
            reminder_overrides.extend(rp["overrides"])
        except Exception as e:
            logger.warning("Skipping changed reminder in %s: %s", display, e)

    cur = conn.cursor()
    try:
        if token is None:
            # First sync (or a lost token): the server returned everything but
            # no deletion history, so do a full replace -- otherwise rows
            # deleted before this token existed would linger in the cache.
            for tbl in ("events", "event_overrides", "reminders", "reminder_overrides"):
                cur.execute(f"DELETE FROM {tbl} WHERE calendar_id=?", (cal_id,))
        elif res.deleted:
            placeholders = ",".join("?" for _ in res.deleted)
            for tbl in ("events", "event_overrides", "reminders", "reminder_overrides"):
                cur.execute(
                    f"DELETE FROM {tbl} WHERE calendar_id=? AND href IN ({placeholders})",
                    [cal_id] + res.deleted,
                )
        if events:
            upsert_events(conn, cal_id, events, commit=False)
        if overrides:
            upsert_overrides(conn, cal_id, overrides, commit=False)
        if reminders:
            upsert_reminders(conn, cal_id, reminders, commit=False)
        if reminder_overrides:
            upsert_reminder_overrides(conn, cal_id, reminder_overrides, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    set_sync_token(conn, cal_id, res.sync_token)
    set_last_sync(conn, cal_id)
    return len(events) + len(reminders), len(res.deleted)


def sync_google_source(conn: Any, cfg: dict, cal_entry: dict) -> tuple[int, list[str], list[str]]:
    """Sync one configured Google provider source (Calendar API v3, not DAV).

    Config entry: {"provider": "google", "display_name": "Google"} — no URL
    needed: the account is the OAuth token set, discovery is the user's
    calendar list. Mirrors sync_source's contract: returns
    (events_stored, urls_replaced, failed_calendar_ids) and raises on a
    source-level failure (no consent / discovery failure) so the caller
    skips the stale prune. Reminders (Google Tasks) are a separate API —
    out of scope for this adapter.

    Google hands back recurring series INSTANCES (no master RRULE is exposed),
    so instances cache as one-off rows and the fetch window is ±WINDOW_DAYS
    (the panel's navigation), not the CalDAV [-30, +90] days.
    """
    from omacal.oauth import oauth_token, OAuthReauthRequired
    from omacal.google_cal import (list_user_calendars, list_events,
                                   to_cache_event, is_deleted, google_url,
                                   WINDOW_DAYS, SyncTokenExpired, GoogleAPIError)
    from omacal.db import (add_calendar, get_sync_token, set_sync_token,
                           set_last_sync, upsert_events)

    try:
        token = oauth_token(cfg, "google")
    except OAuthReauthRequired as e:
        # Refresh token dead: surface like a source-level failure; the
        # caller reports error and the stale prune is skipped.
        raise ValueError(f"Google re-consent needed: {e}") from None
    if not token:
        raise ValueError('Google OAuth consent needed (run "omacal oauth login google")')

    try:
        gcal_list = list_user_calendars(token)
    except (GoogleAPIError, SyncTokenExpired) as e:
        raise ValueError(f"Google calendar discovery failed: {e}") from None

    now = datetime.now(timezone.utc)
    time_min = now - timedelta(days=WINDOW_DAYS)
    time_max = now + timedelta(days=WINDOW_DAYS)

    total = 0
    urls: list[str] = []
    failed: list[str] = []

    for gc in gcal_list:
        cal_id = add_calendar(
            conn,
            uid=gc["id"],
            display_name=gc["display_name"],
            url=google_url(gc["id"]),
            color=gc["color"],
            provider="google",
        )
        stored = get_sync_token(conn, cal_id)
        try:
            items, new_token = list_events(token, gc["id"], time_min, time_max,
                                           sync_token=stored)
        except SyncTokenExpired:
            # Dead token: retry this calendar as a full fetch (no token),
            # which replaces the whole cache set for it.
            logger.warning("Google syncToken dead for %s; full fetch", gc["id"])
            stored = None
            try:
                items, new_token = list_events(token, gc["id"], time_min, time_max,
                                               sync_token=None)
            except GoogleAPIError as e:
                logger.error("Google full fetch failed for %s: %s", gc["id"], e)
                failed.append(gc["id"])
                continue
        except GoogleAPIError as e:
            logger.error("Google fetch failed for %s: %s", gc["id"], e)
            failed.append(gc["id"])
            continue

        changed: list[dict] = []
        deleted_uids: list[str] = []
        for it in items:
            if is_deleted(it):
                deleted_uids.append(it["id"])
                continue
            row = to_cache_event(it)
            if row:
                changed.append(row)

        # Apply atomically per calendar, mirroring _sync_calendar_incremental:
        # no token means the fetch was full (wipe-then-insert, so instances
        # deleted before the new token existed cannot linger); with a token
        # deletions come back as flagged items matched by uid (href == uid on
        # Google rows, so either column works).
        cur = conn.cursor()
        try:
            if stored is None:
                cur.execute("DELETE FROM events WHERE calendar_id=?", (cal_id,))
                cur.execute("DELETE FROM event_overrides WHERE calendar_id=?", (cal_id,))
            elif deleted_uids:
                placeholders = ",".join("?" for _ in deleted_uids)
                cur.execute(
                    f"DELETE FROM events WHERE calendar_id=? AND uid IN ({placeholders})",
                    [cal_id] + deleted_uids,
                )
            if changed:
                upsert_events(conn, cal_id, changed, commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            failed.append(gc["id"])
            logger.error("Google cache apply failed for %s", gc["id"])
            continue

        set_sync_token(conn, cal_id, new_token)
        set_last_sync(conn, cal_id)
        urls.append(google_url(gc["id"]))
        total += len(changed)

    return total, urls, failed


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
        client = _dav_client(url, username, password)
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
            supports_todo=cal_info.get("supports_todo", 0),
        )

        # One sync-collection REPORT per calendar: full replace on the first
        # run (no token), incremental afterwards. Applies atomically and a
        # failed fetch leaves the previous cache untouched.
        try:
            n, dels = _sync_calendar_incremental(conn, client, cal_id, cal_info["url"], display, cutoff)
            total_events += n
            urls_synced.append(cal_info["url"])
            logger.info("Synced %s: %d events, %d deleted", display, n, dels)
        except Exception as e:
            logger.error("Sync failed for %s: %s", display, e)
            urls_failed.append(cal_info["url"])
            continue

    return total_events, urls_synced, urls_failed


def _google_row(conn, calendar_id: int) -> tuple[str, str] | None:
    """(google_calendar_id, access_token) if the cached calendar is Google-backed, else None.

    Write-path dispatch: google rows carry url="google:<id>" and provider="google";
    the access token comes from the OAuth token set (raises OAuthReauthRequired
    when the refresh token died -- callers surface it as an error).
    """
    from omacal.oauth import oauth_token

    row = conn.execute("SELECT url, provider FROM calendars WHERE id = ?",
                       (calendar_id,)).fetchone()
    if not row or row["provider"] != "google":
        return None
    gcal_id = (row["url"] or "")[len("google:"):]
    token = oauth_token(load_config(), "google")
    if not token:
        raise ValueError('Google OAuth consent needed (run "omacal oauth login google")')
    return gcal_id, token


def create_event(
    conn: Any,
    calendar_id: int,
    summary: str,
    start: datetime,
    end: datetime | None = None,
    all_day: bool = False,
    location: str | None = None,
    description: str | None = None,
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
    g = _google_row(conn, calendar_id)
    if g:
        from omacal.google_cal import create_event as gc_create
        from omacal.db import add_event

        gcal_id, token = g
        created = gc_create(token, gcal_id, summary, start, end, all_day,
                            location, description, rrule)
        uid = created.get("id")
        if not uid:
            raise ValueError("Google create returned no event id")
        if all_day:
            sd = start.date() if isinstance(start, datetime) else start
            ed = end.date() if isinstance(end, datetime) else end
            if ed is None or ed <= sd:
                ed = sd + timedelta(days=1)
            start_iso = datetime(sd.year, sd.month, sd.day, tzinfo=timezone.utc).isoformat()
            end_iso = datetime(ed.year, ed.month, ed.day, tzinfo=timezone.utc).isoformat()
        else:
            if end is None:
                end = start + timedelta(hours=1)
            start_iso, end_iso = start.isoformat(), end.isoformat()
        # Cache under Google's own id so later edits/deletes address the same
        # resource; recurring series land as instances on the next sync.
        return add_event(conn, calendar_id, uid, summary, start_iso, end_iso,
                         1 if all_day else 0, location, description=description,
                         rrule=rrule, tzid=_tzid_of(start, start))

    from uuid import uuid4

    from omacal.db import add_event

    # Look up calendar URL and credentials (password from the keyring)
    url, username, password = _calendar_creds(conn, calendar_id)

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
    client = _dav_client(url, username, password)
    cal_obj = client.calendar(url=url)
    cal_obj.add_event(summary=summary, uid=uid, location=location, description=description, **push_kwargs)

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
        description=description,
        rrule=rrule,
        tzid=_tzid_of(start, start),
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
    g = _google_row(conn, calendar_id)
    if g:
        from omacal.google_cal import delete_event as gc_delete, get_event as gc_get, GoogleAPIError
        from omacal.db import delete_event as db_delete_event

        gcal_id, token = g
        try:
            res = gc_get(token, gcal_id, uid)
        except GoogleAPIError as e:
            raise ValueError(f"event not found on Google calendar: {e}") from None
        master_id = res.get("recurringEventId")
        # No guard: the uid of a Google row IS the occurrence, so a bare
        # delete removes exactly that instance; series=true targets the master.
        if master_id and series:
            gc_delete(token, gcal_id, master_id)
            # Our cache stores each instance as its own row (uid = <master>_<stamp>);
            # deleting the master on Google kills every instance, so drop them all.
            conn.execute("DELETE FROM events WHERE calendar_id = ? AND (uid = ? OR uid LIKE ?)",
                         (calendar_id, master_id, master_id + "_%"))
            conn.commit()
            return {"status": "deleted", "scope": "series", "uid": uid}
        # Instance id = that occurrence only; plain id = the whole one-off event.
        gc_delete(token, gcal_id, uid)
        db_delete_event(conn, calendar_id, uid)
        return {"status": "deleted",
                "scope": "occurrence" if occurrence is not None else "event",
                "uid": uid}

    from omacal.db import delete_event as db_delete_event, set_event_exdates
    from omacal.recur import parse_dt

    url, username, password = _calendar_creds(conn, calendar_id)

    client = _dav_client(url, username, password)
    cal_obj = client.calendar(url=url)
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
    description: str | None = None,
    occurrence: datetime | None = None,
) -> dict[str, Any]:
    """Update an existing event on the CalDAV server and local cache.

    occurrence=None edits the whole event (or moves it between calendars).
    occurrence=<dt> edits just that one occurrence of a recurring series by
    writing a detached RECURRENCE-ID override (the same mechanism the phone
    uses), leaving the rest of the series untouched.
    """
    g = _google_row(conn, calendar_id)
    if g:
        from omacal.google_cal import (patch_event as gc_patch, get_event as gc_get,
                                       move_event as gc_move, GoogleAPIError)
        from omacal.db import add_event as db_add_event

        gcal_id, token = g
        try:
            probe = gc_get(token, gcal_id, uid)
            on_target = True
        except GoogleAPIError:
            probe = None
            on_target = False
        if not on_target:
            # uid not on the target calendar: google->google uses the move
            # endpoint (keeps the id); cross-provider moves are refused.
            row = conn.execute("SELECT calendar_id FROM events WHERE uid = ?", (uid,)).fetchone()
            old_id = row["calendar_id"] if row else None
            old = _google_row(conn, old_id) if old_id is not None else None
            if old:
                gc_move(token, old[0], uid, gcal_id)
            else:
                raise ValueError("moving events between providers is not supported yet")
        # Google rows are instances: occurrence=<dt> edits just that instance;
        # no occurrence means the whole series, which targets the master.
        target = uid
        if occurrence is None and probe and probe.get("recurringEventId"):
            target = probe["recurringEventId"]
        gc_patch(token, gcal_id, target, summary=summary, start=start, end=end,
                 all_day=all_day, location=location, description=description)
        prev = conn.execute(
            "SELECT rrule, exdates, status, transparency FROM events WHERE uid = ? ORDER BY start DESC LIMIT 1", (uid,)
        ).fetchone()
        conn.execute("DELETE FROM events WHERE uid = ?", (uid,))
        conn.commit()
        return db_add_event(
            conn, calendar_id, uid, summary,
            start.isoformat(), end.isoformat() if end else None,
            1 if all_day else 0, location, description=description,
            status=prev["status"] if prev else "CONFIRMED",
            transparency=prev["transparency"] if prev else None,
            rrule=prev["rrule"] if prev else None,
            exdates=prev["exdates"] if prev else None,
            tzid=_tzid_of(start, start),
        )

    from omacal.db import update_event as db_update_event

    url, username, password = _calendar_creds(conn, calendar_id)

    client = _dav_client(url, username, password)
    cal_obj = client.calendar(url=url)
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
            description=description,
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
            description=description,
            tzid=_tzid_of(start, start),
        )
    # --- Per-occurrence edit: write a detached RECURRENCE-ID override ---
    if occurrence is not None:
        from omacal.recur import parse_dt
        from omacal.db import upsert_overrides
        from icalendar import vDatetime, vDate, Event

        comp = Calendar.from_ical(event.data)
        masters = [c for c in comp.walk("VEVENT") if not c.get("RECURRENCE-ID")]
        master = masters[0] if masters else None
        if master is None or master.get("RRULE") is None:
            raise ValueError("occurrence given but this event does not repeat")

        target = parse_dt(occurrence)
        if target is None:
            raise ValueError(f"invalid occurrence value: {occurrence!r}")

        # RECURRENCE-ID must mirror the master's DTSTART form so other clients
        # (and this parser) match it: DATE for all-day, same tz otherwise.
        mstart = master.get("DTSTART").dt
        if not hasattr(mstart, "hour"):                 # all-day series
            rid_value = target.date()
        elif mstart.tzinfo is not None:
            rid_value = target.astimezone(mstart.tzinfo)
        else:
            rid_value = target.replace(tzinfo=None)

        # Build the detached override VEVENT.
        ov = Event()
        ov.add("uid", uid)
        ov.add("recurrence-id", rid_value)
        ov.add("summary", summary)
        if description:
            ov.add("description", description)
        if location:
            ov.add("location", location)
        if all_day:
            sd = start.date() if isinstance(start, datetime) else start
            ed = end.date() if isinstance(end, datetime) else end
            if ed is None or ed <= sd:
                ed = sd + timedelta(days=1)
            ov.add("dtstart", vDate(sd), parameters={"VALUE": "DATE"})
            ov.add("dtend", vDate(ed), parameters={"VALUE": "DATE"})
        else:
            ov.add("dtstart", vDatetime(start))
            if end is not None:
                ov.add("dtend", vDatetime(end))
        comp.add_component(ov)
        event.data = comp.to_ical().decode()
        event.save()

        # Cache the override so the panel reflects it immediately (expansion
        # applies overrides from event_overrides). Upsert on (cal, uid, rid).
        upsert_overrides(conn, calendar_id, [{
            "uid": uid,
            "recurrence_id": target.isoformat(),
            "summary": summary,
            "description": description,
            "location": location,
            "start": start.isoformat(),
            "end": end.isoformat() if end else None,
            "all_day": 1 if all_day else 0,
            "status": "CONFIRMED",
            "href": None,
            "tzid": _tzid_of(start, start),
        }])
        return {"status": "updated", "scope": "occurrence", "uid": uid,
                "occurrence": target.isoformat()}

    comp = event.icalendar_component
    comp["summary"] = summary
    if description:
        comp["description"] = description
    else:
        comp.pop("description", None)
    from icalendar import vDatetime, vDate
    # Pop DTSTART/DTEND before re-adding. Assigning to an existing key keeps the
    # old property's params, so converting a timed event to all-day left a stale
    # VALUE=DATE-TIME on a DATE value and SabreDAV rejected it ("20260921 is not
    # a correct DATE-TIME", HTTP 415). Re-adding with the explicit VALUE=DATE
    # param emits DTSTART;VALUE=DATE:... correctly.
    comp.pop("DTSTART", None)
    comp.pop("DTEND", None)
    if all_day:
        start_date = start.date() if isinstance(start, datetime) else start
        end_date = None
        if end is not None:
            end_date = end.date() if isinstance(end, datetime) else end
        if end_date is None or end_date <= start_date:
            end_date = start_date + timedelta(days=1)
        comp.add("dtstart", vDate(start_date), parameters={"VALUE": "DATE"})
        comp.add("dtend", vDate(end_date), parameters={"VALUE": "DATE"})
    else:
        comp.add("dtstart", vDatetime(start))
        if end is not None:
            comp.add("dtend", vDatetime(end))
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
        "SELECT rrule, exdates, status, transparency FROM events WHERE uid = ? ORDER BY start DESC LIMIT 1", (uid,)
    ).fetchone()
    prev_rrule = prev["rrule"] if prev else None
    prev_exdates = prev["exdates"] if prev else None
    prev_status = prev["status"] if prev else "CONFIRMED"
    prev_transparency = prev["transparency"] if prev else None
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
        description=description,
        status=prev_status,
        transparency=prev_transparency,
        rrule=prev_rrule,
        exdates=prev_exdates,
        tzid=_tzid_of(start, start),
    )


def _todo_due_value(due: datetime, all_day: bool):
    """The DUE value to push to the server: DATE for all-day, datetime otherwise."""
    if all_day:
        return due.date() if isinstance(due, datetime) else due
    return due


def _parse_alarm_time(alarm_time: str | None) -> tuple[int, int]:
    """Split 'HH:MM' into (hour, minute); default 09:00."""
    if alarm_time and ":" in alarm_time:
        try:
            h, m = alarm_time.split(":", 1)
            return max(0, min(23, int(h))), max(0, min(59, int(m)))
        except ValueError:
            pass
    return 9, 0


def _set_valarm(comp, due: datetime, all_day: bool, alarm_offset: int | None, alarm_time: str | None, summary: str):
    """Set (or replace) the VALARM on a VTODO component.

    Timed: relative TRIGGER before DUE (alarm_offset seconds, default 3600).
    All-day: absolute TRIGGER;VALUE=DATE-TIME at alarm_time on the DUE date
    (a relative 1h-before-midnight would fire the previous evening).
    """
    for sub in [s for s in comp.subcomponents if s.name == "VALARM"]:
        comp.subcomponents.remove(sub)
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", summary or "Reminder")
    if all_day:
        d = due.date() if isinstance(due, datetime) else due
        hh, mm = _parse_alarm_time(alarm_time)
        local_tz = datetime.now().astimezone().tzinfo
        dt = datetime.combine(d, time(hh, mm), tzinfo=local_tz)
        alarm.add("trigger", vDatetime(dt), parameters={"VALUE": "DATE-TIME"})
    else:
        offset = alarm_offset if alarm_offset is not None else 3600
        alarm.add("trigger", timedelta(seconds=-offset))
    comp.add_component(alarm)


def reminder_alarm_instant(row: dict) -> datetime | None:
    """The instant this reminder occurrence's alarm should fire (aware datetime).

    None when the reminder carries no alarm (both alarm fields NULL -- a legacy
    reminder created before alarms existed).
    """
    due_iso = row.get("due")
    if not due_iso:
        return None
    due = datetime.fromisoformat(str(due_iso).replace("Z", "+00:00"))
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    if row.get("all_day"):
        alarm_time = row.get("alarm_time")
        if not alarm_time:
            return None
        hh, mm = _parse_alarm_time(alarm_time)
        return datetime.combine(due.date(), time(hh, mm), tzinfo=datetime.now().astimezone().tzinfo)
    offset = row.get("alarm_offset_seconds")
    if offset is None:
        return None
    return due - timedelta(seconds=int(offset))



def create_reminder(
    conn: Any,
    calendar_id: int,
    summary: str,
    due: datetime,
    start: datetime | None = None,
    all_day: bool = False,
    location: str | None = None,
    description: str | None = None,
    rrule: str | None = None,
    alarm_offset: int | None = None,
    alarm_time: str | None = None,
) -> dict[str, Any]:
    """Create a reminder (VTODO) on the CalDAV server and cache it locally.

    DUE is the fire/display date (a reminder has no DTEND). DTSTART is optional
    and defaults to DUE, matching how iOS writes tasks (both set to the same
    instant). all_day stores DATE-valued DUE/DTSTART. A VALARM carries the
    heads-up: timed reminders use a relative offset before DUE (alarm_offset,
    default 3600 = 1h); all-day reminders use an absolute time on the DUE date
    (alarm_time "HH:MM", default 09:00). Pushes first; only caches if the
    server accepts it. Returns the cached row as a dict.
    """
    if _google_row(conn, calendar_id):
        raise ValueError("reminders on Google calendars use the Tasks API (not wired yet)")

    from uuid import uuid4

    from omacal.db import add_reminder

    url, username, password = _calendar_creds(conn, calendar_id)

    if all_day and not alarm_time:
        alarm_time = "09:00"

    uid = str(uuid4())
    push_kwargs: dict[str, Any] = {
        "uid": uid,
        "summary": summary,
        "due": _todo_due_value(due, all_day),
        "status": "NEEDS-ACTION",
    }
    if start is not None:
        push_kwargs["dtstart"] = _todo_due_value(start, all_day)
    else:
        push_kwargs["dtstart"] = _todo_due_value(due, all_day)
    if location:
        push_kwargs["location"] = location
    if description:
        push_kwargs["description"] = description
    if rrule:
        push_kwargs["rrule"] = rrule

    client = _dav_client(url, username, password)
    cal_obj = client.calendar(url=url)
    todo = cal_obj.add_todo(**push_kwargs)
    # Add the VALARM on the returned component -- caldav's add_alarm can't emit
    # the all-day TRIGGER;VALUE=DATE-TIME form, and the timed default (1h) has
    # to be made explicit.
    comp = todo.icalendar_component
    _set_valarm(comp, due, all_day, alarm_offset, alarm_time, summary)
    todo.data = comp.to_ical().decode()
    todo.save()

    due_iso = due.replace(tzinfo=timezone.utc).isoformat() if all_day and isinstance(due, datetime) else due.isoformat()
    start_iso = None
    if start is not None:
        start_iso = start.replace(tzinfo=timezone.utc).isoformat() if all_day and isinstance(start, datetime) else start.isoformat()

    cached = add_reminder(
        conn, calendar_id, uid, summary,
        due=due_iso, start=start_iso,
        all_day=1 if all_day else 0,
        location=location, description=description,
        status="NEEDS-ACTION",
        rrule=rrule,
        tzid=_tzid_of(due, due),
        alarm_offset_seconds=None if all_day else (alarm_offset if alarm_offset is not None else 3600),
        alarm_time=alarm_time if all_day else None,
    )
    return cached
def delete_reminder(conn: Any, calendar_id: int, uid: str) -> dict[str, Any]:
    """Delete a reminder (VTODO) from the CalDAV server and local cache.

    Whole-reminder delete (the task, and its series, is removed) -- matches how
    you delete a task. Per-occurrence delete can be added later if needed.
    """
    if _google_row(conn, calendar_id):
        raise ValueError("reminders on Google calendars use the Tasks API (not wired yet)")

    from omacal.db import delete_reminder as db_delete_reminder

    url, username, password = _calendar_creds(conn, calendar_id)
    client = _dav_client(url, username, password)
    cal_obj = client.calendar(url=url)

    todo = None
    try:
        todo = cal_obj.get_todo_by_uid(uid)
    except caldav.error.NotFoundError:
        pass
    if todo is None:
        # Search every calendar on this server, mirroring update_event's move.
        principal = client.principal()
        for cal in principal.calendars():
            try:
                todo = cal.get_todo_by_uid(uid)
                break
            except caldav.error.NotFoundError:
                continue
    if todo is None:
        raise ValueError(f"Reminder uid={uid} not found on any calendar")

    todo.delete()
    db_delete_reminder(conn, calendar_id, uid)
    return {"status": "deleted", "uid": uid, "scope": "reminder"}


def update_reminder(
    conn: Any,
    calendar_id: int,
    uid: str,
    summary: str,
    due: datetime,
    start: datetime | None = None,
    all_day: bool = False,
    location: str | None = None,
    description: str | None = None,
    alarm_offset: int | None = None,
    alarm_time: str | None = None,
) -> dict[str, Any]:
    """Update an existing reminder (VTODO) on the server and local cache.

    Whole-reminder edit this pass (per-occurrence via RECURRENCE-ID can follow
    later). The resource keeps its RRULE/EXDATE, so the cached row does too.
    """
    if _google_row(conn, calendar_id):
        raise ValueError("reminders on Google calendars use the Tasks API (not wired yet)")

    from omacal.db import add_reminder as db_add_reminder

    url, username, password = _calendar_creds(conn, calendar_id)
    client = _dav_client(url, username, password)
    cal_obj = client.calendar(url=url)

    try:
        todo = cal_obj.get_todo_by_uid(uid)
    except caldav.error.NotFoundError:
        raise ValueError(f"Reminder uid={uid} not found on calendar")

    comp = todo.icalendar_component
    comp["summary"] = summary
    if description:
        comp["description"] = description
    else:
        comp.pop("description", None)
    if location:
        comp["location"] = location
    else:
        comp.pop("location", None)

    from icalendar import vDatetime, vDate
    comp.pop("DTSTART", None)
    comp.pop("DUE", None)
    if all_day:
        due_date = due.date() if isinstance(due, datetime) else due
        comp.add("due", vDate(due_date), parameters={"VALUE": "DATE"})
        if start is not None:
            start_date = start.date() if isinstance(start, datetime) else start
            comp.add("dtstart", vDate(start_date), parameters={"VALUE": "DATE"})
        else:
            comp.add("dtstart", vDate(due_date), parameters={"VALUE": "DATE"})
    else:
        comp.add("due", vDatetime(due))
        if start is not None:
            comp.add("dtstart", vDatetime(start))
        else:
            comp.add("dtstart", vDatetime(due))
    # STATUS must stay NEEDS-ACTION while it's still a live reminder.
    comp.pop("STATUS", None)
    comp["status"] = "NEEDS-ACTION"
    _set_valarm(comp, due, all_day, alarm_offset, alarm_time, summary)
    todo.data = comp.to_ical().decode()
    todo.save()

    prev = conn.execute(
        "SELECT rrule, exdates FROM reminders WHERE uid = ? ORDER BY due DESC LIMIT 1", (uid,)
    ).fetchone()
    prev_rrule = prev["rrule"] if prev else None
    prev_exdates = prev["exdates"] if prev else None
    conn.execute("DELETE FROM reminders WHERE uid = ?", (uid,))
    conn.commit()

    due_iso = due.replace(tzinfo=timezone.utc).isoformat() if all_day and isinstance(due, datetime) else due.isoformat()
    start_iso = None
    if start is not None:
        start_iso = start.replace(tzinfo=timezone.utc).isoformat() if all_day and isinstance(start, datetime) else start.isoformat()

    return db_add_reminder(
        conn, calendar_id, uid, summary,
        due=due_iso, start=start_iso,
        all_day=1 if all_day else 0,
        location=location, description=description,
        status="NEEDS-ACTION",
        rrule=prev_rrule, exdates=prev_exdates,
        tzid=_tzid_of(due, due),
        alarm_offset_seconds=None if all_day else (alarm_offset if alarm_offset is not None else 3600),
        alarm_time=alarm_time if all_day else None,
    )


def _deliver_alarm_toast(summary: str) -> None:
    """Fire the omarchy desktop toast for a due reminder."""
    import subprocess
    try:
        subprocess.run(
            ["omarchy-notification-send", "-g", "\U0001F362", "-u", "normal",
             "Reminder due", summary or "Reminder"],
            check=False, capture_output=True, timeout=10,
        )
    except Exception as e:
        logger.error("Failed to send reminder toast: %s", e)


def check_due_alarms(config_path: Path | None = None, now: datetime | None = None) -> list[dict]:
    """Fire toasts for reminders whose alarm instant has passed and not yet toasted.

    Called on a short timer by the API server. Expands reminders over a wide
    window (an all-day reminder's DUE is midnight but its alarm fires hours
    into the day, so a narrow window would miss it), then toasts each
    occurrence whose alarm fired within the last hour -- so a machine that was
    sleeping still catches recently-due alarms, but a long-dormant reminder
    doesn't flood toasts. Each (calendar, uid, occurrence DUE) is marked fired
    so recurring reminders toast once per occurrence; the fired table is keyed
    on uid+calendar (not the row id) so a full sync that re-inserts rows with
    new ids doesn't re-toast.
    """
    import sqlite3

    from omacal.db import (_migrate, get_reminders_for_window, get_reminder_overrides,
                           mark_alarm_fired, is_alarm_fired)
    from omacal.recur import expand_reminders

    now = now or datetime.now().astimezone()
    grace_start = now - timedelta(hours=1)
    # Wide expand window: covers all-day (DUE at midnight, alarm hours later)
    # and any reasonable lead offset.
    expand_start = now - timedelta(hours=48)
    expand_end = now + timedelta(hours=48)

    cfg = load_config(config_path)
    db_path = Path(cfg["database"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    try:
        masters = get_reminders_for_window(conn, expand_start, expand_end, None)
        if not masters:
            return []
        overrides = get_reminder_overrides(conn, None)
        occurrences = expand_reminders(masters, overrides, expand_start, expand_end)
        fired_rows = []
        for occ in occurrences:
            alarm_at = reminder_alarm_instant(occ)
            if alarm_at is None:
                continue
            if not (alarm_at <= now and alarm_at >= grace_start):
                continue
            cid = occ.get("calendar_id")
            uid = occ.get("uid")
            due = str(occ.get("due") or "")
            if is_alarm_fired(conn, cid, uid, due):
                continue
            _deliver_alarm_toast(occ.get("summary") or "Reminder")
            mark_alarm_fired(conn, cid, uid, due)
            fired_rows.append(occ)
        return fired_rows
    finally:
        conn.close()



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

    # Provider soft-disable (POST /api/account/toggle): an oauth section entry
    # with enabled=false skips its sync source AND protects its cached rows from
    # the stale prune below — a skipped source contributes no URLs to synced_urls,
    # so without this protection the prune would DELETE the disabled provider's
    # calendars and events, turning the soft-disable into a hard one. OAuth key
    # "google" maps to DB provider google; any other oauth key (the configured
    # CalDAV account, e.g. nextcloud) maps to the caldav rows.
    oauth_cfg = cfg.get("oauth", {})
    disabled_providers = {
        ("google" if k == "google" else "caldav")
        for k, v in oauth_cfg.items()
        if not (v or {}).get("enabled", True)
    }

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
        # Provider dispatch (google branch): a config entry with
        # provider="google" syncs over the Calendar API v3 adapter; the
        # CalDAV path stays the default for everything else.
        provider = cal.get("provider", "caldav")
        if provider in disabled_providers:
            results[name] = {"status": "disabled"}
            continue
        try:
            if provider == "google":
                n, urls, failed = sync_google_source(conn, cfg, cal)
            else:
                # Password comes from the keyring, never from config.json.
                from omacal.keyring_store import get_password

                username = cal.get("username")
                password = get_password(cal["url"], username) if username else None
                n, urls, failed = sync_source(
                    conn,
                    cal["url"],
                    username,
                    password,
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

    # Refresh subscribed (URL-added) calendars: they are not config sources,
    # so the loop above never reaches them. Failures are logged, not fatal.
    sync_subscribed(conn)

    if incomplete:
        logger.warning(
            "Skipping stale-calendar cleanup: %d source(s) did not complete (%s)",
            len(incomplete), ", ".join(incomplete),
        )
    else:
        # Remove stale calendar entries that are no longer discovered.
        # Subscribed rows are exempt: they are not part of any source's
        # discovery, so "not in synced_urls" does not mean stale for them.
        cur = conn.execute("SELECT id, url, subscribed, provider FROM calendars")
        stale = [row["id"] for row in cur.fetchall()
                 if not row["subscribed"]
                 and row["url"] not in synced_urls
                 and row["provider"] not in disabled_providers]
        if stale:
            placeholders = ",".join("?" for _ in stale)
            conn.execute(f"DELETE FROM calendars WHERE id IN ({placeholders})", stale)
            conn.execute(f"DELETE FROM events WHERE calendar_id IN ({placeholders})", stale)
            conn.execute(f"DELETE FROM event_overrides WHERE calendar_id IN ({placeholders})", stale)
            conn.commit()
            logger.info("Removed %d stale calendar(s) from cache", len(stale))

    conn.close()
    return {"total_events": total, "calendars": results}







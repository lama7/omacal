"""Google Calendar API v3 adapter (google branch).

Read side for now: list the signed-in user's calendars and fetch their
events into the omacal cache. Write paths (create/update/delete)
come in a later chunk.

Keying rules that keep the cache provider-agnostic:
  * Google calendar ids live in calendars.uid (provider='google' row); the
    cache `url` is "google:<calendarId>" so the stale-prune URL set never
    collides with DAV collection URLs.
  * The API hands back recurring SERIES INSTANCES (recurringEventId points at
    the master) and never exposes the master's RRULE, so instances are cached
    as one-off rows. The fetch window must therefore cover the panel's
    navigation (±12 months), not the CalDAV [-30, +90] days.
  * Quota is handled AT THIS LAYER (per the OAuth design notes): 429/5xx
    retry with backoff here; callers see success or a raised error.
  * An expired syncToken is Google's 410: raise SyncTokenExpired so the
    caller retries that calendar with a full fetch (token cleared).

Event cache mapping mirrors the CalDAV cache conventions:
  * all-day events cache UTC-midnight datetimes (start.date), tzid NULL
  * timed events cache the dateTime (offset baked in) + tzid = the event's
    IANA timeZone, re-attached DST-aware at load (same v6 tzid design)
  * status 'confirmed'/'tentative'/'cancelled' uppercases to the ICS values
"""

from __future__ import annotations

import json
import logging
import time as _time
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("omacal.google_cal")

_API_BASE = "https://www.googleapis.com/calendar/v3"

# Panel navigates ±12 months; Google instances are cached as one-offs, so the
# fetch window must cover that, not the CalDAV [-30, +90] day window.
WINDOW_DAYS = 365

_MAX_PAGED_ITEMS = 2500  # events.list maxResults cap
_CALENDARLIST_MAX = 250  # users/me/calendarList maxResults cap

# Retry budget for quota/5xx at this layer.
_RETRY_ATTEMPTS = 4
_RETRY_MAX_DELAY_S = 60


class SyncTokenExpired(Exception):
    """Google answered 410: the stored syncToken is dead; full-fetch fallback."""


class GoogleAPIError(Exception):
    """A Google API call failed after retries. Message carries detail."""


def _api_request(token: str, path: str, method: str = "GET",
                 params: dict | None = None, body: dict | None = None) -> dict:
    """Call a Google Calendar API v3 path with a Bearer token; return JSON.

    Retries 429/5xx with exponential backoff (honouring Retry-After when
    sane). 410 raises SyncTokenExpired; other failures raise GoogleAPIError.

    Retry safety for writes: 429 (quota) never executed the request, so it
    is retried for every method; 5xx retries are limited to idempotent
    methods (GET/PUT/PATCH/DELETE) — a POST create that 500s is NOT retried
    because it may already have executed server-side (duplicate event).
    """
    url = _API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
    last_err = "unknown"
    for attempt in range(_RETRY_ATTEMPTS):
        req = urllib.request.Request(url, data=payload, method=method)
        if payload is not None:
            req.headers["Content-Type"] = "application/json"
        req.headers["Authorization"] = f"Bearer {token}"
        retryable_5xx = method in ("GET", "PUT", "PATCH", "DELETE")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 410:
                raise SyncTokenExpired(f"{path}: syncToken expired") from None
            codes = (429, 500, 502, 503) if retryable_5xx else (429,)
            if e.code in codes and attempt < _RETRY_ATTEMPTS - 1:
                delay = _RETRY_MAX_DELAY_S
                ra = e.headers.get("Retry-After")
                if ra and ra.isdigit():
                    delay = min(int(ra), _RETRY_MAX_DELAY_S)
                else:
                    delay = min(2 ** attempt * 2, _RETRY_MAX_DELAY_S)
                logger.warning("Google API %d on %s; backing off %ds", e.code, path, delay)
                _time.sleep(delay)
                last_err = f"HTTP {e.code}"
                continue
            last_err = f"HTTP {e.code}"
            try:
                detail = json.loads(e.read().decode("utf-8", "replace"))
                msg = (detail.get("error") or {}).get("message")
                if msg:
                    last_err += f" ({msg})"
            except (OSError, ValueError):
                pass
            break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < _RETRY_ATTEMPTS - 1:
                delay = min(2 ** attempt * 2, _RETRY_MAX_DELAY_S)
                logger.warning("Google API network error on %s (%s); backing off %ds", path, e, delay)
                _time.sleep(delay)
                last_err = str(e)
                continue
            last_err = str(e)
            break
    raise GoogleAPIError(f"{path}: {last_err}")


def _api_get(token: str, path: str, params: dict | None = None) -> dict:
    return _api_request(token, path, "GET", params)


def _q(s: str) -> str:
    """URL-quote a calendar/event id (they contain @ # . chars)."""
    return urllib.parse.quote(s, safe="")


def list_user_calendars(token: str) -> list[dict]:
    """The signed-in user's calendar list. [{id, summary, color, timeZone}]."""
    items: list[dict] = []
    page_token = None
    while True:
        params = {"maxResults": _CALENDARLIST_MAX}
        if page_token:
            params["pageToken"] = page_token
        data = _api_get(token, "/users/me/calendarList", params)
        for it in data.get("items", []):
            if it.get("id"):
                items.append({
                    "id": it["id"],
                    "display_name": it.get("summary") or "Calendar",
                    "color": it.get("backgroundColor") or it.get("colorRgbHex")
                    or it.get("colorXSS"),
                    "time_zone": it.get("timeZone"),
                })
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return items


def list_events(token: str, calendar_id: str,
                time_min: datetime, time_max: datetime,
                sync_token: str | None = None) -> tuple[list[dict], str | None]:
    """Events of one calendar overlapping [time_min, time_max).

    Returns (items, new_sync_token). With sync_token set, Google returns only
    what changed since that token (deleted instances flagged; showDeleted is
    implied) and a 410 raises SyncTokenExpired.
    """
    path = f"/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
    events: list[dict] = []
    new_token = None
    page_token = None
    while True:
        params = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "maxResults": _MAX_PAGED_ITEMS,
        }
        if sync_token:
            params["syncToken"] = sync_token
            params["showDeleted"] = "true"
        if page_token:
            params["pageToken"] = page_token
        data = _api_get(token, path, params)
        events.extend(data.get("items", []))
        new_token = data.get("syncToken") or new_token
        page_token = data.get("nextPagePageToken") or data.get("nextPageToken")
        if not page_token:
            break
    return events, new_token


def is_deleted(ev: dict) -> bool:
    """A sync-response item that marks an instance as deleted.

    Google flags deletions as status 'deleted' (and older responses carry a
    `deleted: true` property); accept either so a deletion is never cached.
    """
    return bool(ev.get("deleted")) or ev.get("status") == "deleted"


def _google_dt(ev: dict, which: str) -> tuple[datetime | None, bool]:
    """Return (datetime, all_day) for an event's start/end half.

    All-day halves carry `date` (YYYY-MM-DD); timed halves carry `dateTime`
    with an offset. Missing end falls back like the CalDAV parser does.
    """
    half = ev.get(which) or {}
    if half.get("date"):
        d = datetime.fromisoformat(half["date"])
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc), True
    if half.get("dateTime"):
        try:
            return datetime.fromisoformat(half["dateTime"]), False
        except ValueError:
            return None, False
    return None, False


def _orig_stamp(ev: dict) -> str | None:
    """Google's originalStartTime for a recurring instance.

    Feeds recurrence_id so the panel renders the row as a series occurrence
    (is_recurring) and sends occurrence=<stamp> on a single-instance delete.
    """
    orig = ev.get("originalStartTime") or {}
    return orig.get("dateTime") or orig.get("date") or None


def to_cache_event(ev: dict) -> dict | None:
    """Map one Google event instance to the cache event dict shape.

    None means "unusable item" (no id/start) — the caller skips it.
    Recurring instances come back as one-off rows: Google never exposes the
    master RRULE, so rrule/exdates stay NULL and the fetch window is the
    series' visibility.
    """
    uid = ev.get("id")
    if not uid:
        return None
    start, all_day = _google_dt(ev, "start")
    if start is None:
        return None
    end, _ = _google_dt(ev, "end")
    if end is None:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))

    tzid = None
    if not all_day:
        zone = ev.get("timeZone")
        if zone:
            # Validate before caching: expansion re-attaches ZoneInfo(tzid)
            # at load, and a bogus name would throw there instead.
            from zoneinfo import ZoneInfo
            try:
                ZoneInfo(zone)
                tzid = zone
            except (KeyError, OSError, ValueError):  # ZoneInfoNotFoundError subclasses KeyError
                logger.warning("Google event %s has unusable timeZone %r; keeping baked offset", uid, zone)

    status = (ev.get("status") or "confirmed").upper()
    return {
        "uid": uid,
        "summary": ev.get("summary") or "",
        "description": ev.get("description") or "",
        "location": ev.get("location") or "",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "all_day": 1 if all_day else 0,
        "recurrence_id": _orig_stamp(ev),
        "status": status,
        "transparency": "OPAQUE",
        "rrule": None,
        "exdates": None,
        # href == uid for Google rows: the incremental deletion path matches
        # changed/deleted resources by href, same as the CalDAV sync expects.
        "href": uid,
        "tzid": tzid,
    }


def google_url(calendar_id: str) -> str:
    """Cache URL for a Google calendar row — namespaced so the stale prune
    and ownership checks never mistake it for a DAV collection URL."""
    return f"google:{calendar_id}"


# --- Write side (panel edits routed here by provider) -----------------------
# Google instances are first-class: PATCH/DELETE on an instance event id
# edits/deletes JUST that occurrence — the same semantics the panel offers
# for CalDAV series, without EXDATE/RECURRENCE-ID surgery.

def _iana_zone(dt: datetime) -> str | None:
    """IANA zone name for a datetime: ZoneInfo.key, astimezone's zone_name,
    else the machine's local zone when only a UTC offset is attached.

    Google accepts bare offsets in dateTime, but recurring events need the
    explicit timeZone field ("Missing time zone definition for start time",
    HTTP 400) — offsets alone are ambiguous across DST for a series.
    """
    tz = getattr(dt, "tzinfo", None)
    if tz is None:
        return None
    key = getattr(tz, "key", None) or getattr(tz, "zone_name", None)
    if key:
        return str(key)
    # Offset-only tzinfo (astimezone on this Python): resolve the machine's
    # IANA zone from TZ env or the /etc/localtime symlink.
    tz_env = os.environ.get("TZ")
    if tz_env:
        return tz_env
    target = os.path.realpath("/etc/localtime")
    if "zoneinfo/" in target:
        # Filenames escape real "/" in zone names as "+" (e.g. America/Argentina/Buenos+Aires)
        return target.rsplit("zoneinfo/", 1)[-1].replace("+", "/")
    return None


def _contrast_fg(hex6: str) -> str:
    """Black/white foreground for a background hex (Google wants both)."""
    r, g, b = (int(hex6[i:i + 2], 16) for i in (1, 3, 5))
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#000000" if lum > 0.55 else "#ffffff"


def patch_calendar(token: str, calendar_id: str,
                   name: str | None = None, color: str | None = None) -> dict:
    """Rename (summary) and/or recolour a calendar.

    Rename: PATCH the calendar resource (/calendars/<id>, summary).
    Recolour: PATCH the calendarList ITEM (/users/me/calendarList/<id>) with
    backgroundColor + foregroundColor and colorRgbFormat=true as a QUERY
    PARAM — colour is a per-list-user subscription property, and without the
    flag the colour fields in the body are silently ignored (colorRgbHex is
    not a writable field at all; Google auto-matches the nearest colorId).
    Panel colours are #RRGGBB[AA]; the alpha pair is dropped.
    """
    result: dict = {}
    if name:
        result = _api_request(token, f"/calendars/{_q(calendar_id)}", "PATCH",
                              None, {"summary": name})
    if color:
        hex6 = color[:7]
        _api_request(token, f"/users/me/calendarList/{_q(calendar_id)}",
                     "PATCH", {"colorRgbFormat": "true"},
                     {"backgroundColor": hex6, "foregroundColor": _contrast_fg(hex6)})
    return result


def create_calendar(token: str, name: str, color: str | None = None) -> dict:
    """POST a new calendar; then recolour its calendarList item (the
    calendar resource itself carries no colour — it is a list property)."""
    created = _api_request(token, "/calendars", "POST", None, {"summary": name})
    if color and created.get("id"):
        patch_calendar(token, created["id"], None, color)
    return created


def delete_calendar(token: str, calendar_id: str) -> None:
    """DELETE /calendars/<id> — only works for calendars the user created."""
    _api_request(token, f"/calendars/{_q(calendar_id)}", "DELETE")


def create_event(token: str, calendar_id: str, summary: str,
                 start: datetime, end: datetime | None,
                 all_day: bool, location: str | None = None,
                 description: str | None = None, rrule: str | None = None) -> dict:
    """POST a new event; returns the created event resource (id, etag...).

    all_day -> date-valued halves (Google's all-day end is EXCLUSIVE, same
    iCalendar convention as our cache); rrule -> recurrence ["RRULE:..."].
    """
    body: dict = {"summary": summary}
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if all_day:
        sd = (start.date() if isinstance(start, datetime) else start)
        ed = (end.date() if isinstance(end, datetime) else end)
        if ed is None or ed <= sd:
            ed = sd + timedelta(days=1)
        body["start"], body["end"] = {"date": sd.isoformat()}, {"date": ed.isoformat()}
    else:
        if end is None:
            end = start + timedelta(hours=1)
        z = _iana_zone(start)
        body["start"] = {"dateTime": start.isoformat()}
        body["end"] = {"dateTime": end.isoformat()}
        if z:
            # Recurring inserts demand the zone INSIDE start/end (start.timeZone
            # is "required for recurring events"); a top-level timeZone is
            # ignored there -> "Missing time zone definition for start time".
            body["start"]["timeZone"] = z
            body["end"]["timeZone"] = z
    if rrule:
        body["recurrence"] = [f"RRULE:{rrule}"]
    return _api_request(token, f"/calendars/{_q(calendar_id)}/events", "POST", None, body)


def patch_event(token: str, calendar_id: str, event_id: str,
                summary: str | None = None, start: datetime | None = None,
                end: datetime | None = None, all_day: bool | None = None,
                location: str | None = None, description: str | None = None) -> dict:
    """PATCH one event (master or instance id) with only the given fields.

    An instance id edits just that occurrence (Google's own model). start/end
    must come as a pair when moving/resizing; all_day switches the field form.
    """
    body: dict = {}
    if summary is not None:
        body["summary"] = summary
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if start is not None:
        if all_day:
            sd = (start.date() if isinstance(start, datetime) else start)
            ed = (end.date() if isinstance(end, datetime) else end)
            if ed is None or ed <= sd:
                ed = sd + timedelta(days=1)
            body["start"], body["end"] = {"date": sd.isoformat()}, {"date": ed.isoformat()}
        else:
            if end is None:
                end = start + timedelta(hours=1)
            body["start"] = {"dateTime": start.isoformat()}
            body["end"] = {"dateTime": end.isoformat()}
            z = _iana_zone(start)
            if z:
                body["start"]["timeZone"] = z
                body["end"]["timeZone"] = z
    if not body:
        return {}
    return _api_request(token, f"/calendars/{_q(calendar_id)}/events/{_q(event_id)}",
                        "PATCH", None, body)


def delete_event(token: str, calendar_id: str, event_id: str) -> dict:
    """DELETE one event resource (instance id = that occurrence only)."""
    return _api_request(token, f"/calendars/{_q(calendar_id)}/events/{_q(event_id)}",
                        "DELETE")


def get_event(token: str, calendar_id: str, event_id: str) -> dict:
    """GET one event resource; recurringEventId present for instances."""
    return _api_request(token, f"/calendars/{_q(calendar_id)}/events/{_q(event_id)}",
                        "GET")


def move_event(token: str, from_cal: str, event_id: str, to_cal: str) -> dict:
    """Move an event between calendars (keeps its id; google->google only)."""
    return _api_request(token, f"/calendars/{_q(to_cal)}/events/{_q(from_cal)}/{_q(event_id)}/move",
                        "POST")

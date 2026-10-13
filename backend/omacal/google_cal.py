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


def _api_get(token: str, path: str, params: dict | None = None) -> dict:
    """GET a Google Calendar API v3 path with a Bearer token; return JSON.

    Retries 429/5xx with exponential backoff (honouring Retry-After when
    sane). 410 raises SyncTokenExpired; other failures raise GoogleAPIError.
    """
    url = _API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last_err = "unknown"
    for attempt in range(_RETRY_ATTEMPTS):
        req = urllib.request.Request(url)
        req.headers["Authorization"] = f"Bearer {token}"
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 410:
                raise SyncTokenExpired(f"{path}: syncToken expired") from None
            if e.code in (429, 500, 502, 503) and attempt < _RETRY_ATTEMPTS - 1:
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
                    "color": it.get("color"),
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
        "recurrence_id": None,
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

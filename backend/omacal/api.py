"""Lightweight REST API for the omacal QML frontend.

Serves events over HTTP on localhost. The QML panel fetches from here.

Endpoints:
    GET  /api/health            → {"status": "ok"}
    GET  /api/calendars         → list of calendar metadata
    GET  /api/events?start=..&end=.. → events in range
    GET  /api/events/today      → today's events
    POST /api/sync              → trigger a sync run
    POST /api/events             → create a new event
    POST /api/calendars/toggle  → enable/disable a calendar's visibility
    POST /api/calendars/add     → create a new named calendar on one account
                                   ({name, color?, account}; reuses that
                                   account's stored credentials)
    PUT  /api/calendars/<id>    → edit an owned calendar's name and/or
                                   color ({name?, color?}; PROPPATCH)
    GET  /api/account           → one row per ACCOUNT (a config.json
                                   calendars[] entry): {key, name,
                                   display_name, provider, enabled, calendars,
                                   username, url}. `name` is the account's own
                                   label (set at setup), or a host fallback;
                                   `display_name` is its primary calendar's
                                   name. CalDAV accounts key on their principal
                                   URL, the Google account on "google"
    POST /api/account/toggle    → soft-enable/disable an account ({key,
                                   enabled}: persists the flag on the entry and
                                   flips the enabled column on all that
                                   account's calendar rows)
    POST /api/providers/nextcloud/login/start → begin a Nextcloud Login Flow
                                   v2 browser sign-in ({server, name?}) →
                                   {flow_id, login_url}
    GET  /api/providers/nextcloud/login/poll?id=.. → poll that flow
                                   (pending|ok|expired|unknown|error); on "ok"
                                   it commits the account (keyring + config
                                   APPEND) and returns {status:"ok"}
    POST /api/providers/google/login/start → begin a Google OAuth sign-in from
                                   the user's OWN Cloud-project client
                                   ({client_id, client_secret, name?}); the
                                   spec is persisted to config.json's oauth
                                   section → {flow_id, login_url}
    GET  /api/providers/google/login/poll?id=.. → poll that flow
                                   (pending|ok|cancelled|unknown|error); on "ok"
                                   it commits the Google account (config
                                   APPEND; tokens already in the keyring)
    POST /api/providers/caldav/add → add a CalDAV account from {url, username,
                                   password, display_name?} (keyring + config
                                   APPEND), for any CalDAV server
    POST /api/providers/flow/cancel?id=.. → abandon an in-flight browser sign-in
                                   flow (Nextcloud or Google), releasing its
                                   server-side state. Idempotent.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, time, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from omacal.config import account_key, load_config
from omacal.db import (
    list_calendars as db_get_calendars,
    get_events as db_get_events,
    get_overrides as db_get_overrides,
    get_events_for_window as db_get_window,
    get_reminder_overrides as db_get_reminder_overrides,
    get_reminders_for_window as db_get_reminders_for_window,
)
from omacal.recur import expand_events, expand_reminders

logger = logging.getLogger("omacal.api")

# Fixed Google OAuth endpoints/scopes. Only client_id/client_secret are user
# input (BYO Cloud project — decision D1); the rest is not user-configurable.
_GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Broad calendar scope on purpose: the narrower calendar.calendars +
# calendar.events pair omits the calendarList endpoints, so a freshly
# connected Google account would show NO calendars.
_GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


def _resolve_caldav_url(
    url: str,
    username: str | None,
    password: str | None,
) -> tuple[str, list[dict]]:
    """Resolve what a user typed into a working CalDAV URL.

    Accepts either a bare server / domain (``cloud.lamafam.org``) or a full
    DAV URL. Tries the literal address first (covers direct calendar/principal
    URLs and legacy servers that publish no discovery hints); if that finds no
    calendars, falls back to RFC 6764 automatic discovery (well-known URI,
    then DNS SRV/TXT). Returns ``(url, discovered)`` where ``url`` is the
    *principal URL* resolved for the authenticated user — the address the
    backend keys the keyring by and stores as ``principal_url``, so the config
    key, keyring key and DB lookup all agree. Raises ValueError if neither the
    literal URL nor discovery finds a working calendar.
    """
    import urllib.parse as up

    from caldav.discovery import discover_caldav

    from omacal.sync import _discover_calendars  # noqa: PLC2701

    if not up.urlparse(url).scheme:
        url = "https://" + url

    # 1. Try the address as typed.
    try:
        discovered = _discover_calendars(url, username, password)
        if discovered:
            return _principal_url(url, discovered), discovered
    except Exception:
        pass

    # 2. Fall back to RFC 6764 discovery (well-known, then SRV/TXT).
    try:
        info = discover_caldav(url, timeout=10, require_tls=True)
    except Exception as e:
        raise ValueError(
            f"Could not discover a CalDAV server for {url!r}: {e}"
        ) from e
    if not info or not info.url:
        raise ValueError(
            f"Could not reach a calendar server at {url!r}. Tried the address "
            "directly and automatic CalDAV discovery; neither found a working "
            "calendar. Check the URL and try again."
        )
    root = info.url
    try:
        discovered = _discover_calendars(root, username, password)
    except Exception as e:
        raise ValueError(
            f"Found a CalDAV server ({root}) but could not load your "
            f"calendars with that username/password: {e}"
        ) from e
    if not discovered:
        raise ValueError(
            f"Found a CalDAV server ({root}) but no calendars were returned."
        )
    return _principal_url(root, discovered), discovered


def _principal_url(fallback: str, discovered: list[dict]) -> str:
    """The principal URL from a discovery result, falling back to ``fallback``.

    The backend stores each calendar's ``principal_url`` in the DB and keys the
    keyring password by it — so the URL setup stores must be that principal
    URL, not the service root discovery started from, or write-path lookup
    misses the keyring entry.
    """
    for cal in discovered:
        pu = cal.get("principal_url")
        if pu:
            return pu
    return fallback


def _account_label(cal: dict) -> str:
    """Human label for an account row.

    Prefers the account's own ``name`` (set when the account is added), so
    several CalDAV or Google accounts can be told apart; falls back to the
    server host (CalDAV) or "Google". ``display_name`` is only a last resort:
    on a calendars[] entry it is the PRIMARY CALENDAR's name captured from the
    server, not an account label.
    """
    if (cal.get("name") or "").strip():
        return str(cal["name"]).strip()
    if (cal.get("provider") or "caldav") == "google":
        return "Google"
    host = urlparse(cal.get("url") or "").hostname
    return host or cal.get("display_name") or "Calendar"


def _row_account_key(row: dict) -> str:
    """The account key a cached calendar ROW belongs to.

    ``account_key`` for DB rows, which carry ``principal_url`` instead of
    ``url``. Used to tell whether a row's account is disabled — the row's own
    ``enabled`` flag cannot, because disabling an account and hiding a single
    calendar both set it to 0.
    """
    return account_key({"provider": row.get("provider"),
                        "url": row.get("principal_url")})


class OmacalHandler(BaseHTTPRequestHandler):
    """Single-connection HTTP handler backed by a shared SQLite connection."""

    db_conn = None  # set by the server before serving
    cfg_calendars = []  # set by the server before serving

    def _cfg_calendars(self) -> list[dict]:
        """Return calendars from config.json"""
        return self.cfg_calendars

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/health":
            from omacal.config import needs_setup
            from omacal import __version__
            self._json(200, {
                "status": "ok",
                "version": __version__,
                "needs_setup": needs_setup(),
            })
            return

        if path == "/api/calendars":
            from omacal.keyring_store import has_password
            from omacal.sync import calendar_is_deletable
            calendars = db_get_calendars(self.db_conn)
            # Account-level gate (POST /api/account/toggle): a row whose ACCOUNT
            # is disabled is marked account_enabled=false so the panel can keep
            # it out of the add-event destination dropdown and the calendars
            # (visibility) list — the account switch in Account Settings is the
            # single control for those calendars. Derived from config, not from
            # the row's own `enabled` flag, which cannot tell an account disable
            # from a single-calendar hide (both set it to 0).
            disabled_accounts = {
                account_key(c) for c in self._cfg_calendars()
                if not c.get("enabled", True)
            }
            for c in calendars:
                if c.get("provider") == "google":
                    # Google rows write through the Calendar API with the OAuth
                    # token — no keyring password involved. calendarList only
                    # returns calendars the user manages, so edits are allowed.
                    c["writable"] = True
                    c["deletable"] = True
                else:
                    # Writability = we hold a password for this calendar in the
                    # keyring. The password is keyed by the principal URL (the
                    # configured URL); fall back to the calendar URL.
                    key = c.get("principal_url") or c.get("url", "")
                    c["writable"] = bool(c.get("username")) and has_password(key, c.get("username"))
                    # Deletable = owned by the signed-in user's namespace (not a
                    # *_shared_by_* collection); the server is the final arbiter.
                    # Subscribed (URL-added) rows are always deletable: the server
                    # treats that as unsubscribe (local cache removal only).
                    c["deletable"] = bool(c.get("subscribed")) or calendar_is_deletable(c.get("url"), c.get("principal_url"))
                c["account_enabled"] = _row_account_key(c) not in disabled_accounts
                c.pop("password", None)
                c.pop("username", None)
                c.pop("sync_token", None)
            self._json(200, calendars)
            return

        if path == "/api/config":
            cfg_names = [c.get("display_name") for c in self._cfg_calendars() if c.get("enabled") and c.get("display_name")]
            self._json(200, {"calendar_names": cfg_names})
            return

        if path == "/api/account":
            # One entry per ACCOUNT — a config.json calendars[] entry — not per
            # oauth provider (the oauth section is credential config, not an
            # account). CalDAV accounts are keyed by principal URL, the Google
            # account by "google"; `enabled` is the account's own soft-disable
            # flag (absent means enabled) and `calendars` is how many cached rows
            # currently belong to it.
            accounts = []
            for cal in self._cfg_calendars():
                provider = cal.get("provider", "caldav")
                key = account_key(cal)
                try:
                    if provider == "google":
                        row = self.db_conn.execute(
                            "SELECT COUNT(*) FROM calendars WHERE provider = 'google'"
                        ).fetchone()
                    else:
                        row = self.db_conn.execute(
                            "SELECT COUNT(*) FROM calendars "
                            "WHERE rtrim(principal_url, '/') = ?", (key,)
                        ).fetchone()
                    count = row[0] if row else 0
                except Exception:
                    count = 0
                accounts.append({
                    "key": key,
                    "name": _account_label(cal),
                    "display_name": cal.get("display_name") or "",
                    "provider": provider,
                    "enabled": bool(cal.get("enabled", True)),
                    "calendars": count,
                    # Non-secret settings, for the account view. The password
                    # lives in the keyring and is never returned.
                    "username": cal.get("username") or "",
                    "url": cal.get("url") or "",
                })
            self._json(200, accounts)
            return

        if path == "/api/providers/nextcloud/login/poll":
            # Poll a browser sign-in; on success this also commits the account.
            self._handle_nc_login_poll(qs)
            return

        if path == "/api/providers/google/login/poll":
            # Poll a Google sign-in; on success this also commits the account.
            self._handle_google_login_poll(qs)
            return

        if path == "/api/events/today":
            # Today in LOCAL time. Midnight-to-midnight UTC made the bar
            # tooltip's "today" mean [yesterday 20:00, today 20:00] in US Eastern:
            # anything after 8pm vanished from it, and last night's 8pm-midnight
            # events showed up as today's. Each boundary is resolved through the
            # system zone separately so the window is right across a DST change.
            local_today = datetime.now().date()
            start = datetime.combine(local_today, time.min).astimezone(timezone.utc)
            end = datetime.combine(local_today + timedelta(days=1), time.min).astimezone(timezone.utc)
            events = self._events_in_window(start, end, None)
            # All-day events are cached at UTC midnight regardless of local
            # zone, so the local-midnight window above can pull in the NEXT
            # local day's all-day events (a 2026-09-26T00:00:00Z all-day event
            # falls inside [09-25T04:00Z, 09-26T04:00Z) at UTC-4 and shows as
            # today). An all-day event belongs to today only when its DATE
            # part is today; timed events keep the honest overlap test.
            today_iso = local_today.isoformat()
            events = [e for e in events if (not e.get("all_day")) or e["start"][:10] == today_iso]
            self._json(200, events)
            return

        if path == "/api/events":
            start_str = qs.get("start", [None])[0]
            end_str = qs.get("end", [None])[0]
            cal_ids_raw = qs.get("calendars", [None])[0]

            if start_str and end_str:
                try:
                    start = datetime.fromisoformat(start_str)
                    end = datetime.fromisoformat(end_str)
                except ValueError:
                    self._json(400, {"error": "invalid start/end format, use ISO 8601"})
                    return
            else:
                # Default: the next 7 days from local midnight (the panel always
                # passes explicit bounds; this is for manual calls, and UTC
                # midnight would put the window 4h off in US Eastern).
                start = datetime.combine(datetime.now().date(), time.min).astimezone(timezone.utc)
                end = start + timedelta(days=7)

            cal_ids = None
            if cal_ids_raw:
                try:
                    cal_ids = [int(x) for x in cal_ids_raw.split(",")]
                except ValueError:
                    self._json(400, {"error": "invalid calendar ids"})
                    return

            events = self._events_in_window(start, end, cal_ids)
            self._json(200, events)
            return

        if path == "/api/reminders":
            start_str = qs.get("start", [None])[0]
            end_str = qs.get("end", [None])[0]
            cal_ids_raw = qs.get("calendars", [None])[0]

            if start_str and end_str:
                try:
                    start = datetime.fromisoformat(start_str)
                    end = datetime.fromisoformat(end_str)
                except ValueError:
                    self._json(400, {"error": "invalid start/end format, use ISO 8601"})
                    return
            else:
                # Default: the next 7 days from local midnight (same as events).
                start = datetime.combine(datetime.now().date(), time.min).astimezone(timezone.utc)
                end = start + timedelta(days=7)

            cal_ids = None
            if cal_ids_raw:
                try:
                    cal_ids = [int(x) for x in cal_ids_raw.split(",")]
                except ValueError:
                    self._json(400, {"error": "invalid calendar ids"})
                    return

            reminders = self._reminders_in_window(start, end, cal_ids)
            self._json(200, reminders)
            return

        self._json(404, {"error": "not found"})

    def _cache_fresh(self, max_age_seconds: int) -> bool:
        """True if every enabled calendar was synced within max_age_seconds.

        A calendar with no last_sync (never synced) makes the cache stale.
        """
        cur = self.db_conn.execute("SELECT last_sync FROM calendars WHERE enabled=1")
        rows = cur.fetchall()
        if not rows:
            return False
        now = datetime.now(timezone.utc)
        for r in rows:
            ts = r["last_sync"]
            if not ts:
                return False
            try:
                age = (now - datetime.fromisoformat(ts)).total_seconds()
            except (ValueError, TypeError):
                return False
            if age > max_age_seconds:
                return False
        return True

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/setup":
            self._handle_setup()
            return
        if parsed.path == "/api/providers/nextcloud/login/start":
            # Begin a Nextcloud Login Flow v2 browser sign-in.
            self._handle_nc_login_start()
            return
        if parsed.path == "/api/providers/google/login/start":
            # Begin a Google OAuth sign-in from the user's own Cloud project.
            self._handle_google_login_start()
            return
        if parsed.path == "/api/providers/caldav/add":
            # Manually add a CalDAV account (server + username + password).
            self._handle_caldav_add()
            return
        if parsed.path == "/api/providers/flow/cancel":
            # Abandon an in-flight browser sign-in flow (Nextcloud Login Flow
            # v2 or Google OAuth) so its state does not linger in the registry
            # until its TTL — and, for Google, so the loopback receiver thread
            # is released. Idempotent. The panel calls this when the user backs
            # out of Add Provider.
            self._handle_flow_cancel(parse_qs(parsed.query))
            return
        if parsed.path == "/api/sync":
            # ?if-stale=N: skip the sync entirely when the cache is fresher than
            # N seconds (the on-demand path the panel uses on open). The lock
            # means a concurrent run (the periodic timer) is reported rather
            # than double-fetching every calendar. No body is read.
            from omacal.sync import sync_all
            qs = parse_qs(parsed.query)
            if_stale = qs.get("if-stale", [None])[0]
            if if_stale is not None:
                try:
                    max_age = int(if_stale)
                except ValueError:
                    self._json(400, {"error": "if-stale must be seconds"})
                    return
                if self._cache_fresh(max_age):
                    logger.info("Sync skipped: cache fresh (if-stale=%d)", max_age)
                    self._json(200, {"status": "fresh", "synced": False})
                    return
            try:
                result = sync_all(raise_if_busy=True)
                self._json(200, result)
            except RuntimeError as e:
                self._json(409, {"error": str(e)})
            except Exception as e:
                self._json(500, {"error": str(e)})
            return
        if parsed.path == "/api/events":
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = json.loads(body)
                summary = data.get("summary", "")
                calendar_id = int(data.get("calendar_id"))
                start_str = data["start"]
                end_str = data.get("end")
                all_day = data.get("all_day", False)
                location = data.get("location", "")
                description = data.get("description", "")
                rrule = (data.get("rrule") or "").strip() or None

                start_dt = datetime.fromisoformat(start_str)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)

                end_dt = None
                if end_str:
                    end_dt = datetime.fromisoformat(end_str)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)

                from omacal.sync import create_event
                result = create_event(self.db_conn, calendar_id, summary, start_dt, end_dt,
                                      all_day, location, description, rrule=rrule)
                self._json(200, result)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                logger.error("POST /api/events failed: %s", e)
                self._json(500, {"error": str(e)})
            return
        if parsed.path == "/api/reminders":
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = json.loads(body)
                summary = data.get("summary", "")
                calendar_id = int(data.get("calendar_id"))
                due_str = data["due"]
                start_str = data.get("start")
                all_day = data.get("all_day", False)
                location = data.get("location", "")
                description = data.get("description", "")
                alarm_offset = data.get("alarm_offset")
                alarm_time = data.get("alarm_time")
                rrule = (data.get("rrule") or "").strip() or None

                due_dt = datetime.fromisoformat(due_str)
                if due_dt.tzinfo is None:
                    due_dt = due_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                start_dt = None
                if start_str:
                    start_dt = datetime.fromisoformat(start_str)
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)

                from omacal.sync import create_reminder
                result = create_reminder(self.db_conn, calendar_id, summary, due_dt,
                                         start_dt, all_day, location, description, rrule=rrule,
                                         alarm_offset=alarm_offset, alarm_time=alarm_time)
                self._json(200, result)
            except (ValueError, KeyError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                logger.error("POST /api/reminders failed: %s", e)
                self._json(500, {"error": str(e)})
            return
        if parsed.path == "/api/calendars/add":
            # Create a NEW named calendar on ONE account, reusing that
            # account's stored credentials (no new login). Body: {name, color?,
            # account} — `account` is the account KEY from GET /api/account
            # ("google", or a CalDAV principal URL) and must name an enabled
            # account.
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = json.loads(body)
                name = (data.get("name") or "").strip()
                if not name:
                    self._json(400, {"error": "Calendar name is required"})
                    return
                color = (data.get("color") or "").strip() or None
                if color and not re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", color):
                    self._json(400, {"error": "Bad calendar color"})
                    return
                account = (data.get("account") or "").strip()
                if not account:
                    self._json(400, {"error": "No account selected"})
                    return
                from omacal.sync import add_calendar_named
                result = add_calendar_named(self.db_conn, name, color, account)
                self._json(200, result)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                logger.error("POST /api/calendars/add failed: %s", e)
                self._json(500, {"error": str(e)})
            return
        if parsed.path == "/api/calendars/subscribe":
            # Subscribe to an EXISTING calendar by collection URL (body: {url,
            # name?, color?}). No new login: same-origin URLs reuse the
            # keyring credentials; others fetch unauthenticated (public).
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = json.loads(body)
                url = (data.get("url") or "").strip()
                if not url:
                    self._json(400, {"error": "Calendar URL is required"})
                    return
                name = (data.get("name") or "").strip() or None
                color = (data.get("color") or "").strip() or None
                if color and not re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", color):
                    self._json(400, {"error": "Bad calendar color"})
                    return
                from omacal.sync import subscribe_calendar
                result = subscribe_calendar(self.db_conn, url, name, color)
                self._json(200, result)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                logger.error("POST /api/calendars/subscribe failed: %s", e)
                self._json(500, {"error": str(e)})
            return
        if parsed.path == "/api/calendars/toggle":
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = json.loads(body)
                calendar_id = int(data["calendar_id"])
                enabled = bool(data.get("enabled", True))

                from omacal.db import set_calendar_enabled
                updated = set_calendar_enabled(self.db_conn, calendar_id, enabled)
                if not updated:
                    self._json(404, {"error": "unknown calendar id"})
                    return
                self._json(200, {"ok": True, "calendar_id": calendar_id, "enabled": enabled})
            except (ValueError, KeyError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                logger.error("POST /api/calendars/toggle failed: %s", e)
                self._json(500, {"error": str(e)})
            return
        if parsed.path == "/api/account/toggle":
            # Soft-enable/disable an ACCOUNT (a config.json calendars[] entry,
            # keyed by principal URL or "google"): persist `enabled` on the entry
            # (survives DB wipes; absent means enabled) and flip the enabled
            # column on every cached row of that account. Per-account, not
            # per-provider — several CalDAV accounts share provider='caldav', so
            # gating on the provider would disable all of them at once.
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = json.loads(body)
                key = str(data["key"])
                enabled = bool(data.get("enabled", True))
                from omacal.config import load_config, save_config
                cfg = load_config()
                calendars = list(cfg.get("calendars") or [])
                target = next((c for c in calendars if account_key(c) == key), None)
                if target is None:
                    self._json(400, {"error": f"unknown account '{key}'"})
                    return
                target["enabled"] = enabled
                cfg["calendars"] = calendars
                save_config(cfg=cfg)
                OmacalHandler.cfg_calendars = calendars
                from omacal.db import set_account_enabled
                updated = set_account_enabled(
                    self.db_conn, key, target.get("provider", "caldav"), enabled)
                self._json(200, {"ok": True, "key": key, "enabled": enabled,
                                  "calendars_updated": updated})
            except (ValueError, KeyError, TypeError) as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                logger.error("POST /api/account/toggle failed: %s", e)
                self._json(500, {"error": str(e)})
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/calendars/") and len(parsed.path) > len("/api/calendars/"):
            # DELETE /api/calendars/<id>: server-side delete of the whole
            # collection (like event delete). Ownership-gated server-side.
            try:
                calendar_id = int(parsed.path.rsplit("/", 1)[-1])
                from omacal.sync import delete_calendar
                result = delete_calendar(self.db_conn, calendar_id)
                self._json(200, result)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                logger.error("DELETE /api/calendars failed: %s", e)
                self._json(500, {"error": str(e)})
            return
        if parsed.path == "/api/events":
            query = parse_qs(parsed.query)
            uid = query.get("uid", [None])[0]
            calendar_id_raw = query.get("calendar_id", [None])[0]
            occurrence_raw = query.get("occurrence", [None])[0]
            series = (query.get("series", ["false"])[0] or "").strip().lower() in ("1", "true", "yes")
            if not uid or not calendar_id_raw:
                self._json(400, {"error": "uid and calendar_id query parameters required"})
                return

            occurrence = None
            if occurrence_raw:
                try:
                    occurrence = datetime.fromisoformat(occurrence_raw)
                except ValueError:
                    self._json(400, {"error": "invalid occurrence format, use ISO 8601"})
                    return
                if occurrence.tzinfo is None:
                    occurrence = occurrence.replace(tzinfo=timezone.utc)

            try:
                calendar_id = int(calendar_id_raw)
                from omacal.sync import delete_event
                result = delete_event(self.db_conn, calendar_id, uid, occurrence=occurrence, series=series)
                self._json(200, result)
            except ValueError as e:
                # e.g. "this is a repeating event: pass occurrence=..." -- a
                # caller error, not a server fault.
                self._json(400, {"error": str(e)})
            except Exception as e:
                logger.error("DELETE failed: %s", e)
                self._json(500, {"error": str(e)})
            return
        if parsed.path == "/api/reminders":
            query = parse_qs(parsed.query)
            uid = query.get("uid", [None])[0]
            calendar_id_raw = query.get("calendar_id", [None])[0]
            if not uid or not calendar_id_raw:
                self._json(400, {"error": "uid and calendar_id query parameters required"})
                return
            try:
                calendar_id = int(calendar_id_raw)
                from omacal.sync import delete_reminder
                result = delete_reminder(self.db_conn, calendar_id, uid)
                self._json(200, result)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                logger.error("DELETE /api/reminders failed: %s", e)
                self._json(500, {"error": str(e)})
            return
        self._json(404, {"error": "not found"})

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/calendars/") and len(parsed.path) > len("/api/calendars/"):
            # PUT /api/calendars/<id> body {name?, color?}: edit an owned
            # calendar's display-name and/or color (PROPPATCH server-side,
            # cache refreshed). Ownership-gated in edit_calendar; subscribed
            # and shared rows are refused.
            try:
                calendar_id = int(parsed.path.rsplit("/", 1)[-1])
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = json.loads(body) if body else {}
                name = (data.get("name") or "").strip() or None
                color = (data.get("color") or "").strip() or None
                if color and not re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", color):
                    self._json(400, {"error": "Bad calendar color"})
                    return
                from omacal.sync import edit_calendar
                result = edit_calendar(self.db_conn, calendar_id, name, color)
                self._json(200, result)
            except ValueError as e:
                self._json(400, {"error": str(e)})
            except Exception as e:
                logger.error("PUT /api/calendars failed: %s", e)
                self._json(500, {"error": str(e)})
            return
        if parsed.path == "/api/events":
            query = parse_qs(parsed.query)
            uid = query.get("uid", [None])[0]
            if not uid:
                self._json(400, {"error": "uid query parameter required"})
                return
            occurrence_raw = query.get("occurrence", [None])[0]
            occurrence = None
            if occurrence_raw:
                try:
                    occurrence = datetime.fromisoformat(occurrence_raw)
                except ValueError:
                    self._json(400, {"error": "invalid occurrence format, use ISO 8601"})
                    return
                if occurrence.tzinfo is None:
                    occurrence = occurrence.replace(tzinfo=timezone.utc)
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = json.loads(body)
                summary = data.get("summary", "")
                calendar_id = int(data.get("calendar_id"))
                start_str = data["start"]
                end_str = data.get("end")
                all_day = data.get("all_day", False)
                location = data.get("location", "")
                description = data.get("description", "")

                start_dt = datetime.fromisoformat(start_str)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                end_dt = None
                if end_str:
                    end_dt = datetime.fromisoformat(end_str)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)

                from omacal.sync import update_event
                result = update_event(self.db_conn, calendar_id, uid, summary, start_dt, end_dt, all_day, location, description, occurrence)
                self._json(200, result)
            except Exception as e:
                import traceback
                logger.error("PUT failed: %s\n%s", e, traceback.format_exc())
                self._json(500, {"error": str(e)})
            return
        if parsed.path == "/api/reminders":
            query = parse_qs(parsed.query)
            uid = query.get("uid", [None])[0]
            if not uid:
                self._json(400, {"error": "uid query parameter required"})
                return
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = json.loads(body)
                summary = data.get("summary", "")
                calendar_id = int(data.get("calendar_id"))
                due_str = data["due"]
                start_str = data.get("start")
                all_day = data.get("all_day", False)
                location = data.get("location", "")
                description = data.get("description", "")
                alarm_offset = data.get("alarm_offset")
                alarm_time = data.get("alarm_time")

                due_dt = datetime.fromisoformat(due_str)
                if due_dt.tzinfo is None:
                    due_dt = due_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                start_dt = None
                if start_str:
                    start_dt = datetime.fromisoformat(start_str)
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)

                from omacal.sync import update_reminder
                result = update_reminder(self.db_conn, calendar_id, uid, summary, due_dt,
                                         start_dt, all_day, location, description,
                                         alarm_offset=alarm_offset, alarm_time=alarm_time)
                self._json(200, result)
            except Exception as e:
                import traceback
                logger.error("PUT /api/reminders failed: %s\n%s", e, traceback.format_exc())
                self._json(500, {"error": str(e)})
            return
        self._json(404, {"error": "not found"})

    def _handle_setup(self) -> None:
        """Configure a single calendar from the setup form.

        Stores the password in the keyring, writes config.json (no password),
        then contacts the server to discover the calendar. Returns {ok: true}
        once the config is committed and the server resolved; the frontend
        follows up with POST /api/sync for the first data pull so it can show
        "found server — syncing" feedback. On failure returns
        {ok: false, error: ...} so the frontend can stay on the form.
        """
        from omacal.config import save_config
        from omacal.keyring_store import store_password

        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(body)
        except Exception as e:
            self._json(400, {"ok": False, "error": f"Invalid request body: {e}"})
            return

        url = (data.get("url") or "").strip()
        display_name = (data.get("display_name") or "").strip() or None
        username = (data.get("username") or "").strip() or None
        password = data.get("password")  # never echoed back, never persisted to disk

        if not url:
            self._json(400, {"ok": False, "error": "Server URL is required"})
            return
        if not username:
            self._json(400, {"ok": False, "error": "Username is required"})
            return
        if not password:
            self._json(400, {"ok": False, "error": "Password is required"})
            return

        # 1. Resolve the URL (literal, else RFC 6764 discovery) and contact
        #    the server — this validates the URL + credentials before we
        #    commit anything to disk or the keyring.
        try:
            url, discovered = _resolve_caldav_url(url, username, password)
        except ValueError as e:
            self._json(200, {"ok": False, "error": str(e)})
            return

        # 2. Store the password in the keyring, keyed by the URL that worked.
        if not store_password(url, username, password):
            self._json(200, {"ok": False, "error": "Could not store credentials in the system keyring"})
            return

        # 3. Write config (username only — never the password).
        cfg = load_config()
        cfg["calendars"] = [{
            "url": url,
            "display_name": display_name or (discovered[0].get("display_name") or "Calendar"),
            "username": username,
            "enabled": True,
        }]
        save_config(cfg=cfg)
        # Pick up the new config in the running server.
        OmacalHandler.cfg_calendars = cfg["calendars"]

        # 4. Return OK immediately. The initial sync is deliberately NOT run
        #    here: the frontend shows "Found server — syncing…" and then calls
        #    POST /api/sync itself, so the user gets honest two-stage feedback
        #    (discovery done / data syncing) instead of one long silent wait.
        self._json(200, {"ok": True})

    def _handle_nc_login_start(self) -> None:
        """Begin a Nextcloud Login Flow v2 (browser sign-in).

        No OAuth client registration and no Nextcloud admin rights are needed:
        the flow mints a per-client app password the user can revoke later.
        """
        from omacal import loginflow

        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(body)
        except Exception as e:
            self._json(400, {"ok": False, "error": f"Invalid request body: {e}"})
            return
        try:
            started = loginflow.nextcloud_start(data.get("server") or "",
                                                data.get("name"))
        except ValueError as e:
            self._json(200, {"ok": False, "error": str(e)})
            return
        except Exception as e:
            logger.error("Nextcloud login flow start failed: %s", e)
            self._json(500, {"ok": False, "error": str(e)})
            return
        self._json(200, {"ok": True, **started})

    def _handle_nc_login_poll(self, qs: dict) -> None:
        """Poll a Nextcloud login flow; on success, commit the account."""
        from omacal import loginflow

        flow_id = (qs.get("id") or [""])[0]
        if not flow_id:
            self._json(400, {"error": "id is required"})
            return
        result = loginflow.nextcloud_poll(flow_id)
        if result.get("status") != "ok":
            self._json(200, result)
            return
        try:
            account = self._commit_caldav_account(
                result["server"], result["login_name"], result["app_password"],
                result.get("name"))
        except ValueError as e:
            self._json(200, {"status": "error", "error": str(e)})
            return
        except Exception as e:
            logger.error("Nextcloud login commit failed: %s", e)
            self._json(500, {"status": "error", "error": str(e)})
            return
        self._json(200, {"status": "ok", "account": account})

    def _handle_google_login_start(self) -> None:
        """Begin a Google OAuth sign-in from the panel.

        The panel supplies the user's OWN Cloud-project client_id/secret (BYO —
        decision D1); endpoints, scope and extra params are fixed here. We
        persist the spec to config.json's oauth section BEFORE starting the
        flow so the later token-refresh path has the client to refresh with
        (unchanged from the original design, which already keeps provider client
        secrets there — /api/account redacts them). The flow's loopback port is
        left unset so the OS picks a free one: Google Desktop-app clients accept
        any loopback port, so nothing must be pre-registered.
        """
        from omacal import loginflow
        from omacal.config import load_config, save_config

        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(body)
        except Exception as e:
            self._json(400, {"ok": False, "error": f"Invalid request body: {e}"})
            return

        client_id = (data.get("client_id") or "").strip()
        client_secret = (data.get("client_secret") or "").strip()
        name = (data.get("name") or "").strip() or None
        if not client_id:
            self._json(200, {"ok": False, "error": "Client ID is required"})
            return
        if not client_secret:
            self._json(200, {"ok": False, "error": "Client secret is required"})
            return

        cfg = load_config()
        oauth = dict(cfg.get("oauth") or {})
        spec = dict(oauth.get("google") or {})
        spec.update({
            "client_id": client_id,
            "client_secret": client_secret,
            "authorize_url": _GOOGLE_AUTHORIZE_URL,
            "token_url": _GOOGLE_TOKEN_URL,
            "scopes": [_GOOGLE_CALENDAR_SCOPE],
            # Refresh tokens are only issued on the FIRST consent, so ask for
            # one every time (see oauth.py's Google notes).
            "extra_params": {"access_type": "offline", "prompt": "consent"},
        })
        spec.pop("redirect_port", None)  # ephemeral loopback port (any is valid)
        oauth["google"] = spec
        cfg["oauth"] = oauth
        save_config(cfg=cfg)

        try:
            started = loginflow.google_start(cfg, name)
        except ValueError as e:
            self._json(200, {"ok": False, "error": str(e)})
            return
        except Exception as e:
            logger.error("Google login flow start failed: %s", e)
            self._json(500, {"ok": False, "error": str(e)})
            return
        self._json(200, {"ok": True, **started})

    def _handle_google_login_poll(self, qs: dict) -> None:
        """Poll a Google login flow; on success, commit the account."""
        from omacal import loginflow

        flow_id = (qs.get("id") or [""])[0]
        if not flow_id:
            self._json(400, {"error": "id is required"})
            return
        result = loginflow.google_poll(flow_id)
        if result.get("status") != "ok":
            self._json(200, result)
            return
        try:
            account = self._commit_google_account(result.get("name"))
        except Exception as e:
            logger.error("Google login commit failed: %s", e)
            self._json(500, {"status": "error", "error": str(e)})
            return
        self._json(200, {"status": "ok", "account": account})

    def _handle_flow_cancel(self, qs: dict) -> None:
        """Drop an in-flight browser sign-in flow's server-side state.

        The panel calls this when the user backs out of Add Provider (Cancel,
        Back to the chooser, or closing the dialog) so the flow registry does
        not hold the entry until its 20-minute TTL. Best-effort and idempotent:
        an id that was never issued, already consumed, or already expired just
        drops nothing.

        Note this cannot un-commit: the API server is single-threaded, so a
        poll already mid-flight when the cancel arrives finishes (and commits)
        first. After the drop, a later poll of the same id reads "unknown".
        """
        from omacal import loginflow

        flow_id = (qs.get("id") or [""])[0]
        if not flow_id:
            self._json(400, {"ok": False, "error": "id is required"})
            return
        loginflow.drop_flow(flow_id)
        self._json(200, {"ok": True})

    def _handle_caldav_add(self) -> None:
        """Add a CalDAV account from server + username + password.

        The manual sibling of the Nextcloud browser sign-in: works for any
        CalDAV server, and for Nextcloud with an app password (which 2FA
        accounts require). APPENDS to config.json, unlike /api/setup.
        """
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data = json.loads(body)
        except Exception as e:
            self._json(400, {"ok": False, "error": f"Invalid request body: {e}"})
            return

        url = (data.get("url") or "").strip()
        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        name = (data.get("name") or "").strip() or None
        if not url:
            self._json(200, {"ok": False, "error": "Server URL is required"})
            return
        if not username:
            self._json(200, {"ok": False, "error": "Username is required"})
            return
        if not password:
            self._json(200, {"ok": False, "error": "Password is required"})
            return
        try:
            account = self._commit_caldav_account(url, username, password, name)
        except ValueError as e:
            self._json(200, {"ok": False, "error": str(e)})
            return
        except Exception as e:
            logger.error("CalDAV add failed: %s", e)
            self._json(500, {"ok": False, "error": str(e)})
            return
        self._json(200, {"ok": True, "account": account})

    def _commit_caldav_account(
        self,
        server: str,
        username: str,
        password: str,
        name: str | None = None,
    ) -> dict:
        """Validate + discover, store the password, APPEND the account.

        Shared by the Nextcloud browser sign-in (password = minted app password)
        and the manual CalDAV path. Unlike /api/setup (which REPLACES the whole
        calendar list for a single first-run account), this appends to
        config.json so an existing account list survives "Add Provider". Nothing
        is written until the credentials are proven: a rejected password or
        unreachable server raises before the keyring or config are touched.

        ``name`` is the user's optional ACCOUNT label (stored under the ``name``
        key, distinct from ``display_name``, which is the primary calendar's
        name captured from the server).
        """
        from omacal.config import load_config, save_config
        from omacal.keyring_store import store_password

        url, discovered = _resolve_caldav_url(server, username, password)
        if not store_password(url, username, password):
            raise ValueError("Could not store credentials in the system keyring")

        cfg = load_config()
        calendars = list(cfg.get("calendars") or [])
        # Idempotent: adding the same principal again just refreshes its secret.
        if not any(
            (c.get("url") or "").rstrip("/") == url.rstrip("/") for c in calendars
        ):
            entry = {
                "url": url,
                "display_name": (discovered[0].get("display_name") if discovered else None)
                or urlparse(server).hostname or "Calendar",
                "username": username,
                "enabled": True,
            }
            if (name or "").strip():
                entry["name"] = name.strip()
            calendars.append(entry)
            cfg["calendars"] = calendars
            save_config(cfg=cfg)
            OmacalHandler.cfg_calendars = calendars
        return {"url": url, "username": username}

    def _commit_google_account(self, name: str | None = None) -> dict:
        """APPEND (idempotently) the Google account to config.json.

        The token set is already in the keyring (stored by the login flow), so
        this only adds the calendars[] entry the sync + panel read — the Google
        sibling of _commit_caldav_account, minus discovery (Google's calendar
        list comes from the API at sync time). Keyed by provider="google"
        (account_key -> "google").
        """
        from omacal.config import load_config, save_config

        cfg = load_config()
        calendars = list(cfg.get("calendars") or [])
        entry = next(
            (c for c in calendars if (c.get("provider") or "caldav") == "google"),
            None,
        )
        if entry is None:
            entry = {"provider": "google", "display_name": "Google"}
            calendars.append(entry)
        entry["enabled"] = True
        if (name or "").strip():
            entry["name"] = name.strip()
        cfg["calendars"] = calendars
        save_config(cfg=cfg)
        OmacalHandler.cfg_calendars = calendars
        return {"provider": "google", "name": entry.get("name") or "Google"}

    def _events_in_window(self, start: datetime, end: datetime, calendar_ids: list[int] | None) -> list[dict]:
        """Cached events plus expanded occurrences for [start, end).

        Series are expanded at read time, so the 5-minute wipe-and-rebuild sync
        never has to keep occurrence rows in step and every occurrence carries
        the date it was expanded from in `recurrence_id`.
        """
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        rows = db_get_window(self.db_conn, start, end, calendar_ids)
        if not rows:
            return []
        for r in rows:
            if r.get("recurrence_id") and not r.get("is_recurring"):
                r["is_recurring"] = 1   # Google instance: uid IS the occurrence
        if not any(r.get("rrule") for r in rows):
            return rows                      # nothing recurring in this window
        overrides = db_get_overrides(self.db_conn, calendar_ids)
        return expand_events(rows, overrides, start, end)

    def _reminders_in_window(self, start: datetime, end: datetime, calendar_ids: list[int] | None) -> list[dict]:
        """Cached reminders plus expanded DUE occurrences for [start, end).

        Recurring reminders expand from their DUE anchor at read time, exactly
        like events. Each occurrence carries the date it was expanded from in
        `recurrence_id`.
        """
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        rows = db_get_reminders_for_window(self.db_conn, start, end, calendar_ids)
        if not rows:
            return []
        if not any(r.get("rrule") for r in rows):
            return rows                     # nothing recurring in this window
        overrides = db_get_reminder_overrides(self.db_conn, calendar_ids)
        return expand_reminders(rows, overrides, start, end)

    def _json(self, code: int, data: object) -> None:
        payload = json.dumps(data, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("%s - %s", self.client_address[0], format % args)


# Import here to avoid circular issues at module level
from datetime import timedelta  # noqa: E402


class _OmacalHTTPServer(HTTPServer):
    """HTTPServer with address reuse enabled so restarts survive TIME_WAIT."""

    allow_reuse_address = True


class OmacalServer:
    """Thin HTTP server wrapping the omacal backend."""

    def __init__(self, config_path: Path | None = None, port: int | None = None):
        self.cfg = load_config(config_path)
        self.port = port or self.cfg.get("port", 9876)
        self.db_path = Path(self.cfg["database"])
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = None
        self._httpd = None

    def _get_conn(self):
        if self._conn is None:
            from omacal.db import open_db
            self._conn = open_db(self.db_path)
        return self._conn

    def start(self, background: bool = False):
        """Start serving. If background=True, returns the server object without blocking."""
        conn = self._get_conn()
        OmacalHandler.db_conn = conn
        OmacalHandler.cfg_calendars = self.cfg.get("calendars", [])

        # Create the HTTP server first so bind failures crash the process
        # before the periodic sync timer gets a chance to keep it alive.
        server = _OmacalHTTPServer(("127.0.0.1", self.port), OmacalHandler)
        self._httpd = server
        logger.info("omacal API listening on http://127.0.0.1:%d", self.port)

        # Schedule periodic sync in a daemon scheduler thread. The scheduler
        # always ticks (Event.wait) and runs each sync in its own worker thread,
        # so a hung sync can never stop future syncs from being scheduled. The
        # old code re-armed the next Timer only AFTER a sync returned, so one
        # hung connection (caldav had no timeout) stopped syncs forever. The
        # sync_all lock makes an overlapping run a cheap no-op ("skipped").
        import threading
        poll_interval = self.cfg.get("poll_interval", 300)
        stop = threading.Event()

        def run_sync():
            from omacal.sync import sync_all
            try:
                result = sync_all()
                if result.get("status") == "skipped":
                    logger.debug("Periodic sync skipped: a sync is already running")
                else:
                    logger.info("Periodic sync complete: %d events", result.get("total_events", 0))
            except Exception as e:
                logger.error("Periodic sync failed: %s", e)

        def scheduler():
            while not stop.wait(poll_interval):
                threading.Thread(target=run_sync, daemon=True).start()

        _scheduler = threading.Thread(target=scheduler, daemon=True)
        _scheduler.start()

        # Reminder alarm delivery: check for due alarms every 30s (same daemon
        # scheduler pattern as the sync). Each check runs in its own worker
        # thread so a hung delivery can't stall future checks.
        alarm_interval = self.cfg.get("alarm_check_interval", 30)

        def run_alarm_check():
            from omacal.sync import check_due_alarms
            try:
                fired = check_due_alarms()
                if fired:
                    logger.info("Fired %d reminder alarm(s)", len(fired))
            except Exception as e:
                logger.error("Alarm check failed: %s", e)
        def alarm_scheduler():
            while not stop.wait(alarm_interval):
                threading.Thread(target=run_alarm_check, daemon=True).start()
        _alarm_check = threading.Thread(target=alarm_scheduler, daemon=True)
        _alarm_check.start()

        if background:
            return server
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


def main():
    import argparse
    import signal

    parser = argparse.ArgumentParser(prog="omacal-api", description="omacal calendar REST API")
    parser.add_argument("--config", type=Path, default=None, help="path to config.json")
    parser.add_argument("--port", type=int, default=None, help="override listen port")
    parser.add_argument("--sync-first", action="store_true", help="run a sync before starting the server")
    parser.add_argument("--debug", action="store_true", help="verbose logging")

    args = parser.parse_args()

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=log_level, format="%(name)s %(levelname)s %(message)s", stream=sys.stderr)

    server = OmacalServer(args.config, args.port)

    if args.sync_first:
        from omacal.sync import sync_all
        logging.info("Running initial sync...")
        result = sync_all(args.config)
        logging.info("Sync complete: %s", result)

    # Handle SIGTERM/SIGINT gracefully.
    #
    # Do NOT raise SystemExit from inside the handler. The handler runs *in*
    # the serve_forever thread, so the interpreter starts tearing the process
    # down while that thread is still live ("Exception ignored while joining a
    # thread in _thread._shutdown()"), and systemd records status=1 -- the unit
    # shows "failed" after every normal stop/restart.
    #
    # HTTPServer.shutdown() blocks until serve_forever() returns, so it cannot
    # be called from the serve_forever thread either. Signal a helper thread
    # and let main() unwind normally, for a clean exit code 0.
    import threading

    def shutdown(sig, frame):
        logging.info("Shutting down (signal %s)...", sig)
        httpd = server._httpd
        if httpd is None:
            # Signal arrived before the socket was bound (e.g. during the
            # initial sync): nothing is serving, so leave now.
            raise SystemExit(0)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.start()
    finally:
        # Close the cached DB connection so the WAL is checkpointed on exit.
        if server._conn is not None:
            try:
                server._conn.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())









"""Lightweight REST API for the omacal QML frontend.

Serves events over HTTP on localhost. The QML panel fetches from here.

Endpoints:
    GET  /api/health            → {"status": "ok"}
    GET  /api/calendars         → list of calendar metadata
    GET  /api/events?start=..&end=.. → events in range
    GET  /api/events/today      → today's events
    POST /api/sync              → trigger a sync run
    POST /api/events             → create a new event
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, time, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from omacal.config import load_config
from omacal.db import (
    list_calendars as db_get_calendars,
    get_events as db_get_events,
    get_overrides as db_get_overrides,
    get_events_for_window as db_get_window,
)
from omacal.recur import expand_events

logger = logging.getLogger("omacal.api")


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
            self._json(200, {
                "status": "ok",
                "version": "0.1.0",
                "needs_setup": needs_setup(),
            })
            return

        if path == "/api/calendars":
            from omacal.keyring_store import has_password
            calendars = db_get_calendars(self.db_conn)
            for c in calendars:
                # Writability = we hold a password for this calendar in the
                # keyring. The password is keyed by the principal URL (the
                # configured URL); fall back to the calendar URL.
                from omacal.keyring_store import has_password
                key = c.get("principal_url") or c.get("url", "")
                c["writable"] = bool(c.get("username")) and has_password(key, c.get("username"))
                c.pop("password", None)
                c.pop("username", None)
                c.pop("sync_token", None)
            self._json(200, calendars)
            return

        if path == "/api/config":
            cfg_names = [c.get("display_name") for c in self._cfg_calendars() if c.get("enabled") and c.get("display_name")]
            self._json(200, {"calendar_names": cfg_names})
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
        self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
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
        self._json(404, {"error": "not found"})

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
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
        if not any(r.get("rrule") for r in rows):
            return rows                      # nothing recurring in this window
        overrides = db_get_overrides(self.db_conn, calendar_ids)
        return expand_events(rows, overrides, start, end)

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

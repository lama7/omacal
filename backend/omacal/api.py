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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from omacal.config import load_config
from omacal.db import list_calendars as db_get_calendars, get_events as db_get_events, get_today_events as db_get_today

logger = logging.getLogger("omacal.api")


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
            self._json(200, {"status": "ok", "version": "0.1.0"})
            return

        if path == "/api/calendars":
            calendars = db_get_calendars(self.db_conn)
            for c in calendars:
                c["writable"] = bool(c.get("username") and c.get("password"))
                c.pop("password", None)
                c.pop("username", None)
            self._json(200, calendars)
            return

        if path == "/api/config":
            cfg_names = [c.get("display_name") for c in self._cfg_calendars() if c.get("enabled") and c.get("display_name")]
            self._json(200, {"calendar_names": cfg_names})
            return

        if path == "/api/events/today":
            events = db_get_today(self.db_conn)
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
                # Default: next 7 days
                now = datetime.now(timezone.utc)
                start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                end = start + timedelta(days=7)

            cal_ids = None
            if cal_ids_raw:
                try:
                    cal_ids = [int(x) for x in cal_ids_raw.split(",")]
                except ValueError:
                    self._json(400, {"error": "invalid calendar ids"})
                    return

            events = db_get_events(self.db_conn, start, end, cal_ids)
            self._json(200, events)
            return

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/sync":
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                # Actually run the sync
                from omacal.sync import sync_all
                result = sync_all()
                self._json(200, result)
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

                start_dt = datetime.fromisoformat(start_str)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)

                end_dt = None
                if end_str:
                    end_dt = datetime.fromisoformat(end_str)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)

                from omacal.sync import create_event
                result = create_event(self.db_conn, calendar_id, summary, start_dt, end_dt, all_day, location)
                self._json(200, result)
            except Exception as e:
                self._json(500, {"error": str(e)})
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/events":
            query = parse_qs(parsed.query)
            uid = query.get("uid", [None])[0]
            calendar_id_raw = query.get("calendar_id", [None])[0]
            if not uid or not calendar_id_raw:
                self._json(400, {"error": "uid and calendar_id query parameters required"})
                return
            try:
                calendar_id = int(calendar_id_raw)
                from omacal.sync import delete_event
                result = delete_event(self.db_conn, calendar_id, uid)
                self._json(200, result)
            except Exception as e:
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
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = json.loads(body)
                summary = data.get("summary", "")
                calendar_id = int(data.get("calendar_id"))
                start_str = data["start"]
                end_str = data.get("end")
                all_day = data.get("all_day", False)
                location = data.get("location", "")

                start_dt = datetime.fromisoformat(start_str)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                end_dt = None
                if end_str:
                    end_dt = datetime.fromisoformat(end_str)
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=datetime.now().astimezone().tzinfo)

                from omacal.sync import update_event
                result = update_event(self.db_conn, calendar_id, uid, summary, start_dt, end_dt, all_day, location)
                self._json(200, result)
            except Exception as e:
                import traceback
                logger.error("PUT failed: %s\n%s", e, traceback.format_exc())
                self._json(500, {"error": str(e)})
            return
        self._json(404, {"error": "not found"})

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

        # Schedule periodic sync in a daemon thread (so it won't keep a
        # broken process alive if the server fails or exits).
        import threading
        poll_interval = self.cfg.get("poll_interval", 300)

        def periodic_sync():
            try:
                from omacal.sync import sync_all

                sync_all()
                logger.info("Periodic sync complete")
            except Exception as e:
                logger.error("Periodic sync failed: %s", e)
            _t = threading.Timer(poll_interval, periodic_sync)
            _t.daemon = True
            _t.start()

        _sync_timer = threading.Timer(poll_interval, periodic_sync)
        _sync_timer.daemon = True
        _sync_timer.start()

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

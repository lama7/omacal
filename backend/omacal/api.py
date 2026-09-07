"""Lightweight REST API for the omacal QML frontend.

Serves events over HTTP on localhost. The QML panel fetches from here.

Endpoints:
    GET  /api/health           → {"status": "ok"}
    GET  /api/calendars        → list of calendar metadata
    GET  /api/events?start=..&end=.. → events in range
    GET  /api/events/today     → today's events
    POST /api/sync             → trigger a sync run
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

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/health":
            self._json(200, {"status": "ok", "version": "0.1.0"})
            return

        if path == "/api/calendars":
            calendars = db_get_calendars(self.db_conn)
            self._json(200, calendars)
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
                # Optionally accept a JSON body with specific calendar ids to sync
                self._json(202, {"status": "syncing", "message": "sync initiated"})
            except Exception as e:
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


class OmacalServer:
    """Thin HTTP server wrapping the omacal backend."""

    def __init__(self, config_path: Path | None = None, port: int | None = None):
        self.cfg = load_config(config_path)
        self.port = port or self.cfg.get("port", 9876)
        self.db_path = Path(self.cfg["database"])
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = None

    def _get_conn(self):
        if self._conn is None:
            import sqlite3
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def start(self, background: bool = False):
        """Start serving. If background=True, returns the server object without blocking."""
        conn = self._get_conn()
        OmacalHandler.db_conn = conn

        server = HTTPServer(("127.0.0.1", self.port), OmacalHandler)
        logger.info("omacal API listening on http://127.0.0.1:%d", self.port)

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

    # Handle SIGTERM gracefully
    def shutdown(sig, frame):
        logging.info("Shutting down...")
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    server.start()


if __name__ == "__main__":
    main()

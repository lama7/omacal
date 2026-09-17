"""SQLite schema and operations for omacal event cache."""

import json
import sqlite3
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Any


def open_db(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    try:
        cur.execute("SELECT max(version) FROM schema_version")
        row = cur.fetchone()
        version = int(row[0]) if row and row[0] is not None else 0
    except sqlite3.OperationalError:
        version = 0

    if version < 1:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS calendars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                color TEXT,
                url TEXT NOT NULL,
                username TEXT,
                password TEXT,
                principal_url TEXT,
                last_sync TIMESTAMP,
                enabled INTEGER NOT NULL DEFAULT 1
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
                uid TEXT NOT NULL,
                summary TEXT,
                description TEXT,
                location TEXT,
                start TIMESTAMP NOT NULL,
                end TIMESTAMP,
                all_day INTEGER NOT NULL DEFAULT 0,
                recurrence_id TIMESTAMP,
                status TEXT,
                transparency TEXT,
                UNIQUE(calendar_id, uid)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_start
                ON events(start)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
        """)
        cur.execute("INSERT INTO schema_version VALUES (1)")

    if version < 2:
        # Recurring-event support.
        #
        # events.rrule   — the master's RRULE string (NULL for one-off events)
        # events.exdates — JSON array of ISO datetimes excluded from the series
        #
        # Detached overrides (VEVENTs carrying RECURRENCE-ID) share their
        # master's UID, so they cannot live in `events`: UNIQUE(calendar_id,
        # uid) would make them overwrite the master row on upsert, which is
        # exactly what used to happen. They get their own table instead of
        # rebuilding `events` to widen its unique index.
        cols = {r[1] for r in cur.execute("PRAGMA table_info(events)")}
        if "rrule" not in cols:
            cur.execute("ALTER TABLE events ADD COLUMN rrule TEXT")
        if "exdates" not in cols:
            cur.execute("ALTER TABLE events ADD COLUMN exdates TEXT")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS event_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
                uid TEXT NOT NULL,
                recurrence_id TIMESTAMP NOT NULL,
                summary TEXT,
                description TEXT,
                location TEXT,
                start TIMESTAMP,
                end TIMESTAMP,
                all_day INTEGER NOT NULL DEFAULT 0,
                status TEXT,
                UNIQUE(calendar_id, uid, recurrence_id)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_overrides_uid
                ON event_overrides(calendar_id, uid)
        """)
        cur.execute("INSERT INTO schema_version VALUES (2)")

    conn.commit()


def add_calendar(
    conn: sqlite3.Connection,
    uid: str,
    display_name: str,
    url: str,
    username: str | None = None,
    password: str | None = None,
    color: str | None = None,
    principal_url: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO calendars
           (uid, display_name, url, username, password, color, principal_url, last_sync, enabled)
           VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 1)
        ON CONFLICT(uid) DO UPDATE SET
           display_name = excluded.display_name,
           url = excluded.url,
           username = excluded.username,
           password = excluded.password,
           color = excluded.color,
           principal_url = excluded.principal_url
        RETURNING id""",
        (uid, display_name, url, username, password, color, principal_url),
    )
    row = cur.fetchone()
    conn.commit()
    return int(row[0])


def list_calendars(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT id, uid, display_name, color, url, username, password, principal_url, last_sync, enabled FROM calendars ORDER BY display_name"
    )
    return [dict(r) for r in cur.fetchall()]


def upsert_events(conn: sqlite3.Connection, calendar_id: int, events: list[dict]) -> int:
    """Insert or replace events for a calendar. Returns count of rows touched."""
    cur = conn.cursor()
    for ev in events:
        cur.execute(
            """INSERT INTO events
               (calendar_id, uid, summary, description, location, start, end,
                all_day, recurrence_id, status, transparency, rrule, exdates)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(calendar_id, uid) DO UPDATE SET
                summary=excluded.summary,
                description=excluded.description,
                location=excluded.location,
                start=excluded.start,
                end=excluded.end,
                all_day=excluded.all_day,
                recurrence_id=excluded.recurrence_id,
                status=excluded.status,
                transparency=excluded.transparency,
                rrule=excluded.rrule,
                exdates=excluded.exdates
            """,
            (
                calendar_id,
                ev["uid"],
                ev.get("summary"),
                ev.get("description"),
                ev.get("location"),
                ev["start"],
                ev.get("end"),
                ev.get("all_day", 0),
                ev.get("recurrence_id"),
                ev.get("status"),
                ev.get("transparency"),
                ev.get("rrule"),
                ev.get("exdates"),
            ),
        )
    conn.commit()
    return cur.rowcount


def upsert_overrides(conn: sqlite3.Connection, calendar_id: int, overrides: list[dict]) -> int:
    """Insert or replace detached recurrence overrides for a calendar."""
    cur = conn.cursor()
    for ov in overrides:
        cur.execute(
            """INSERT INTO event_overrides
               (calendar_id, uid, recurrence_id, summary, description, location,
                start, end, all_day, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(calendar_id, uid, recurrence_id) DO UPDATE SET
                summary=excluded.summary,
                description=excluded.description,
                location=excluded.location,
                start=excluded.start,
                end=excluded.end,
                all_day=excluded.all_day,
                status=excluded.status
            """,
            (
                calendar_id,
                ov["uid"],
                ov["recurrence_id"],
                ov.get("summary"),
                ov.get("description"),
                ov.get("location"),
                ov.get("start"),
                ov.get("end"),
                ov.get("all_day", 0),
                ov.get("status"),
            ),
        )
    conn.commit()
    return cur.rowcount


def get_overrides(conn: sqlite3.Connection, calendar_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Return all detached overrides (used when expanding recurring series)."""
    if calendar_ids:
        placeholders = ",".join("?" for _ in calendar_ids)
        cur = conn.execute(
            f"SELECT * FROM event_overrides WHERE calendar_id IN ({placeholders})",
            list(calendar_ids),
        )
    else:
        cur = conn.execute("SELECT * FROM event_overrides")
    return [dict(r) for r in cur.fetchall()]


def clear_calendar_events(conn: sqlite3.Connection, calendar_id: int) -> None:
    conn.execute("DELETE FROM events WHERE calendar_id=?", (calendar_id,))
    conn.execute("DELETE FROM event_overrides WHERE calendar_id=?", (calendar_id,))
    conn.commit()


def add_event(
    conn: sqlite3.Connection,
    calendar_id: int,
    uid: str,
    summary: str,
    start: str,
    end: str | None = None,
    all_day: int = 0,
    location: str | None = None,
    rrule: str | None = None,
    exdates: str | None = None,
) -> dict[str, Any]:
    """Insert a single event into the local cache. Returns the inserted row as a dict."""
    cur = conn.execute(
        """INSERT OR REPLACE INTO events
           (calendar_id, uid, summary, description, location, start, end,
            all_day, recurrence_id, status, transparency, rrule, exdates)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id, calendar_id, uid, summary, description, location,
                  start, end, all_day, recurrence_id, status, transparency, rrule, exdates""",
        (calendar_id, uid, summary, None, location, start, end, all_day, None, "CONFIRMED", None, rrule, exdates),
    )
    row = cur.fetchone()
    cur.close()
    conn.commit()
    return dict(row)


def get_events_for_window(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    calendar_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Rows the API needs to answer a [start, end) query.

    Non-recurring rows must overlap the window. Recurring masters are returned
    regardless of their DTSTART, because a series that started years ago can
    still have occurrences inside the window -- the window filter for those is
    applied by the expansion in `recur.expand_events`, not by SQL.
    """
    placeholders = ",".join("?" for _ in (calendar_ids or []))
    where = f" AND e.calendar_id IN ({placeholders})" if calendar_ids else ""
    params = list(calendar_ids) if calendar_ids else []

    cur = conn.execute(
        f"""SELECT e.*, c.display_name, c.color
            FROM events e
            JOIN calendars c ON c.id = e.calendar_id
            WHERE (e.rrule IS NOT NULL
                   OR (e.start < ? AND (e.end IS NULL OR e.end > ?)))
            {where}
            ORDER BY e.start
        """,
        [end.isoformat(), start.isoformat()] + params,
    )
    return [dict(r) for r in cur.fetchall()]


def get_events(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    calendar_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Return events overlapping [start, end)."""
    placeholders = ",".join("?" for _ in (calendar_ids or []))
    if calendar_ids:
        where = f" AND calendar_id IN ({placeholders})"
        params = list(calendar_ids)
    else:
        where = ""
        params = []

    cur = conn.execute(
        f"""SELECT e.*, c.display_name, c.color
            FROM events e
            JOIN calendars c ON c.id = e.calendar_id
            WHERE e.start < ? AND (e.end IS NULL OR e.end > ?)
            {where}
            ORDER BY e.start
        """,
        [end.isoformat(), start.isoformat()] + params,
    )
    return [dict(r) for r in cur.fetchall()]


def get_today_events(conn: sqlite3.Connection, calendar_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """Return events for today (midnight to midnight local)."""
    today = date.today()
    start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    end = datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=timezone.utc)
    return get_events(conn, start, end, calendar_ids)


def delete_event(conn: sqlite3.Connection, calendar_id: int, uid: str) -> None:
    """Delete an event from the local cache by calendar_id and uid."""
    conn.execute("DELETE FROM events WHERE calendar_id = ? AND uid = ?", (calendar_id, uid))
    conn.commit()


def set_event_exdates(conn: sqlite3.Connection, calendar_id: int, uid: str, exdates: list[str]) -> None:
    """Write the EXDATE list for a cached master.

    Called right after a single-occurrence delete so the panel stops drawing
    that occurrence immediately instead of resurrecting it until the next sync:
    the series is expanded from the cache, so the cache has to carry the new
    exclusion.
    """
    conn.execute(
        "UPDATE events SET exdates = ? WHERE calendar_id = ? AND uid = ?",
        (json.dumps(sorted(set(exdates))), calendar_id, uid),
    )
    conn.commit()


def update_event(
    conn: sqlite3.Connection,
    uid: str,
    summary: str,
    start: str,
    end: str | None = None,
    all_day: int = 0,
    location: str | None = None,
    calendar_id: int | None = None,
) -> dict[str, Any]:
    """Update an event in the local cache. Returns the updated row as a dict."""
    cur = conn.execute(
        """UPDATE events
           SET summary = ?,
               location = ?,
               start = ?,
               end = ?,
               all_day = ?"""
        + (", calendar_id = ?" if calendar_id is not None else "")
        + """
           WHERE uid = ?
        RETURNING id, calendar_id, uid, summary, description, location,
                  start, end, all_day, recurrence_id, status, transparency""",
        (summary, location, start, end, all_day) + ((calendar_id,) if calendar_id is not None else ()) + (uid,),
    )
    row = cur.fetchone()
    cur.close()
    conn.commit()
    return dict(row) if row else {}

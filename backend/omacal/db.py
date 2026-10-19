"""SQLite schema and operations for omacal event cache."""

import json
import sqlite3
from datetime import datetime, timezone
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

    if version < 3:
        # Incremental sync (RFC 6578 sync-collection). Each calendar stores the
        # server's sync token; on SabreDAV this is also the getctag, so one value
        # is both the change-detection cursor and the incremental-sync cursor.
        cols = {r[1] for r in cur.execute("PRAGMA table_info(calendars)")}
        if "sync_token" not in cols:
            cur.execute("ALTER TABLE calendars ADD COLUMN sync_token TEXT")
        cur.execute("INSERT INTO schema_version VALUES (3)")

    if version < 4:
        # events.href -- the CalDAV resource URL. Needed to match a deleted
        # resource (sync-collection reports deletions as hrefs) back to a cached
        # row: the filename UID and the iCal UID differ, so the href is the only
        # stable key. Populated on the next full sync.
        cols = {r[1] for r in cur.execute("PRAGMA table_info(events)")}
        if "href" not in cols:
            cur.execute("ALTER TABLE events ADD COLUMN href TEXT")
        cur.execute("INSERT INTO schema_version VALUES (4)")

    if version < 5:
        # event_overrides.href -- same deletion-matching need as events.href.
        cols = {r[1] for r in cur.execute("PRAGMA table_info(event_overrides)")}
        if "href" not in cols:
            cur.execute("ALTER TABLE event_overrides ADD COLUMN href TEXT")
        cur.execute("INSERT INTO schema_version VALUES (5)")

    if version < 6:
        # events.tzid / event_overrides.tzid -- the IANA zone name of a timed
        # event's DTSTART. Start/end are cached as `isoformat()` which bakes in
        # a FIXED offset and drops the zone name, so a tz-aware series expanded
        # from cache drifts 1 h after a DST change, and a TZID-aware EXDATE /
        # detached override written by another client never matches the cached
        # occurrence (a deleted occurrence reappears). Persisting the TZID lets
        # expansion re-attach a real (DST-aware) ZoneInfo at load time. NULL for
        # all-day events (date-valued, no zone).
        cols = {r[1] for r in cur.execute("PRAGMA table_info(events)")}
        if "tzid" not in cols:
            cur.execute("ALTER TABLE events ADD COLUMN tzid TEXT")
        cols = {r[1] for r in cur.execute("PRAGMA table_info(event_overrides)")}
        if "tzid" not in cols:
            cur.execute("ALTER TABLE event_overrides ADD COLUMN tzid TEXT")
        # Existing cached rows have NULL tzid and a token-based incremental sync
        # will NOT re-fetch unchanged events, so the fix would never reach them.
        # Clear each calendar's sync token once so the next sync does a full
        # rebuild and repopulates tzid from the server.
        cur.execute("UPDATE calendars SET sync_token = NULL")
        cur.execute("INSERT INTO schema_version VALUES (6)")

    if version < 7:
        # Passwords no longer live in the DB — they moved to the system
        # keyring. Wipe any plaintext password already cached so the secret
        # exists in exactly one place. The column stays (dropping it would
        # require a table rebuild) but is never written again.
        cur.execute("UPDATE calendars SET password = NULL")
        cur.execute("INSERT INTO schema_version VALUES (7)")

    if version < 9:
        # Whether a calendar's server advertises VTODO (task) support. Lets the
        # panel route reminders to the right calendar instead of showing every
        # event calendar. Defaults to 0 (events); discovery rewrites it.
        cols = {r[1] for r in cur.execute("PRAGMA table_info(calendars)")}
        if "supports_todo" not in cols:
            cur.execute("ALTER TABLE calendars ADD COLUMN supports_todo INTEGER NOT NULL DEFAULT 0")
        cur.execute("INSERT INTO schema_version VALUES (9)")

    if version < 8:
        # Reminder support. A reminder is a CalDAV VTODO: it has a DUE date
        # (when it fires / needs doing) rather than an event's DTEND, plus
        # status and priority. Recurrence is carried by rrule/exdates exactly
        # like events and expanded from DUE. Detached RECURRENCE-ID VTODOs
        # share their master's UID, so they live in a separate table (same
        # reason as event_overrides) keyed by (calendar_id, uid, recurrence_id).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
                uid TEXT NOT NULL,
                summary TEXT,
                description TEXT,
                location TEXT,
                start TIMESTAMP,           -- DTSTART (may be NULL)
                due TIMESTAMP,             -- DUE: the display / fire date
                all_day INTEGER NOT NULL DEFAULT 0,
                status TEXT,
                priority INTEGER,
                rrule TEXT,
                exdates TEXT,              -- JSON array of ISO datetimes
                href TEXT,
                tzid TEXT,
                UNIQUE(calendar_id, uid)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reminder_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                calendar_id INTEGER NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
                uid TEXT NOT NULL,
                recurrence_id TIMESTAMP NOT NULL,
                summary TEXT,
                description TEXT,
                location TEXT,
                start TIMESTAMP,
                due TIMESTAMP,
                all_day INTEGER NOT NULL DEFAULT 0,
                status TEXT,
                priority INTEGER,
                href TEXT,
                tzid TEXT,
                UNIQUE(calendar_id, uid, recurrence_id)
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_reminders_due
                ON reminders(due)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_reminder_overrides_uid
                ON reminder_overrides(calendar_id, uid)
        """)
        cur.execute("INSERT INTO schema_version VALUES (8)")

    if version < 10:
        # Reminder alarms (2026-09). A reminder can carry a heads-up that
        # fires before DUE. Timed reminders use a relative VALARM offset
        # (alarm_offset_seconds, default 3600 = 1h before DUE); all-day
        # reminders use an absolute alarm time on the due date
        # (alarm_time, "HH:MM"). The fired table dedupes so a recurring
        # reminder toasts each occurrence once.
        rcols = {r[1] for r in cur.execute("PRAGMA table_info(reminders)")}
        if "alarm_offset_seconds" not in rcols:
            cur.execute("ALTER TABLE reminders ADD COLUMN alarm_offset_seconds INTEGER")
        if "alarm_time" not in rcols:
            cur.execute("ALTER TABLE reminders ADD COLUMN alarm_time TEXT")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reminder_alarms_fired (
                calendar_id INTEGER NOT NULL,
                uid TEXT NOT NULL,
                occurrence_due TEXT NOT NULL,   -- ISO instant of the DUE that fired
                fired_at TIMESTAMP NOT NULL,
                PRIMARY KEY (calendar_id, uid, occurrence_due)
            )
        """)
        cur.execute("INSERT INTO schema_version VALUES (10)")

    if version < 11:
        # Subscribed calendars (2026-10). A calendar added by URL (subscribe-by-URL
        # in the add-calendar form) is not part of the configured account's
        # discovery, so the stale-calendar prune must never treat it as stale.
        # 1 = added by URL; the sync's prune exempts these rows and the periodic
        # sync refreshes them directly.
        cols = {r[1] for r in cur.execute("PRAGMA table_info(calendars)")}
        if "subscribed" not in cols:
            cur.execute("ALTER TABLE calendars ADD COLUMN subscribed INTEGER NOT NULL DEFAULT 0")
        cur.execute("INSERT INTO schema_version VALUES (11)")

    if version < 12:
        # Calendar provenance (2026-10, google branch). 'caldav' rows are the
        # existing DAV-backed calendars; 'google' rows are backed by the
        # Google Calendar API v3 adapter and carry the Google calendar id in
        # `uid`. Drives which sync/edit/delete/event path a row takes.
        cols = {r[1] for r in cur.execute("PRAGMA table_info(calendars)")}
        if "provider" not in cols:
            cur.execute("ALTER TABLE calendars ADD COLUMN provider TEXT NOT NULL DEFAULT 'caldav'")
        cur.execute("INSERT INTO schema_version VALUES (12)")

    if version < 13:
        # Discovery cache (2026-10). Calendar discovery (principal lookup + a
        # properties/privilege PROPFIND per calendar) used to run on EVERY
        # sync even though the calendar list changes rarely. On Nextcloud that
        # burst is what trips the server's rate limiter, and once tripped every
        # poll re-triggered it. Cache the discovered list per account and reuse
        # it for `discovery_interval` seconds; a failed discovery falls back to
        # the last cached list instead of failing the whole source.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS discovery_cache (
                account_url TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                discovered_at TIMESTAMP NOT NULL
            )
        """)
        cur.execute("INSERT INTO schema_version VALUES (13)")

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
    supports_todo: int = 0,
    subscribed: int = 0,
    provider: str = "caldav",
) -> int:
    # The password is never stored in the DB — it lives in the keyring. The
    # column is kept for schema stability but always written as NULL.
    # `subscribed` is deliberately NOT in the ON CONFLICT update: discovery
    # (which passes 0) must never clear the flag of a URL-added row.
    # `provider` likewise: discovery passes 'caldav' and must never rewrite
    # the provenance of an existing google row.
    cur = conn.execute(
        """INSERT INTO calendars
           (uid, display_name, url, username, password, color, principal_url, last_sync, enabled, supports_todo, subscribed, provider)
           VALUES (?, ?, ?, ?, NULL, ?, ?, NULL, 1, ?, ?, ?)
        ON CONFLICT(uid) DO UPDATE SET
           display_name = excluded.display_name,
           url = excluded.url,
           username = excluded.username,
           password = NULL,
           color = excluded.color,
           principal_url = excluded.principal_url,
           supports_todo = excluded.supports_todo
        RETURNING id""",
        (uid, display_name, url, username, color, principal_url, supports_todo, subscribed, provider),
    )
    row = cur.fetchone()
    conn.commit()
    return int(row[0])


def list_calendars(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT id, uid, display_name, color, url, username, password, principal_url, last_sync, sync_token, enabled, supports_todo, subscribed, provider FROM calendars ORDER BY display_name"
    )
    return [dict(r) for r in cur.fetchall()]


def get_sync_token(conn: sqlite3.Connection, calendar_id: int) -> str | None:
    """The calendar's stored RFC 6578 sync token (also the getctag on SabreDAV)."""
    cur = conn.execute("SELECT sync_token FROM calendars WHERE id = ?", (calendar_id,))
    row = cur.fetchone()
    return row["sync_token"] if row else None


def set_sync_token(conn: sqlite3.Connection, calendar_id: int, token: str | None) -> None:
    """Persist the sync token after a successful sync-collection REPORT."""
    conn.execute("UPDATE calendars SET sync_token = ? WHERE id = ?", (token, calendar_id))
    conn.commit()


def set_last_sync(conn: sqlite3.Connection, calendar_id: int) -> None:
    """Stamp a calendar as freshly synced (for the API's staleness surface)."""
    conn.execute("UPDATE calendars SET last_sync = ? WHERE id = ?",
                 (datetime.now(timezone.utc).isoformat(), calendar_id))
    conn.commit()


def get_discovered_calendars(
    conn: sqlite3.Connection,
    account_url: str,
    max_age_seconds: int | None = None,
) -> list[dict[str, Any]] | None:
    """The cached discovery list for an account, or None.

    ``max_age_seconds`` limits how old a cached list may be; None returns the
    last list regardless of age (used as a fallback when fresh discovery
    fails). A malformed/empty payload is treated as a cache miss so a bad row
    can never wedge the sync.
    """
    row = conn.execute(
        "SELECT payload, discovered_at FROM discovery_cache WHERE account_url = ?",
        (account_url,),
    ).fetchone()
    if not row:
        return None
    if max_age_seconds is not None:
        try:
            age = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(row["discovered_at"])
            ).total_seconds()
        except (ValueError, TypeError):
            return None
        if age > max_age_seconds:
            return None
    try:
        data = json.loads(row["payload"])
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, list) and data else None


def set_discovered_calendars(
    conn: sqlite3.Connection,
    account_url: str,
    calendars: list[dict[str, Any]],
) -> None:
    """Persist an account's discovery list (JSON) after a successful discovery."""
    conn.execute(
        """INSERT INTO discovery_cache (account_url, payload, discovered_at)
           VALUES (?, ?, ?)
           ON CONFLICT(account_url) DO UPDATE SET
             payload = excluded.payload,
             discovered_at = excluded.discovered_at""",
        (account_url, json.dumps(calendars), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def set_calendar_enabled(conn: sqlite3.Connection, calendar_id: int, enabled: bool) -> bool:
    """Set a calendar's visibility flag (set, not flip).

    The flag is read by the event/reminder window queries (``c.enabled = 1``),
    by the panel's ``/api/calendars`` listing, and by sync (which skips disabled
    calendars). Setting it off hides the calendar's events/reminders from the
    panel immediately and skips it on future syncs. Returns True if a row was
    updated, False if the calendar id is unknown.
    """
    cur = conn.execute("UPDATE calendars SET enabled = ? WHERE id = ?",
                       (1 if enabled else 0, calendar_id))
    conn.commit()
    return cur.rowcount > 0


def set_account_enabled(
    conn: sqlite3.Connection, key: str, provider: str, enabled: bool
) -> int:
    """Flip the enabled flag on every cached calendar row of one ACCOUNT.

    Accounts are ``config.json`` ``calendars[]`` entries. Google is a single
    account (``provider='google'``); CalDAV accounts all share
    ``provider='caldav'`` and are distinguished by ``principal_url``, so for
    them the key (the account's principal URL) is matched against the row's
    principal_url. Soft disable: rows and events stay in the DB; the
    event/reminder queries filter on ``c.enabled = 1``, so a disabled account's
    calendars are neither viewed nor altered until re-enabled — and re-enabling
    is instant because sync_token/last_sync survive the disable. Returns the
    number of rows updated.
    """
    if provider == "google":
        cur = conn.execute(
            "UPDATE calendars SET enabled = ? WHERE provider = 'google'",
            (1 if enabled else 0,),
        )
    else:
        cur = conn.execute(
            "UPDATE calendars SET enabled = ? WHERE rtrim(principal_url, '/') = ?",
            (1 if enabled else 0, (key or "").rstrip("/")),
        )
    conn.commit()
    return cur.rowcount


def upsert_events(conn: sqlite3.Connection, calendar_id: int, events: list[dict], commit: bool = True) -> int:
    """Insert or replace events for a calendar. Returns count of rows touched.

    commit=False leaves the caller's transaction open, for replace_calendar_events.
    """
    cur = conn.cursor()
    for ev in events:
        cur.execute(
                    """INSERT INTO events
                       (calendar_id, uid, summary, description, location, start, end,
                        all_day, recurrence_id, status, transparency, rrule, exdates, href, tzid)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                       exdates=excluded.exdates,
                       href=excluded.href,
                       tzid=excluded.tzid
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
                        ev.get("href"),
                        ev.get("tzid"),
                    ),
                )
    if commit:
        conn.commit()
    return cur.rowcount


def upsert_overrides(conn: sqlite3.Connection, calendar_id: int, overrides: list[dict], commit: bool = True) -> int:
    """Insert or replace detached recurrence overrides for a calendar."""
    cur = conn.cursor()
    for ov in overrides:
        cur.execute(
                    """INSERT INTO event_overrides
                       (calendar_id, uid, recurrence_id, summary, description, location,
                        start, end, all_day, status, href, tzid)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(calendar_id, uid, recurrence_id) DO UPDATE SET
                       summary=excluded.summary,
                       description=excluded.description,
                       location=excluded.location,
                       start=excluded.start,
                       end=excluded.end,
                       all_day=excluded.all_day,
                       status=excluded.status,
                       href=excluded.href,
                       tzid=excluded.tzid
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
                        ov.get("href"),
                        ov.get("tzid"),
                    ),
                )
    if commit:
        conn.commit()
    return cur.rowcount


def replace_calendar_events(
    conn: sqlite3.Connection,
    calendar_id: int,
    events: list[dict],
    overrides: list[dict] | None = None,
) -> int:
    """Swap a calendar's cached rows for a freshly fetched set, in ONE transaction.

    A reader (GET /api/events) either sees the old set or the new one -- never a
    calendar mid-wipe. An empty fetch legitimately clears the calendar; a failed
    fetch must not reach here at all (leave the previous cache in place).
    """
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM events WHERE calendar_id=?", (calendar_id,))
        cur.execute("DELETE FROM event_overrides WHERE calendar_id=?", (calendar_id,))
        if events:
            upsert_events(conn, calendar_id, events, commit=False)
        if overrides:
            upsert_overrides(conn, calendar_id, overrides, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(events)


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


# ---------------------------------------------------------------------------
# Reminders (CalDAV VTODO)
# ---------------------------------------------------------------------------

_reminder_cols = (
"(calendar_id, uid, summary, description, location, start, due, all_day, "
"status, priority, rrule, exdates, href, tzid, alarm_offset_seconds, alarm_time)"
)


def upsert_reminders(conn: sqlite3.Connection, calendar_id: int, reminders: list[dict], commit: bool = True) -> int:
    """Insert or replace reminders for a calendar. commit=False for replace_calendar_reminders."""
    cur = conn.cursor()
    for rm in reminders:
        cur.execute(
            f"""INSERT INTO reminders {_reminder_cols}
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(calendar_id, uid) DO UPDATE SET
                summary=excluded.summary,
                description=excluded.description,
                location=excluded.location,
                start=excluded.start,
                due=excluded.due,
                all_day=excluded.all_day,
                status=excluded.status,
                priority=excluded.priority,
                rrule=excluded.rrule,
                exdates=excluded.exdates,
                href=excluded.href,
                tzid=excluded.tzid,
                alarm_offset_seconds=excluded.alarm_offset_seconds,
                alarm_time=excluded.alarm_time
            """,
            (
                calendar_id,
                rm["uid"],
                rm.get("summary"),
                rm.get("description"),
                rm.get("location"),
                rm.get("start"),
                rm.get("due"),
                rm.get("all_day", 0),
                rm.get("status"),
                rm.get("priority"),
                rm.get("rrule"),
                rm.get("exdates"),
rm.get("href"),
rm.get("tzid"),
rm.get("alarm_offset_seconds"),
rm.get("alarm_time"),
            ),
        )
    if commit:
        conn.commit()
    return cur.rowcount


def upsert_reminder_overrides(conn: sqlite3.Connection, calendar_id: int, overrides: list[dict], commit: bool = True) -> int:
    """Insert or replace detached RECURRENCE-ID reminder overrides."""
    cur = conn.cursor()
    for ov in overrides:
        cur.execute(
            """INSERT INTO reminder_overrides
               (calendar_id, uid, recurrence_id, summary, description, location,
                start, due, all_day, status, priority, href, tzid)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(calendar_id, uid, recurrence_id) DO UPDATE SET
               summary=excluded.summary,
               description=excluded.description,
               location=excluded.location,
               start=excluded.start,
               due=excluded.due,
               all_day=excluded.all_day,
               status=excluded.status,
               priority=excluded.priority,
               href=excluded.href,
               tzid=excluded.tzid
            """,
            (
                calendar_id,
                ov["uid"],
                ov["recurrence_id"],
                ov.get("summary"),
                ov.get("description"),
                ov.get("location"),
                ov.get("start"),
                ov.get("due"),
                ov.get("all_day", 0),
                ov.get("status"),
                ov.get("priority"),
                ov.get("href"),
                ov.get("tzid"),
            ),
        )
    if commit:
        conn.commit()
    return cur.rowcount


def replace_calendar_reminders(
    conn: sqlite3.Connection,
    calendar_id: int,
    reminders: list[dict],
    overrides: list[dict] | None = None,
) -> int:
    """Atomically swap a calendar's cached reminders for a freshly fetched set."""
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM reminders WHERE calendar_id=?", (calendar_id,))
        cur.execute("DELETE FROM reminder_overrides WHERE calendar_id=?", (calendar_id,))
        if reminders:
            upsert_reminders(conn, calendar_id, reminders, commit=False)
        if overrides:
            upsert_reminder_overrides(conn, calendar_id, overrides, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(reminders)


def get_reminder_overrides(conn: sqlite3.Connection, calendar_ids: list[int] | None = None) -> list[dict[str, Any]]:
    """All detached reminder overrides (used when expanding recurring reminders)."""
    if calendar_ids:
        placeholders = ",".join("?" for _ in calendar_ids)
        cur = conn.execute(
            f"SELECT * FROM reminder_overrides WHERE calendar_id IN ({placeholders})",
            list(calendar_ids),
        )
    else:
        cur = conn.execute("SELECT * FROM reminder_overrides")
    return [dict(r) for r in cur.fetchall()]


def get_reminders_for_window(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    calendar_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Rows the API needs to answer a reminders [start, end) query.

    Non-recurring reminders must have a DUE inside the window. Recurring masters
    are returned regardless (their DUE may be years old) -- the window filter for
    those is applied by expand_reminders, not SQL, exactly like events.
    """
    placeholders = ",".join("?" for _ in (calendar_ids or []))
    where = f" AND r.calendar_id IN ({placeholders})" if calendar_ids else ""
    params = list(calendar_ids) if calendar_ids else []
    cur = conn.execute(
        f"""SELECT r.*, c.display_name, c.color
            FROM reminders r
            JOIN calendars c ON c.id = r.calendar_id
            WHERE (r.due IS NOT NULL AND r.due >= ? AND r.due < ? OR r.rrule IS NOT NULL) AND c.enabled = 1
            {where}
            ORDER BY r.due""",
        [start, end] + params,
    )
    return [dict(r) for r in cur.fetchall()]


def add_reminder(
    conn: sqlite3.Connection,
    calendar_id: int,
    uid: str,
    summary: str,
    due: str | None = None,
    start: str | None = None,
    all_day: int = 0,
    location: str | None = None,
    description: str | None = None,
    status: str = "NEEDS-ACTION",
    priority: int | None = None,
    rrule: str | None = None,
    exdates: str | None = None,
    href: str | None = None,
    tzid: str | None = None,
    alarm_offset_seconds: int | None = None,
    alarm_time: str | None = None,
) -> dict[str, Any]:
    """Insert a single reminder into the local cache. Returns the row as a dict."""
    cur = conn.execute(
        f"""INSERT OR REPLACE INTO reminders {_reminder_cols}
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id, calendar_id, uid, summary, description, location,
                  start, due, all_day, status, priority, rrule, exdates, href, tzid,
                  alarm_offset_seconds, alarm_time""",
        (calendar_id, uid, summary, description, location, start, due, all_day,
         status, priority, rrule, exdates, href, tzid, alarm_offset_seconds, alarm_time),
    )
    row = cur.fetchone()
    cur.close()
    conn.commit()
    return dict(row)


def delete_reminder(conn: sqlite3.Connection, calendar_id: int, uid: str) -> bool:
    """Remove a reminder (and any detached overrides) for a calendar. Returns True if a row was removed."""
    cur = conn.execute("DELETE FROM reminders WHERE calendar_id=? AND uid=?", (calendar_id, uid))
    cur.execute("DELETE FROM reminder_overrides WHERE calendar_id=? AND uid=?", (calendar_id, uid))
    conn.commit()
    return cur.rowcount > 0


def set_reminder_href(conn: sqlite3.Connection, calendar_id: int, uid: str, href: str) -> None:
    """Record the server resource path for a reminder (needed for incremental-sync deletions)."""
    conn.execute("UPDATE reminders SET href=? WHERE calendar_id=? AND uid=?", (href, calendar_id, uid))
    conn.commit()



def mark_alarm_fired(conn: sqlite3.Connection, calendar_id: int, uid: str, occurrence_due: str, fired_at: str | None = None) -> None:
    """Record that a reminder occurrence's alarm has been toasted (dedupes recurring reminders)."""
    fired_at = fired_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO reminder_alarms_fired (calendar_id, uid, occurrence_due, fired_at) VALUES (?, ?, ?, ?)",
        (calendar_id, uid, occurrence_due, fired_at),
    )
    conn.commit()


def is_alarm_fired(conn: sqlite3.Connection, calendar_id: int, uid: str, occurrence_due: str) -> bool:
    """True if this reminder occurrence's alarm has already been toasted."""
    cur = conn.execute(
        "SELECT 1 FROM reminder_alarms_fired WHERE calendar_id=? AND uid=? AND occurrence_due=?",
        (calendar_id, uid, occurrence_due),
    )
    return cur.fetchone() is not None

def add_event(
    conn: sqlite3.Connection,
    calendar_id: int,
    uid: str,
    summary: str,
    start: str,
    end: str | None = None,
    all_day: int = 0,
    location: str | None = None,
    description: str | None = None,
    status: str = "CONFIRMED",
    transparency: str | None = None,
    rrule: str | None = None,
    exdates: str | None = None,
    href: str | None = None,
    tzid: str | None = None,
) -> dict[str, Any]:
    """Insert a single event into the local cache. Returns the inserted row as a dict."""
    cur = conn.execute(
        """INSERT OR REPLACE INTO events
           (calendar_id, uid, summary, description, location, start, end,
            all_day, recurrence_id, status, transparency, rrule, exdates, href, tzid)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id, calendar_id, uid, summary, description, location,
                  start, end, all_day, recurrence_id, status, transparency, rrule, exdates, href, tzid""",
        (calendar_id, uid, summary, description, location, start, end, all_day, None, status, transparency, rrule, exdates, href, tzid),
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
                   OR (e.start < ? AND (e.end IS NULL OR e.end > ?))) AND c.enabled = 1
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






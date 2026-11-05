"""Shared helpers for the backend test suite.

Run with stdlib unittest — the install is hash-locked
(`backend/requirements.lock`, `pip install --require-hashes`), so the suite must
not add a dependency. From `backend/`:

    .venv/bin/python3 -m unittest discover -s tests -t . -v

`TempDb` gives a throwaway SQLite cache built by the real `db.open_db`/
`_migrate` (so the tests exercise the shipped schema, not a hand-written one),
and the `Fake*` classes stand in for caldav resources so a write path can be
tested end to end — what gets PUT to the server, what lands in the cache —
without a live CalDAV server.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from icalendar import Calendar

from omacal import db

try:  # sync.py catches caldav.error.NotFoundError; mirror the same symbol.
    from caldav import error as caldav_error
except ImportError:  # pragma: no cover - caldav always ships this
    from caldav.lib import error as caldav_error  # type: ignore[no-redef]


UTC = timezone.utc


def utc(*args: int) -> datetime:
    """Aware UTC datetime shorthand: utc(2026, 11, 4, 22, 30)."""
    return datetime(*args, tzinfo=UTC)


class TempDb:
    """A throwaway cache DB with the real schema; closed+removed on cleanup."""

    def __init__(self, testcase) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="omacal-tests-"))
        self.path = self.dir / "calendar.db"
        self.conn = db.open_db(self.path)
        testcase.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        try:
            self.conn.close()
        finally:
            shutil.rmtree(self.dir, ignore_errors=True)

    def add_calendar(
        self,
        uid: str = "test-cal",
        display_name: str = "Test Calendar",
        url: str = "https://example.test/calendars/user/test-cal/",
        username: str | None = None,
        **kwargs,
    ) -> int:
        """A cached calendar row.

        `username` stays None by default so the write paths never reach the
        system keyring (credentials are resolved only when a username is set).
        """
        return db.add_calendar(
            self.conn, uid=uid, display_name=display_name, url=url,
            username=username, **kwargs,
        )


def todo_text(
    uid: str = "todo-1",
    summary: str = "Test reminder",
    due: str | None = "20260915T120000Z",
    dtstart: str | None = None,
    rrule: str | None = None,
    all_day: bool = False,
    exdate: str | None = None,
) -> str:
    """Raw iCalendar VTODO text, as the server would hand it back."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//omacal//tests//EN",
        "BEGIN:VTODO",
        f"UID:{uid}",
        f"SUMMARY:{summary}",
    ]
    if all_day:
        if due:
            lines.append(f"DUE;VALUE=DATE:{due}")
        if dtstart:
            lines.append(f"DTSTART;VALUE=DATE:{dtstart}")
    else:
        if due:
            lines.append(f"DUE:{due}")
        if dtstart:
            lines.append(f"DTSTART:{dtstart}")
    if rrule:
        lines.append(f"RRULE:{rrule}")
    if exdate:
        lines.append(f"EXDATE:{exdate}")
    lines.append("STATUS:NEEDS-ACTION")
    lines.append("END:VTODO")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


class FakeTodo:
    """Stands in for a caldav VTODO resource: raw iCal text + save/delete."""

    def __init__(self, data: str) -> None:
        self.data = data
        self.saved = 0
        self.deleted = False

    def delete(self) -> None:
        self.deleted = True

    def save(self) -> None:
        self.saved += 1

    # --- assertions helpers -------------------------------------------------
    def components(self) -> list:
        return Calendar.from_ical(self.data).walk("VTODO")

    def master(self):
        for comp in self.components():
            if not comp.get("RECURRENCE-ID"):
                return comp
        return None

    def exdate_strings(self) -> list[str]:
        """EXDATE values as ISO-ish strings, for assertion."""
        comp = self.master()
        raw = comp.get("EXDATE") if comp is not None else None
        if raw is None:
            return []
        out: list[str] = []
        for item in (raw if isinstance(raw, list) else [raw]):
            for d in getattr(item, "dts", [item]):
                value = d.dt if hasattr(d, "dt") else d
                out.append(value.isoformat() if hasattr(value, "isoformat") else str(value))
        return out


class FakeCalendar:
    """A caldav Calendar stub serving the todos it was built with."""

    def __init__(self, todos: list[FakeTodo] | None = None) -> None:
        self.todos = list(todos or [])

    def get_todo_by_uid(self, uid: str) -> FakeTodo:
        for todo in self.todos:
            for comp in Calendar.from_ical(todo.data).walk("VTODO"):
                if str(comp.get("UID", "")) == uid:
                    return todo
        raise caldav_error.NotFoundError(f"no todo with uid={uid}")


class FakeClient:
    """A caldav DAVClient stub: `client.calendar(url=...)` -> FakeCalendar."""

    def __init__(self, calendar: FakeCalendar | None = None) -> None:
        self.cal = calendar if calendar is not None else FakeCalendar()
        self.calendar_calls: list[str] = []

    def calendar(self, url: str | None = None) -> FakeCalendar:
        self.calendar_calls.append(url or "")
        return self.cal

    def principal(self):  # pragma: no cover - tests never need principal lookup
        raise AssertionError("unexpected principal() call in a stub client")

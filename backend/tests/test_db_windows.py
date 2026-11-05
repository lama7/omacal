"""Window queries: which cached rows a [start, end) read has to return.

`GET /api/reminders` (and the alarm checker) ask for a window and then expand
series in Python, so the SQL is a prefilter: it must never hide a row that
belongs inside the window. The panel's bucket rule is what "belongs" means:

  * an all-day row is cached at UTC midnight and is drawn on the DATE part of
    that stored value (frontend `groupRemindersByDay`/`groupEventsByDay`), so it
    belongs to a window when its stored date is one of the window's local dates;
  * a timed row belongs to the window when its instant does.

Regression: the reminder query used to bind `datetime` objects, which sqlite
renders space-separated ("2026-11-04 12:00:00-05:00") while cached values are
`isoformat()` ("...T12:00:00-05:00"). The separator decided same-date
comparisons, which made the reminder window date-granular and lost in-window
rows.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from omacal import db

from tests.support import OmacalTestCase, TempDb


class ReminderWindowTests(OmacalTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db = TempDb(self)
        self.cal = self.db.add_calendar()

    # ---- the reported bug ---------------------------------------------------

    def test_timed_reminder_inside_a_narrow_window_is_returned(self) -> None:
        """A reminder due at 17:30 local must survive a 12:00-23:00 window.

        The stored DUE is UTC ("22:30+00:00"); passing the bounds as datetime
        objects produced "... 12:00:00-05:00", so `due < end` was compared
        separator-first and the row was dropped.
        """
        db.add_reminder(
            self.db.conn, self.cal, "dinner", "Dinner",
            due="2026-11-04T22:30:00+00:00", start="2026-11-04T22:30:00+00:00",
        )
        tz = datetime.now().astimezone().tzinfo
        start = datetime(2026, 11, 4, 12, 0, tzinfo=tz)
        end = datetime(2026, 11, 4, 23, 0, tzinfo=tz)

        rows = db.get_reminders_for_window(self.db.conn, start, end, None)

        self.assertEqual([r["summary"] for r in rows], ["Dinner"])

    def test_reminder_outside_the_window_is_not_returned(self) -> None:
        db.add_reminder(
            self.db.conn, self.cal, "later", "Later",
            due="2026-11-06T22:30:00+00:00", start="2026-11-06T22:30:00+00:00",
        )
        tz = datetime.now().astimezone().tzinfo
        rows = db.get_reminders_for_window(
            self.db.conn,
            datetime(2026, 11, 4, 12, 0, tzinfo=tz),
            datetime(2026, 11, 4, 23, 0, tzinfo=tz),
            None,
        )
        self.assertEqual(rows, [])

    # ---- the all-day convention --------------------------------------------

    def test_all_day_reminder_is_returned_for_its_own_local_date(self) -> None:
        """All-day rows are cached at UTC midnight and drawn on that date.

        A window starting exactly on that date must include it, even though the
        stored instant (00:00Z) is hours before the local-midnight bound.
        """
        db.add_reminder(
            self.db.conn, self.cal, "allday", "Pay bill",
            due="2026-11-05T00:00:00+00:00", start="2026-11-05T00:00:00+00:00",
            all_day=1,
        )
        tz = datetime.now().astimezone().tzinfo
        start = datetime(2026, 11, 5, 0, 0, tzinfo=tz)   # window opens on the date
        rows = db.get_reminders_for_window(self.db.conn, start, start + timedelta(days=42), None)
        self.assertEqual([r["summary"] for r in rows], ["Pay bill"])

        # ...and the day before must not drag it in.
        before = start - timedelta(days=42)
        rows = db.get_reminders_for_window(self.db.conn, before, start, None)
        self.assertEqual(rows, [])

    # ---- recurring masters -------------------------------------------------

    def test_recurring_master_is_returned_outside_its_due_window(self) -> None:
        """A series whose DUE is old is still needed: expansion applies the window."""
        db.add_reminder(
            self.db.conn, self.cal, "monthly", "Pay Bank of America",
            due="2026-01-18T07:00:00-05:00", start="2026-01-18T07:00:00-05:00",
            rrule="FREQ=MONTHLY",
        )
        tz = datetime.now().astimezone().tzinfo
        rows = db.get_reminders_for_window(
            self.db.conn,
            datetime(2026, 11, 1, 0, 0, tzinfo=tz),
            datetime(2026, 12, 1, 0, 0, tzinfo=tz),
            None,
        )
        self.assertEqual([r["summary"] for r in rows], ["Pay Bank of America"])

    def test_disabled_calendar_rows_are_hidden(self) -> None:
        db.add_reminder(self.db.conn, self.cal, "hidden", "Hidden",
                        due="2026-11-04T22:30:00+00:00")
        db.set_calendar_enabled(self.db.conn, self.cal, False)
        tz = datetime.now().astimezone().tzinfo
        rows = db.get_reminders_for_window(
            self.db.conn,
            datetime(2026, 11, 1, 0, 0, tzinfo=tz),
            datetime(2026, 12, 1, 0, 0, tzinfo=tz),
            None,
        )
        self.assertEqual(rows, [])

    def test_calendar_id_filter(self) -> None:
        other = self.db.add_calendar(uid="test-cal-2", display_name="Other")
        db.add_reminder(self.db.conn, self.cal, "mine", "Mine", due="2026-11-04T18:00:00+00:00")
        db.add_reminder(self.db.conn, other, "theirs", "Theirs", due="2026-11-04T19:00:00+00:00")
        tz = datetime.now().astimezone().tzinfo
        start = datetime(2026, 11, 4, 0, 0, tzinfo=tz)
        end = start + timedelta(days=1)
        rows = db.get_reminders_for_window(self.db.conn, start, end, [self.cal])
        self.assertEqual([r["summary"] for r in rows], ["Mine"])


class EventWindowTests(OmacalTestCase):
    """The events query is the reference shape — keep it behaving the same way."""

    def setUp(self) -> None:
        super().setUp()
        self.db = TempDb(self)
        self.cal = self.db.add_calendar()

    def test_timed_event_inside_a_narrow_window_is_returned(self) -> None:
        db.add_event(self.db.conn, self.cal, "ev1", "Standup",
                     start="2026-11-04T17:30:00+00:00", end="2026-11-04T18:00:00+00:00")
        tz = datetime.now().astimezone().tzinfo
        rows = db.get_events_for_window(
            self.db.conn,
            datetime(2026, 11, 4, 12, 0, tzinfo=tz),
            datetime(2026, 11, 4, 23, 0, tzinfo=tz),
            None,
        )
        self.assertEqual([r["summary"] for r in rows], ["Standup"])

    def test_all_day_event_is_returned_for_its_own_local_date(self) -> None:
        db.add_event(self.db.conn, self.cal, "ev2", "Holiday",
                     start="2026-11-05T00:00:00+00:00", end="2026-11-06T00:00:00+00:00",
                     all_day=1)
        tz = datetime.now().astimezone().tzinfo
        start = datetime(2026, 11, 5, 0, 0, tzinfo=tz)
        rows = db.get_events_for_window(self.db.conn, start, start + timedelta(days=42), None)
        self.assertEqual([r["summary"] for r in rows], ["Holiday"])


if __name__ == "__main__":
    unittest.main()

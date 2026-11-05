"""Deleting a reminder: the whole task, or one occurrence of a repeating one.

`sync.delete_reminder` is exercised end to end against the caldav stubs in
`tests.support`: the tests assert what is pushed to the server (an EXDATE added
to the master VTODO, or a resource DELETE) and what lands in the cache, then
re-expand the series from the cache to prove the occurrence really is gone (an
EXDATE that the expansion does not match comes straight back).

Contract, deliberately the same as `delete_event`'s:

    occurrence=None            -> one-off reminder: remove the resource
    series=True                -> repeating reminder: remove the whole series
    occurrence=<ISO due>       -> repeating reminder: exclude that occurrence
    repeating reminder, neither -> raise (a right-click on one occurrence must
                                    never silently wipe the series)
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from omacal import db, sync
from omacal.recur import expand_reminders

from tests.support import FakeCalendar, FakeClient, FakeTodo, OmacalTestCase, TempDb, todo_text

UTC = timezone.utc

# A monthly task due 07:00 New York, the shape a phone writes.
MONTHLY_NY = todo_text(
    uid="monthly-1", summary="Pay Bank of America",
    due="20261118T070000", rrule="FREQ=MONTHLY",
)


def _todo_with_tzid(uid: str, summary: str, rrule: str) -> str:
    """A TZID-valued VTODO, as the server hands it back."""
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//omacal//tests//EN",
        "BEGIN:VTODO",
        f"UID:{uid}",
        f"SUMMARY:{summary}",
        "DUE;TZID=America/New_York:20261118T070000",
        f"RRULE:{rrule}",
        "STATUS:NEEDS-ACTION",
        "END:VTODO", "END:VCALENDAR",
    ]) + "\r\n"


class ReminderDeleteTests(OmacalTestCase):
    def setUp(self) -> None:
        super().setUp()          # closes the keyring + HTTP (see support.OmacalTestCase)
        self.db = TempDb(self)
        self.cal = self.db.add_calendar()

    def use_todo(self, data: str) -> tuple[FakeTodo, FakeClient]:
        """Point sync at a stub server holding one VTODO."""
        todo = FakeTodo(data)
        client = FakeClient(FakeCalendar([todo]))
        patcher = mock.patch.object(sync, "_dav_client", lambda *a, **k: client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return todo, client

    def cache_reminder(self, uid: str, due: str, rrule: str | None, tzid: str | None = None) -> None:
        db.add_reminder(self.db.conn, self.cal, uid, "Pay Bank of America",
                        due=due, start=due, rrule=rrule, tzid=tzid)

    # ---- one occurrence ----------------------------------------------------

    def test_occurrence_delete_adds_an_exdate_and_keeps_the_series(self) -> None:
        todo, _ = self.use_todo(_todo_with_tzid("monthly-1", "Pay Bank of America", "FREQ=MONTHLY"))
        self.cache_reminder("monthly-1", "2026-11-18T07:00:00-05:00", "FREQ=MONTHLY",
                            tzid="America/New_York")

        result = sync.delete_reminder(
            self.db.conn, self.cal, "monthly-1",
            occurrence=datetime(2026, 12, 18, 7, 0, tzinfo=timezone(timedelta(hours=-5))),
        )

        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["scope"], "occurrence")
        self.assertFalse(todo.deleted, "the task itself must survive")
        self.assertEqual(todo.saved, 1, "the master VTODO is re-saved with the EXDATE")
        # The EXDATE mirrors the DUE's own form (TZID + wall clock), because that
        # is what expansion compares against.
        self.assertEqual(todo.exdate_strings(), ["2026-12-18T07:00:00-05:00"])

        rows = db.get_reminders_for_window(
            self.db.conn,
            datetime(2026, 11, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC), None)
        self.assertEqual(len(rows), 1)
        self.assertIn("2026-12-18T07:00:00-05:00", rows[0]["exdates"])

    def test_the_excluded_occurrence_disappears_from_expansion(self) -> None:
        """The cache EXDATE has to match what expansion compares against.

        A value written in the wrong form matches nothing, so the occurrence
        reappears -- this is the assertion that catches that.
        """
        todo, _ = self.use_todo(_todo_with_tzid("monthly-1", "Pay Bank of America", "FREQ=MONTHLY"))
        self.cache_reminder("monthly-1", "2026-11-18T07:00:00-05:00", "FREQ=MONTHLY",
                            tzid="America/New_York")
        ws, we = datetime(2026, 11, 1, tzinfo=UTC), datetime(2027, 2, 1, tzinfo=UTC)

        rows = db.get_reminders_for_window(self.db.conn, ws, we, None)
        before = [o["recurrence_id"] for o in expand_reminders(rows, [], ws, we)]
        self.assertIn("2026-12-18T07:00:00-05:00", before)

        sync.delete_reminder(
            self.db.conn, self.cal, "monthly-1",
            occurrence=datetime(2026, 12, 18, 7, 0, tzinfo=timezone(timedelta(hours=-5))),
        )

        rows = db.get_reminders_for_window(self.db.conn, ws, we, None)
        after = [o["recurrence_id"] for o in expand_reminders(rows, [], ws, we)]
        self.assertNotIn("2026-12-18T07:00:00-05:00", after)
        self.assertIn("2026-11-18T07:00:00-05:00", after, "the other occurrences stay")

    def test_all_day_occurrence_writes_a_date_valued_exdate(self) -> None:
        todo, _ = self.use_todo(todo_text(
            uid="allday-1", summary="Kohl's",
            due="20261130", all_day=True,
            rrule="FREQ=MONTHLY;BYMONTHDAY=28,29,30,31;BYSETPOS=-1",
        ))
        self.cache_reminder("allday-1", "2026-11-30T00:00:00+00:00",
                            "FREQ=MONTHLY;BYMONTHDAY=28,29,30,31;BYSETPOS=-1")

        sync.delete_reminder(self.db.conn, self.cal, "allday-1",
                             occurrence=datetime(2026, 12, 31, tzinfo=UTC))

        self.assertEqual(todo.exdate_strings(), ["2026-12-31"])   # a DATE, not a datetime
        self.assertIn("VALUE=DATE", todo.data)

    def test_a_second_delete_of_the_same_occurrence_is_a_no_op(self) -> None:
        todo, _ = self.use_todo(_todo_with_tzid("monthly-1", "Pay Bank of America", "FREQ=MONTHLY"))
        self.cache_reminder("monthly-1", "2026-11-18T07:00:00-05:00", "FREQ=MONTHLY",
                            tzid="America/New_York")
        when = datetime(2026, 12, 18, 7, 0, tzinfo=timezone(timedelta(hours=-5)))

        sync.delete_reminder(self.db.conn, self.cal, "monthly-1", occurrence=when)
        result = sync.delete_reminder(self.db.conn, self.cal, "monthly-1", occurrence=when)

        self.assertEqual(result["status"], "already-excluded")
        self.assertEqual(todo.saved, 1, "no second save for an EXDATE that is already there")
        self.assertEqual(todo.exdate_strings(), ["2026-12-18T07:00:00-05:00"])

    # ---- the whole series / one-off ---------------------------------------

    def test_series_delete_removes_the_resource_and_the_cache_rows(self) -> None:
        todo, _ = self.use_todo(_todo_with_tzid("monthly-1", "Pay Bank of America", "FREQ=MONTHLY"))
        self.cache_reminder("monthly-1", "2026-11-18T07:00:00-05:00", "FREQ=MONTHLY",
                            tzid="America/New_York")

        result = sync.delete_reminder(self.db.conn, self.cal, "monthly-1", series=True)

        self.assertEqual(result["scope"], "series")
        self.assertTrue(todo.deleted)
        self.assertEqual(db.get_reminders_for_window(
            self.db.conn, datetime(2026, 11, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC), None), [])

    def test_one_off_delete_removes_the_resource(self) -> None:
        todo, _ = self.use_todo(todo_text(uid="one-1", summary="Call Ed Duncan", due="20261115T120000Z"))
        self.cache_reminder("one-1", "2026-11-15T12:00:00+00:00", None)

        result = sync.delete_reminder(self.db.conn, self.cal, "one-1")

        self.assertEqual(result["scope"], "reminder")
        self.assertTrue(todo.deleted)

    # ---- guards ------------------------------------------------------------

    def test_repeating_reminder_without_scope_raises(self) -> None:
        """The regression this whole change is about: no silent series wipe."""
        todo, _ = self.use_todo(_todo_with_tzid("monthly-1", "Pay Bank of America", "FREQ=MONTHLY"))
        self.cache_reminder("monthly-1", "2026-11-18T07:00:00-05:00", "FREQ=MONTHLY",
                            tzid="America/New_York")

        with self.assertRaises(ValueError) as ctx:
            sync.delete_reminder(self.db.conn, self.cal, "monthly-1")

        self.assertIn("repeating reminder", str(ctx.exception))
        self.assertFalse(todo.deleted, "nothing is deleted on a caller error")
        self.assertEqual(todo.saved, 0)

    def test_occurrence_on_a_one_off_reminder_raises(self) -> None:
        todo, _ = self.use_todo(todo_text(uid="one-1", summary="Call Ed Duncan", due="20261115T120000Z"))
        self.cache_reminder("one-1", "2026-11-15T12:00:00+00:00", None)

        with self.assertRaises(ValueError):
            sync.delete_reminder(self.db.conn, self.cal, "one-1",
                                 occurrence=datetime(2026, 11, 15, 12, 0, tzinfo=UTC))
        self.assertFalse(todo.deleted)

    def test_unknown_uid_raises(self) -> None:
        self.use_todo(todo_text(uid="other", summary="Something else"))
        # No principal() on the stub: the search falls back to the one calendar
        # and finds nothing.
        with self.assertRaises(ValueError):
            sync.delete_reminder(self.db.conn, self.cal, "nope")

    def test_google_calendar_is_refused(self) -> None:
        """Reminders on a Google row are refused before any server work.

        oauth_token is stubbed to a fake token: the point is the VTODO guard, not
        the OAuth machinery (and the real one would touch the keyring).
        """
        gcal = self.db.add_calendar(uid="g-1", display_name="GerryGC",
                                    url="google:g-1@group.calendar.google.com",
                                    provider="google", access_role="owner")
        with mock.patch("omacal.oauth.oauth_token", return_value="fake-token"):
            with self.assertRaises(ValueError) as ctx:
                sync.delete_reminder(self.db.conn, gcal, "one-1")
        self.assertIn("Tasks API", str(ctx.exception))


class OccurrenceValueTests(unittest.TestCase):
    """`_occurrence_value` mirrors the anchor property's form — silently wrong otherwise."""

    def test_all_day_writes_a_date(self) -> None:
        target = datetime(2026, 12, 31, 7, 0, tzinfo=timezone(timedelta(hours=-5)))
        from datetime import date
        self.assertEqual(sync._occurrence_value(date(2026, 11, 30), target), date(2026, 12, 31))

    def test_tzid_keeps_the_same_wall_clock(self) -> None:
        ny = timezone(timedelta(hours=-5))
        target = datetime(2026, 12, 18, 12, 0, tzinfo=UTC)      # 07:00 in New York
        self.assertEqual(sync._occurrence_value(datetime(2026, 11, 18, 7, 0, tzinfo=ny), target),
                         datetime(2026, 12, 18, 7, 0, tzinfo=ny))

    def test_naive_anchor_stays_naive(self) -> None:
        target = datetime(2026, 12, 18, 7, 0, tzinfo=UTC)
        self.assertEqual(sync._occurrence_value(datetime(2026, 11, 18, 7, 0), target),
                         datetime(2026, 12, 18, 7, 0))


if __name__ == "__main__":
    unittest.main()

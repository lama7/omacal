"""Reminder series expansion: expand_reminders must place DUE and DTSTART right.

A reminder's recurrence anchors on DUE (a VTODO's DUE is when the task is due;
`recur.expand_reminders` falls back to DTSTART for tasks that carry only that).
So the stepped occurrence IS the DUE, and an occurrence's DTSTART has to keep the
master's offset from DUE -- a task that starts two days before it is due must
still start two days before each occurrence's due date.

These two were swapped: the stepped anchor landed in `start`, and `due` was the
anchor shifted BACK by the start-to-due gap (so an occurrence could even be due
before it started). Latent for the common case (iOS/Nextcloud set DTSTART ==
DUE, gap 0) and wrong for every occurrence of a task whose two dates differ --
including its alarm, which `check_due_alarms` derives from the expanded DUE.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from omacal.recur import expand_reminders

UTC = timezone.utc
WS = datetime(2026, 10, 1, tzinfo=UTC)
WE = datetime(2026, 12, 1, tzinfo=UTC)


def master(**kw):
    row = {
        "calendar_id": 1, "uid": "u1", "summary": "task", "all_day": 0,
        "start": None, "due": None, "rrule": "FREQ=WEEKLY",
        "exdates": None, "tzid": "UTC", "status": "NEEDS-ACTION", "priority": None,
    }
    row.update(kw)
    return row


class ExpandReminderTests(unittest.TestCase):
    def test_start_before_due_keeps_its_offset(self) -> None:
        """Starts Thu 09:00, due Sat 17:00 -> every occurrence keeps that gap."""
        rows = expand_reminders([master(
            start="2026-10-29T09:00:00+00:00", due="2026-10-31T17:00:00+00:00",
        )], [], WS, WE)

        got = [(r["start"], r["due"]) for r in rows]
        self.assertEqual(got, [
            ("2026-10-29T09:00:00+00:00", "2026-10-31T17:00:00+00:00"),
            ("2026-11-05T09:00:00+00:00", "2026-11-07T17:00:00+00:00"),
            ("2026-11-12T09:00:00+00:00", "2026-11-14T17:00:00+00:00"),
            ("2026-11-19T09:00:00+00:00", "2026-11-21T17:00:00+00:00"),
            ("2026-11-26T09:00:00+00:00", "2026-11-28T17:00:00+00:00"),
        ])

    def test_every_occurrence_is_due_after_it_starts(self) -> None:
        """The bug let an occurrence be due BEFORE its own start."""
        rows = expand_reminders([master(
            start="2026-10-29T09:00:00+00:00", due="2026-10-31T17:00:00+00:00",
        )], [], WS, WE)
        for r in rows:
            self.assertLess(r["start"], r["due"], f"{r['start']} should precede {r['due']}")

    def test_first_occurrence_matches_the_master(self) -> None:
        """The series' phase must not move: occurrence 1 == the master's own dates."""
        start, due = "2026-10-29T09:00:00+00:00", "2026-10-31T17:00:00+00:00"
        rows = expand_reminders([master(start=start, due=due)], [], WS, WE)
        self.assertEqual((rows[0]["start"], rows[0]["due"]), (start, due))

    def test_recurrence_id_is_the_anchored_occurrence(self) -> None:
        """recurrence_id feeds EXDATE / RECURRENCE-ID matching, so it stays the DUE."""
        rows = expand_reminders([master(
            start="2026-10-29T09:00:00+00:00", due="2026-10-31T17:00:00+00:00",
        )], [], WS, WE)
        self.assertEqual([r["recurrence_id"] for r in rows], [r["due"] for r in rows])
        self.assertTrue(all(r["is_recurring"] for r in rows))

    def test_start_equals_due_is_unchanged(self) -> None:
        """The common shape (iOS/Nextcloud write both) must behave as before."""
        rows = expand_reminders([master(
            start="2026-10-31T17:00:00+00:00", due="2026-10-31T17:00:00+00:00",
        )], [], WS, WE)
        self.assertEqual([(r["start"], r["due"]) for r in rows], [
            ("2026-10-31T17:00:00+00:00", "2026-10-31T17:00:00+00:00"),
            ("2026-11-07T17:00:00+00:00", "2026-11-07T17:00:00+00:00"),
            ("2026-11-14T17:00:00+00:00", "2026-11-14T17:00:00+00:00"),
            ("2026-11-21T17:00:00+00:00", "2026-11-21T17:00:00+00:00"),
            ("2026-11-28T17:00:00+00:00", "2026-11-28T17:00:00+00:00"),
        ])

    def test_due_only_task_has_no_start(self) -> None:
        """A VTODO with only DUE (what the panel creates) leaves start NULL."""
        rows = expand_reminders([master(due="2026-10-31T17:00:00+00:00")], [], WS, WE)
        self.assertTrue(rows)
        for r in rows:
            self.assertIsNone(r["start"])
            self.assertEqual(r["due"], r["recurrence_id"])

    def test_start_only_task_falls_back_to_start(self) -> None:
        """No DUE: the series anchors on DTSTART and DUE mirrors the occurrence."""
        rows = expand_reminders([master(start="2026-10-30T08:00:00+00:00", due=None)], [], WS, WE)
        self.assertEqual([(r["start"], r["due"]) for r in rows][:2], [
            ("2026-10-30T08:00:00+00:00", "2026-10-30T08:00:00+00:00"),
            ("2026-11-06T08:00:00+00:00", "2026-11-06T08:00:00+00:00"),
        ])

    def test_all_day_series_keeps_whole_day_offsets(self) -> None:
        """All-day: DTSTART a day before DUE stays a day before, per occurrence."""
        rows = expand_reminders([master(
            all_day=1, start="2026-10-29T00:00:00+00:00", due="2026-10-30T00:00:00+00:00",
        )], [], WS, WE)
        self.assertEqual([(r["start"], r["due"]) for r in rows][:2], [
            ("2026-10-29T00:00:00+00:00", "2026-10-30T00:00:00+00:00"),
            ("2026-11-05T00:00:00+00:00", "2026-11-06T00:00:00+00:00"),
        ])

    def test_exdate_suppresses_the_occurrence(self) -> None:
        """The form `sync.delete_reminder` writes is what suppression matches."""
        rows = expand_reminders([master(
            start="2026-10-29T09:00:00+00:00", due="2026-10-31T17:00:00+00:00",
            exdates='["2026-11-07T17:00:00+00:00"]',
        )], [], WS, WE)
        self.assertNotIn("2026-11-07T17:00:00+00:00", [r["due"] for r in rows])
        self.assertIn("2026-11-14T17:00:00+00:00", [r["due"] for r in rows])

    def test_override_replaces_that_occurrence(self) -> None:
        override = {
            "calendar_id": 1, "uid": "u1", "recurrence_id": "2026-11-07T17:00:00+00:00",
            "summary": "moved", "start": "2026-11-09T09:00:00+00:00",
            "due": "2026-11-11T17:00:00+00:00", "all_day": 0, "status": "NEEDS-ACTION",
            "priority": None, "tzid": "UTC",
        }
        rows = expand_reminders([master(
            start="2026-10-29T09:00:00+00:00", due="2026-10-31T17:00:00+00:00",
        )], [override], WS, WE)
        moved = [r for r in rows if r["recurrence_id"] == "2026-11-07T17:00:00+00:00"]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["summary"], "moved")
        self.assertEqual(moved[0]["due"], "2026-11-11T17:00:00+00:00")
        self.assertEqual(moved[0]["start"], "2026-11-09T09:00:00+00:00")

    def test_cancelled_override_removes_the_occurrence(self) -> None:
        override = {
            "calendar_id": 1, "uid": "u1", "recurrence_id": "2026-11-07T17:00:00+00:00",
            "summary": "gone", "start": None, "due": None, "all_day": 0,
            "status": "CANCELLED", "priority": None, "tzid": "UTC",
        }
        rows = expand_reminders([master(
            start="2026-10-29T09:00:00+00:00", due="2026-10-31T17:00:00+00:00",
        )], [override], WS, WE)
        self.assertNotIn("2026-11-07T17:00:00+00:00", [r["due"] for r in rows])


if __name__ == "__main__":
    unittest.main()

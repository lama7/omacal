"""The guard rails themselves: a test must never reach the live machine.

`support.OmacalTestCase` closes the two live surfaces the backend uses — the
system keyring (the developer's real credentials) and HTTP. These tests pin that
down, because breaking it is silent: an un-patched write path once called
`oauth_token`, which POSTed a refresh to Google's token endpoint and, on
`invalid_grant`, deleted the stored token set out of the real keyring.
"""

from __future__ import annotations

import unittest
import urllib.request

from omacal import keyring_store, sync

from tests.support import OmacalTestCase, TempDb, todo_text


class LiveStateGuardTests(OmacalTestCase):
    def test_keyring_reads_are_closed(self) -> None:
        with self.assertRaises(AssertionError):
            keyring_store.get_password("https://example.test/principals/u/", "user")

    def test_keyring_writes_are_closed(self) -> None:
        with self.assertRaises(AssertionError):
            keyring_store.store_password("https://example.test/principals/u/", "user", "pw")

    def test_http_is_closed(self) -> None:
        with self.assertRaises(AssertionError):
            urllib.request.urlopen("https://example.test/token")

    def test_oauth_token_is_closed(self) -> None:
        """A write path on a Google row must fail loudly, not talk to Google."""
        db = TempDb(self)
        gcal = db.add_calendar(uid="g-1", display_name="GerryGC",
                               url="google:g-1@group.calendar.google.com",
                               provider="google", access_role="owner")
        with self.assertRaises(AssertionError) as ctx:
            sync.delete_reminder(db.conn, gcal, "one-1")
        self.assertIn("oauth_token", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

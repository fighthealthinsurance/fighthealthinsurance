"""Live-connection proof that the shared-file sqlite options take effect.

The sync-actor suite runs under TestActor against a real sqlite FILE (the
same file Ray actor workers open), so the pragmas that matter for
multi-process safety can be asserted on the actual Django connection here --
unlike the in-memory Test/TestSync suites, where journal_mode is always
"memory" no matter what the options say.
"""

from django.db import connection
from django.test import TestCase


class SharedFileSqlitePragmasTest(TestCase):
    def _pragma(self, name):
        with connection.cursor() as cursor:
            cursor.execute(f"PRAGMA {name}")
            return cursor.fetchone()[0]

    def test_journal_mode_is_wal(self):
        self.assertEqual(self._pragma("journal_mode"), "wal")

    def test_busy_timeout_matches_options(self):
        # OPTIONS["timeout"] (seconds) becomes the sqlite busy handler (ms).
        expected_ms = connection.settings_dict["OPTIONS"]["timeout"] * 1000
        self.assertEqual(self._pragma("busy_timeout"), expected_ms)

    def test_synchronous_is_normal(self):
        # 1 == NORMAL (0 OFF, 2 FULL, 3 EXTRA)
        self.assertEqual(self._pragma("synchronous"), 1)

    def test_write_transactions_begin_immediate(self):
        self.assertEqual(connection.transaction_mode, "IMMEDIATE")

"""The shared-file sqlite configs must carry the multi-process options.

Dev and TestActor point several processes (server/test process + Ray actor
workers, each booting its own Django) at one sqlite file; without WAL +
IMMEDIATE transactions + a busy timeout that shape flakes with "database is
locked". These are plain class-attribute checks -- the live-connection proof
(the pragmas actually taking effect on the shared file) lives in
tests/sync-actor/test_sqlite_shared_file.py where the file-backed database
is the active one.
"""

from django.test import SimpleTestCase

from fighthealthinsurance.settings import Dev, TestActor


class SharedFileSqliteOptionsTest(SimpleTestCase):
    def _options(self, config_class):
        return config_class.DATABASES["default"]["OPTIONS"]

    def test_dev_uses_immediate_transactions(self):
        self.assertEqual(self._options(Dev)["transaction_mode"], "IMMEDIATE")

    def test_dev_enables_wal_and_normal_sync(self):
        init_command = self._options(Dev)["init_command"]
        self.assertIn("journal_mode=WAL", init_command)
        self.assertIn("synchronous=NORMAL", init_command)

    def test_dev_sets_a_busy_timeout(self):
        self.assertGreaterEqual(self._options(Dev)["timeout"], 5)

    def test_testactor_gets_the_same_shared_file_options(self):
        options = self._options(TestActor)
        self.assertEqual(options["transaction_mode"], "IMMEDIATE")
        self.assertIn("journal_mode=WAL", options["init_command"])
        self.assertGreaterEqual(options["timeout"], 5)

    def test_options_dicts_are_not_shared_between_configs(self):
        # Django mutates settings_dict in place; a shared literal would
        # couple the configurations (see _sqlite_shared_file_options).
        self.assertIsNot(self._options(Dev), self._options(TestActor))

"""The shared SQLite database has to make a second writer wait.

The actor configuration deliberately points this process and the Ray worker
processes at one file-based SQLite database, and SQLite allows a single
writer. So a write that arrives while another is in progress has to wait for
it rather than raise, and the wait is set by the ``timeout`` sqlite3 gets at
connect time.

That setting used to be written as a top-level ``TIMEOUT`` key, which
Django's SQLite backend does not read: ``get_connection_params`` passes only
``**settings_dict["OPTIONS"]`` to ``sqlite3.connect``. So the configured wait
was silently inert and the real one was sqlite3's five-second default. This
pins it in the key that is actually read, because the failure mode of getting
it wrong is invisible: everything passes until two writers overlap, and then
CI fails somewhere unrelated with "database is locked".
"""

from django.conf import settings
from django.db import connection
from django.test import SimpleTestCase


class TheConfiguredTimeoutReachesSqliteTest(SimpleTestCase):
    def test_it_is_in_options_where_django_reads_it(self):
        options = settings.DATABASES["default"].get("OPTIONS", {})

        self.assertIn(
            "timeout",
            options,
            "a top-level TIMEOUT is ignored by the SQLite backend; it has to "
            "be in OPTIONS",
        )
        self.assertGreaterEqual(options["timeout"], 10)

    def test_the_live_connection_actually_got_it(self):
        """Read it back off the connection rather than the settings dict."""
        params = connection.get_connection_params()

        self.assertIn("timeout", params)
        self.assertEqual(
            params["timeout"], settings.DATABASES["default"]["OPTIONS"]["timeout"]
        )

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
from django.test import TestCase

# Seconds a writer waits for the lock before "database is locked". The
# default is five, and a Ray worker mid-write plus a test class starting its
# own writes has been seen to need more than that.
BUSY_WAIT_SECONDS = 30


class TheConfiguredTimeoutReachesSqliteTest(TestCase):
    def test_it_is_in_options_where_django_reads_it(self):
        options = settings.DATABASES["default"].get("OPTIONS", {})

        self.assertIn(
            "timeout",
            options,
            "a top-level TIMEOUT is ignored by the SQLite backend; it has to "
            "be in OPTIONS",
        )
        self.assertEqual(options["timeout"], BUSY_WAIT_SECONDS)

    def test_the_open_connection_actually_got_it(self):
        """Ask SQLite itself, on a connection Django opened.

        get_connection_params() only builds the arguments; nothing is proved
        until sqlite3.connect has run with them. busy_timeout is the value
        that call installed, in milliseconds.
        """
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA busy_timeout")
            (milliseconds,) = cursor.fetchone()

        self.assertEqual(milliseconds, BUSY_WAIT_SECONDS * 1000)

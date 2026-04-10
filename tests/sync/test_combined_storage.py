"""Tests for fighthealthinsurance.combined_storage.CombinedStorage.

CombinedStorage wraps multiple Django Storage backends, trying each in turn on
open / delete / url / exists, and attempting to write to all of them on save.
This test suite exercises the failover semantics with MagicMock-backed
`Storage` instances — no real filesystem or network I/O is needed.
"""

from unittest import TestCase
from unittest.mock import MagicMock, call

from django.core.files.base import ContentFile, File
from django.core.files.storage import Storage

from fighthealthinsurance.combined_storage import CombinedStorage


def _make_storage() -> MagicMock:
    """Build a `Storage` mock with the methods CombinedStorage calls."""
    return MagicMock(spec=Storage)


def _make_file() -> File:
    """Return a concrete `File` instance acceptable to CombinedStorage.open."""
    return File(ContentFile(b"hello"), name="hello.txt")


class TestCombinedStorageInit(TestCase):
    def test_init_filters_out_none_backends(self):
        """None entries passed to __init__ are dropped from self.backends."""
        backend = _make_storage()
        storage = CombinedStorage(None, backend, None)
        self.assertEqual(storage.backends, [backend])

    def test_init_preserves_order(self):
        """Backend ordering from __init__ is preserved (failover order)."""
        a = _make_storage()
        b = _make_storage()
        c = _make_storage()
        storage = CombinedStorage(a, b, c)
        self.assertEqual(storage.backends, [a, b, c])


class TestCombinedStorageOpen(TestCase):
    def test_open_returns_from_first_successful_backend(self):
        """open() returns the File from the first backend that succeeds."""
        file_obj = _make_file()
        first = _make_storage()
        first.open.return_value = file_obj
        second = _make_storage()

        storage = CombinedStorage(first, second)
        result = storage.open("foo.txt", "rb")

        self.assertIs(result, file_obj)
        first.open.assert_called_once_with("foo.txt", mode="rb")
        second.open.assert_not_called()

    def test_open_falls_through_on_exception(self):
        """If the first backend raises, the next backend is tried."""
        file_obj = _make_file()
        first = _make_storage()
        first.open.side_effect = IOError("boom")
        second = _make_storage()
        second.open.return_value = file_obj

        storage = CombinedStorage(first, second)
        result = storage.open("foo.txt", "rb")

        self.assertIs(result, file_obj)
        first.open.assert_called_once_with("foo.txt", mode="rb")
        second.open.assert_called_once_with("foo.txt", mode="rb")

    def test_open_raises_last_error_when_all_fail(self):
        """If every backend fails, the last exception is re-raised."""
        first = _make_storage()
        first.open.side_effect = IOError("first")
        second = _make_storage()
        last_error = RuntimeError("last")
        second.open.side_effect = last_error

        storage = CombinedStorage(first, second)
        with self.assertRaises(RuntimeError) as ctx:
            storage.open("foo.txt", "rb")
        self.assertIs(ctx.exception, last_error)

    def test_open_rejects_non_file_return(self):
        """A backend that returns a non-File object is treated as failing."""
        bad = _make_storage()
        bad.open.return_value = "not-a-file"
        good = _make_storage()
        good.open.return_value = _make_file()

        storage = CombinedStorage(bad, good)
        # The assertion inside open() raises AssertionError; that error is
        # caught and the next backend is tried.
        result = storage.open("foo.txt")
        self.assertIsInstance(result, File)
        good.open.assert_called_once()


class TestCombinedStorageSave(TestCase):
    def test_save_returns_first_successful_name_and_replicates(self):
        """save() returns the name from the first successful backend, but
        attempts to save to every other backend as well."""
        first = _make_storage()
        first.save.return_value = "stored/foo.txt"
        second = _make_storage()
        second.save.return_value = "different/name-should-be-ignored.txt"

        content = ContentFile(b"payload")
        storage = CombinedStorage(first, second)
        result = storage.save("foo.txt", content)

        self.assertEqual(result, "stored/foo.txt")
        first.save.assert_called_once_with(
            name="foo.txt", content=content, max_length=None
        )
        second.save.assert_called_once_with(
            name="foo.txt", content=content, max_length=None
        )

    def test_save_continues_through_failures(self):
        """If the first backend raises, save() still succeeds via the second."""
        first = _make_storage()
        first.save.side_effect = IOError("first down")
        second = _make_storage()
        second.save.return_value = "fallback/foo.txt"

        storage = CombinedStorage(first, second)
        result = storage.save("foo.txt", ContentFile(b"payload"))

        self.assertEqual(result, "fallback/foo.txt")
        first.save.assert_called_once()
        second.save.assert_called_once()

    def test_save_raises_when_all_backends_fail(self):
        """If every backend fails, save() raises an Exception that surfaces
        the underlying errors."""
        first = _make_storage()
        first.save.side_effect = IOError("first down")
        second = _make_storage()
        second.save.side_effect = IOError("second down")

        storage = CombinedStorage(first, second)
        with self.assertRaises(Exception) as ctx:
            storage.save("foo.txt", ContentFile(b"payload"))
        # The error message mentions the collected errors.
        self.assertIn("second down", str(ctx.exception))


class TestCombinedStorageDelete(TestCase):
    def test_delete_returns_on_first_success(self):
        """delete() returns as soon as a backend succeeds."""
        first = _make_storage()
        first.delete.return_value = None
        second = _make_storage()

        storage = CombinedStorage(first, second)
        storage.delete("foo.txt")

        first.delete.assert_called_once_with("foo.txt")
        second.delete.assert_not_called()

    def test_delete_raises_last_error_when_all_fail(self):
        """If every backend fails, the last exception is re-raised."""
        first = _make_storage()
        first.delete.side_effect = IOError("first")
        second = _make_storage()
        last_error = RuntimeError("last")
        second.delete.side_effect = last_error

        storage = CombinedStorage(first, second)
        with self.assertRaises(RuntimeError) as ctx:
            storage.delete("foo.txt")
        self.assertIs(ctx.exception, last_error)


class TestCombinedStorageUrl(TestCase):
    def test_url_returns_from_first_successful_backend(self):
        first = _make_storage()
        first.url.return_value = "https://first.example/foo"
        second = _make_storage()

        storage = CombinedStorage(first, second)
        self.assertEqual(storage.url("foo"), "https://first.example/foo")
        second.url.assert_not_called()

    def test_url_falls_through_on_exception(self):
        first = _make_storage()
        first.url.side_effect = IOError("first")
        second = _make_storage()
        second.url.return_value = "https://second.example/foo"

        storage = CombinedStorage(first, second)
        self.assertEqual(storage.url("foo"), "https://second.example/foo")

    def test_url_raises_last_error_when_all_backends_fail(self):
        """Regression: url() previously forgot to record the backend error
        and raised a misleading "No backends?" exception when every backend
        failed. It should now surface the actual last error."""
        first = _make_storage()
        first.url.side_effect = IOError("first")
        second = _make_storage()
        last_error = RuntimeError("last")
        second.url.side_effect = last_error

        storage = CombinedStorage(first, second)
        with self.assertRaises(RuntimeError) as ctx:
            storage.url("foo")
        self.assertIs(ctx.exception, last_error)

    def test_url_raises_when_no_backends_configured(self):
        """url() raises when CombinedStorage was constructed with zero
        backends (the None-filtering path can land here)."""
        storage = CombinedStorage(None, None)
        with self.assertRaises(Exception):
            storage.url("foo")


class TestCombinedStorageExists(TestCase):
    def test_exists_returns_true_on_first_match(self):
        """exists() short-circuits the first time a backend reports True."""
        first = _make_storage()
        first.exists.return_value = False
        second = _make_storage()
        second.exists.return_value = True
        third = _make_storage()

        storage = CombinedStorage(first, second, third)
        self.assertTrue(storage.exists("foo.txt"))
        first.exists.assert_called_once_with(name="foo.txt")
        second.exists.assert_called_once_with(name="foo.txt")
        third.exists.assert_not_called()

    def test_exists_returns_false_when_no_backend_matches(self):
        """exists() returns False when every backend responds False and no
        backend raises."""
        first = _make_storage()
        first.exists.return_value = False
        second = _make_storage()
        second.exists.return_value = False

        storage = CombinedStorage(first, second)
        self.assertFalse(storage.exists("foo.txt"))
        self.assertEqual(
            first.exists.call_args_list + second.exists.call_args_list,
            [call(name="foo.txt"), call(name="foo.txt")],
        )

"""Tests for fighthealthinsurance.combined_storage.CombinedStorage.

CombinedStorage wraps multiple Django Storage backends, trying each in turn on
open / url / exists, and acting on all of them on save and delete.
This test suite exercises the failover semantics with MagicMock-backed
`Storage` instances, plus temporary-directory backends for delete.
"""

import tempfile
from unittest import TestCase
from unittest.mock import MagicMock, call, patch

from django.core.files.base import ContentFile, File
from django.core.files.storage import FileSystemStorage, Storage
from stopit import ThreadingTimeout

from fighthealthinsurance.combined_storage import CombinedStorage


def _make_storage() -> MagicMock:
    """Build a `Storage` mock with the methods CombinedStorage calls."""
    return MagicMock(spec=Storage)


def _make_file() -> File:
    """Return a concrete `File` instance acceptable to CombinedStorage.open."""
    return File(ContentFile(b"hello"), name="hello.txt")


def _timed_out() -> MagicMock:
    """A stand-in for a stopit timeout context that ran out of time."""
    timed_out = MagicMock()
    timed_out.__enter__.return_value = timed_out
    timed_out.__exit__.return_value = False
    timed_out.__bool__.return_value = False
    return timed_out


class TestCombinedStorageInit(TestCase):
    def test_init_filters_out_none_backends(self):
        backend = _make_storage()
        storage = CombinedStorage(None, backend, None)
        self.assertEqual(storage.backends, [backend])

    def test_init_preserves_order(self):
        # Order matters: it defines failover order for every other method.
        a = _make_storage()
        b = _make_storage()
        c = _make_storage()
        storage = CombinedStorage(a, b, c)
        self.assertEqual(storage.backends, [a, b, c])


class TestCombinedStorageOpen(TestCase):
    def test_open_returns_from_first_successful_backend(self):
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
        # open() has an internal assert that the backend's return value is a
        # File instance; a string return should trigger fallback, not leak
        # out a bogus object.
        bad = _make_storage()
        bad.open.return_value = "not-a-file"
        good = _make_storage()
        good.open.return_value = _make_file()

        storage = CombinedStorage(bad, good)
        result = storage.open("foo.txt")
        self.assertIsInstance(result, File)
        good.open.assert_called_once()


class TestCombinedStorageSave(TestCase):
    def test_save_returns_first_successful_name_and_replicates(self):
        """save() returns the name from the first successful backend and
        saves every other backend's copy under that same name."""
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
            name="stored/foo.txt", content=content, max_length=None
        )

    def test_save_uses_the_next_backends_name_when_the_first_fails(self):
        first = _make_storage()
        first.save.side_effect = IOError("first down")
        second = _make_storage()
        second.save.return_value = "stored/foo.txt"
        third = _make_storage()
        third.save.return_value = "stored/foo.txt"

        storage = CombinedStorage(first, second, third)
        storage.save("foo.txt", ContentFile(b"payload"))

        self.assertEqual(third.save.call_args.kwargs["name"], "stored/foo.txt")

    def test_save_removes_a_copy_a_later_backend_stored_under_another_name(self):
        first = _make_storage()
        first.save.return_value = "SBC.pdf"
        second = _make_storage()
        second.save.return_value = "SBC_x1y2z3Q.pdf"

        storage = CombinedStorage(first, second)
        result = storage.save("SBC.pdf", ContentFile(b"plan"))

        self.assertEqual(result, "SBC.pdf")
        second.delete.assert_called_once_with("SBC_x1y2z3Q.pdf")
        first.delete.assert_not_called()

    def test_save_warning_names_both_files_when_a_backend_picks_another_name(self):
        first = _make_storage()
        first.save.return_value = "SBC.pdf"
        second = _make_storage()
        second.save.return_value = "SBC_x1y2z3Q.pdf"

        storage = CombinedStorage(first, second)
        with patch("fighthealthinsurance.combined_storage.logger") as log:
            storage.save("SBC.pdf", ContentFile(b"plan"))

        message = log.warning.call_args.args[0]
        self.assertIn("SBC.pdf", message)
        self.assertIn("SBC_x1y2z3Q.pdf", message)

    def test_save_carries_on_when_a_renamed_copy_cannot_be_removed(self):
        first = _make_storage()
        first.save.return_value = "SBC.pdf"
        second = _make_storage()
        second.save.return_value = "SBC_x1y2z3Q.pdf"
        second.delete.side_effect = PermissionError("read-only mount")
        third = _make_storage()
        third.save.return_value = "SBC.pdf"

        storage = CombinedStorage(first, second, third)
        with patch("fighthealthinsurance.combined_storage.logger") as log:
            result = storage.save("SBC.pdf", ContentFile(b"plan"))

        self.assertEqual(result, "SBC.pdf")
        third.save.assert_called_once()
        self.assertIn("read-only mount", log.warning.call_args.args[0])

    def test_save_continues_through_failures(self):
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
    def test_delete_calls_every_backend(self):
        first = _make_storage()
        first.delete.return_value = None
        second = _make_storage()

        storage = CombinedStorage(first, second)
        storage.delete("foo.txt")

        first.delete.assert_called_once_with("foo.txt")
        second.delete.assert_called_once_with("foo.txt")

    def test_delete_continues_past_a_backend_without_the_file(self):
        first = _make_storage()
        first.delete.side_effect = FileNotFoundError("foo.txt")
        second = _make_storage()

        storage = CombinedStorage(first, second)
        storage.delete("foo.txt")

        second.delete.assert_called_once_with("foo.txt")

    def test_delete_does_not_raise_when_another_backend_deletes_the_file(self):
        first = _make_storage()
        first.delete.side_effect = PermissionError("read-only mount")
        second = _make_storage()

        storage = CombinedStorage(first, second)
        storage.delete("foo.txt")

        second.delete.assert_called_once_with("foo.txt")

    def test_delete_logs_a_backend_failure_with_the_file_name_and_error(self):
        first = _make_storage()
        first.delete.side_effect = PermissionError("read-only mount")
        second = _make_storage()

        storage = CombinedStorage(first, second)
        with patch("fighthealthinsurance.combined_storage.logger") as log:
            storage.delete("foo.txt")

        message = log.error.call_args.args[0]
        self.assertIn("foo.txt", message)
        self.assertIn("read-only mount", message)

    def test_delete_raises_when_one_backend_has_no_copy_and_the_other_fails(self):
        first = _make_storage()
        first.delete.side_effect = FileNotFoundError("foo.txt")
        second = _make_storage()
        failure = PermissionError("read-only mount")
        second.delete.side_effect = failure

        storage = CombinedStorage(first, second)
        with self.assertRaises(PermissionError) as ctx:
            storage.delete("foo.txt")
        self.assertIs(ctx.exception, failure)

    def test_delete_does_not_raise_when_one_backend_times_out(self):
        first = _make_storage()
        second = _make_storage()

        storage = CombinedStorage(first, second)
        with patch(
            "fighthealthinsurance.combined_storage.Timeout",
            side_effect=[_timed_out(), ThreadingTimeout(2.0)],
        ):
            storage.delete("foo.txt")

        second.delete.assert_called_once_with("foo.txt")

    def test_delete_raises_naming_the_file_when_every_backend_times_out(self):
        first = _make_storage()
        second = _make_storage()

        storage = CombinedStorage(first, second)
        with patch(
            "fighthealthinsurance.combined_storage.Timeout", return_value=_timed_out()
        ):
            with self.assertRaises(TimeoutError) as ctx:
                storage.delete("foo.txt")
        self.assertIn("foo.txt", str(ctx.exception))
        second.delete.assert_called_once_with("foo.txt")

    def test_delete_raises_last_error_when_all_fail(self):
        first = _make_storage()
        first.delete.side_effect = IOError("first")
        second = _make_storage()
        last_error = RuntimeError("last")
        second.delete.side_effect = last_error

        storage = CombinedStorage(first, second)
        with self.assertRaises(RuntimeError) as ctx:
            storage.delete("foo.txt")
        self.assertIs(ctx.exception, last_error)


class TestCombinedStorageDeleteOnDisk(TestCase):
    """save / open / delete against real directories, like the app's backends."""

    def setUp(self):
        self.dirs = [tempfile.TemporaryDirectory() for _ in range(3)]
        for d in self.dirs:
            self.addCleanup(d.cleanup)
        self.backends = [FileSystemStorage(location=d.name) for d in self.dirs]
        self.storage = CombinedStorage(*self.backends)

    def test_delete_removes_the_file_from_every_backend(self):
        name = self.storage.save("plan.pdf", ContentFile(b"plan"))
        self.assertTrue(all(b.exists(name) for b in self.backends))

        self.storage.delete(name)

        self.assertEqual([b.exists(name) for b in self.backends], [False] * 3)

    def test_delete_removes_the_other_copies_when_one_is_missing(self):
        name = self.storage.save("plan.pdf", ContentFile(b"plan"))
        self.backends[0].delete(name)

        self.storage.delete(name)

        self.assertEqual([b.exists(name) for b in self.backends], [False] * 3)

    def two_uploads_named_sbc(self):
        first = self.storage.save("SBC.pdf", ContentFile(b"first plan"))
        second = self.storage.save("SBC.pdf", ContentFile(b"second plan"))
        return first, second

    def test_a_repeated_upload_name_is_stored_under_one_name_everywhere(self):
        first, second = self.two_uploads_named_sbc()

        self.assertNotEqual(first, second)
        self.assertEqual([b.exists(second) for b in self.backends], [True] * 3)

    def test_delete_after_a_repeated_upload_name_removes_every_copy(self):
        first, second = self.two_uploads_named_sbc()

        self.storage.delete(second)

        self.assertEqual([b.listdir("")[1] for b in self.backends], [[first]] * 3)

    def test_open_falls_back_after_a_repeated_upload_name(self):
        _first, second = self.two_uploads_named_sbc()
        self.backends[0].delete(second)

        with self.storage.open(second) as f:
            self.assertEqual(f.read(), b"second plan")


class TestCombinedStorageSaveWhereALaterBackendHoldsTheName(TestCase):
    """Backend 1 already holds SBC.pdf, so it stores a new upload under another name."""

    def setUp(self):
        dirs = [tempfile.TemporaryDirectory() for _ in range(3)]
        for d in dirs:
            self.addCleanup(d.cleanup)
        self.backends = [FileSystemStorage(location=d.name) for d in dirs]
        self.backends[1].save("SBC.pdf", ContentFile(b"older plan"))
        self.storage = CombinedStorage(*self.backends)
        self.name = self.storage.save("SBC.pdf", ContentFile(b"new plan"))

    def test_the_first_backends_name_is_returned(self):
        self.assertEqual(self.name, "SBC.pdf")

    def test_that_backend_keeps_no_extra_copy(self):
        self.assertEqual(self.backends[1].listdir("")[1], ["SBC.pdf"])

    def test_that_backends_existing_file_is_untouched(self):
        with self.backends[1].open("SBC.pdf") as f:
            self.assertEqual(f.read(), b"older plan")

    def test_the_other_backends_hold_the_new_file(self):
        contents = []
        for backend in (self.backends[0], self.backends[2]):
            with backend.open("SBC.pdf") as f:
                contents.append(f.read())
        self.assertEqual(contents, [b"new plan", b"new plan"])


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

"""Tests for the staff Storage Health page (StorageHealthView) and the checks
in storage_health.py it is built from.

The document storage is pointed at temporary directories, as in
test_deleting_a_row_removes_its_stored_files.py. Whether a temporary
directory sits on the app's own disk depends on the host, so every test
that cares patches storage_health.placement to say.
"""

import os
import shutil
import tempfile
from collections import namedtuple
from unittest import mock

import minio
import minio.error
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance import storage_health
from fighthealthinsurance.log_tags import file_tag
from fighthealthinsurance.models import Denial, PlanDocuments
from minio_storage.storage import MinioStorage

User = get_user_model()

UPLOAD_NAME = "Rebecca Crumpler SBC.pdf"
_Usage = namedtuple("_Usage", "total used free")


class _StorageTestCase(TestCase):
    """Three temporary directories stand in for the document storage's backends."""

    def setUp(self):
        super().setUp()
        dirs = [tempfile.TemporaryDirectory() for _ in range(3)]
        for d in dirs:
            self.addCleanup(d.cleanup)
        self.dirs = [d.name for d in dirs]
        self.backends = [FileSystemStorage(location=d) for d in self.dirs]
        self.storage = storage_health.stored_file_storage()
        patcher = mock.patch.object(self.storage, "backends", self.backends)
        patcher.start()
        self.addCleanup(patcher.stop)
        # By default the last backend is off the app's disk.
        self.set_off_disk(self.backends[2])

    def set_off_disk(self, *off_disk):
        def placement(backend):
            if backend in off_disk:
                return storage_health.OWN_MOUNT
            return storage_health.APP_DISK

        patcher = mock.patch.object(storage_health, "placement", placement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def plan_document(self):
        denial, _ = Denial.objects.get_or_create(
            denial_id=1,
            defaults=dict(
                semi_sekret="sekret",
                hashed_email=Denial.get_hashed_email("rebecca@example.com"),
            ),
        )
        row = PlanDocuments(denial=denial)
        row.plan_document_enc.save(UPLOAD_NAME, ContentFile(b"%PDF-1.4"), save=False)
        row.save()
        return row

    def rows(self):
        return storage_health.backend_rows(self.storage)


class StorageHealthAccessTest(TestCase):
    def test_anonymous_get_is_redirected_to_login(self):
        response = self.client.get(reverse("storage_health"))
        self.assertEqual(response.status_code, 302)

    def test_non_staff_get_is_redirected(self):
        User.objects.create_user(username="plain", password="pw123", is_staff=False)
        self.client.login(username="plain", password="pw123")
        response = self.client.get(reverse("storage_health"))
        self.assertEqual(response.status_code, 302)

    def test_non_staff_post_does_not_run_the_write_test(self):
        User.objects.create_user(username="plain", password="pw123", is_staff=False)
        self.client.login(username="plain", password="pw123")
        with mock.patch.object(storage_health, "write_test") as write_test:
            response = self.client.post(reverse("storage_health"))
        self.assertEqual(response.status_code, 302)
        write_test.assert_not_called()

    def test_staff_dashboard_links_to_storage_health(self):
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")
        response = self.client.get(reverse("staff_dashboard"))
        self.assertContains(response, reverse("storage_health"))


class StorageHealthPageTest(_StorageTestCase):
    def setUp(self):
        super().setUp()
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")

    def test_staff_get_renders_every_backend(self):
        response = self.client.get(reverse("storage_health"))
        self.assertEqual(response.status_code, 200)
        for location in self.dirs:
            self.assertContains(response, location)

    def test_page_never_shows_a_stored_file_name(self):
        row = self.plan_document()
        os.remove(os.path.join(self.dirs[2], row.plan_document_enc.name))
        response = self.client.get(reverse("storage_health"))
        self.assertNotContains(response, "Crumpler")

    def test_page_names_a_flagged_document_by_its_file_tag(self):
        row = self.plan_document()
        os.remove(os.path.join(self.dirs[2], row.plan_document_enc.name))
        response = self.client.get(reverse("storage_health"))
        self.assertContains(response, file_tag(row.plan_document_enc.name))

    def test_get_does_not_write_a_probe(self):
        self.client.get(reverse("storage_health"))
        self.assertEqual([os.listdir(d) for d in self.dirs], [[], [], []])

    def test_staff_post_runs_the_write_test(self):
        response = self.client.post(reverse("storage_health"))
        self.assertContains(response, "PASSED", count=3)

    def test_sample_zero_skips_the_recent_documents_check(self):
        with mock.patch.object(storage_health, "sample_recent_files") as sample:
            self.client.get(reverse("storage_health"), {"sample": "0"})
        sample.assert_not_called()

    def test_failed_write_test_degrades_the_overall_badge(self):
        with mock.patch.object(
            self.backends[0], "save", side_effect=PermissionError("read-only")
        ):
            response = self.client.post(reverse("storage_health"))
        self.assertEqual(response.context["level"], storage_health.WARN)

    def test_failed_write_test_is_listed_as_a_problem(self):
        with mock.patch.object(
            self.backends[0], "save", side_effect=PermissionError("read-only")
        ):
            response = self.client.post(reverse("storage_health"))
        self.assertContains(response, "backend 1 failed the write test")


class SampleSizeTest(TestCase):
    def test_missing_value_uses_the_default(self):
        from fighthealthinsurance.staff_views import StorageHealthView

        self.assertEqual(
            StorageHealthView._sample_size(None), storage_health.DEFAULT_SAMPLE
        )

    def test_garbage_uses_the_default(self):
        from fighthealthinsurance.staff_views import StorageHealthView

        self.assertEqual(
            StorageHealthView._sample_size("lots"), storage_health.DEFAULT_SAMPLE
        )

    def test_large_value_is_capped(self):
        from fighthealthinsurance.staff_views import StorageHealthView

        self.assertEqual(
            StorageHealthView._sample_size("100000"), storage_health.MAX_SAMPLE
        )

    def test_negative_value_is_zero(self):
        from fighthealthinsurance.staff_views import StorageHealthView

        self.assertEqual(StorageHealthView._sample_size("-5"), 0)


class CheckReachableTest(TestCase):
    def test_existing_directory_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            reach = storage_health.check_reachable(FileSystemStorage(location=tmp))
        self.assertTrue(reach["ok"])

    def test_missing_directory_names_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "not-mounted")
        reach = storage_health.check_reachable(FileSystemStorage(location=missing))
        self.assertIn(missing, reach["error"])

    def test_unconfigured_backend_is_not_ok(self):
        self.assertFalse(storage_health.check_reachable(None)["ok"])

    def test_object_store_error_degrades_instead_of_raising(self):
        store = mock.Mock(spec=["exists"])
        store.exists.side_effect = ConnectionError("refused")
        reach = storage_health.check_reachable(store)
        self.assertEqual(reach["error"], "refused")


def _s3_error(code):
    return minio.error.S3Error(mock.Mock(), code, code, "/docs", "req", "host")


class ObjectStoreTest(TestCase):
    """The real MinioStorage adapter over a stand-in client.

    The adapter's own exists() answers False for every S3 error and for a
    dropped connection, so these fail if the checks fall back to it.
    """

    def setUp(self):
        self.client = mock.Mock(spec=minio.Minio)
        self.store = MinioStorage(
            self.client, bucket_name="docs", assume_bucket_exists=True
        )

    def test_answering_bucket_is_reachable(self):
        self.client.list_objects.return_value = iter([])
        self.assertTrue(storage_health.check_reachable(self.store)["ok"])

    def test_denied_bucket_head_does_not_matter_when_listing_works(self):
        """The probe is the startup check's listing, not a bucket HEAD."""
        self.client.bucket_exists.side_effect = _s3_error("AccessDenied")
        self.client.list_objects.return_value = iter([])
        self.assertTrue(storage_health.check_reachable(self.store)["ok"])

    def test_access_denied_is_unreachable(self):
        self.client.list_objects.side_effect = _s3_error("AccessDenied")
        self.assertFalse(storage_health.check_reachable(self.store)["ok"])

    def test_dropped_connection_is_unreachable(self):
        self.client.list_objects.side_effect = ConnectionError("reset")
        self.assertFalse(storage_health.check_reachable(self.store)["ok"])

    def test_missing_bucket_is_unreachable_and_named(self):
        self.client.list_objects.side_effect = _s3_error("NoSuchBucket")
        reach = storage_health.check_reachable(self.store)
        self.assertIn("NoSuchBucket", reach["error"])

    def test_stored_object_is_a_copy(self):
        self.assertEqual(storage_health._copy(self.store, "a.pdf"), "yes")

    def test_no_such_key_is_no_copy(self):
        self.client.stat_object.side_effect = _s3_error("NoSuchKey")
        self.assertEqual(storage_health._copy(self.store, "a.pdf"), "no")

    def test_access_denied_on_an_object_is_an_error_not_a_missing_copy(self):
        self.client.stat_object.side_effect = _s3_error("AccessDenied")
        self.assertEqual(storage_health._copy(self.store, "a.pdf"), "error")

    def test_dropped_connection_on_an_object_is_an_error(self):
        self.client.stat_object.side_effect = ConnectionError("reset")
        self.assertEqual(storage_health._copy(self.store, "a.pdf"), "error")

    def test_object_store_counts_as_off_disk(self):
        self.assertEqual(storage_health.placement(self.store), "object store")


class BackendRowsTest(_StorageTestCase):
    def test_answering_backends_are_ok(self):
        self.assertEqual([r["level"] for r in self.rows()], ["ok"] * 3)

    def test_unanswering_backend_fails(self):
        shutil.rmtree(self.dirs[1])
        self.assertEqual(self.rows()[1]["level"], storage_health.FAIL)

    def test_disk_past_the_warning_line_warns(self):
        with mock.patch.object(
            storage_health.shutil, "disk_usage", return_value=_Usage(100, 90, 10)
        ):
            self.assertEqual(self.rows()[0]["level"], storage_health.WARN)

    def test_unreadable_disk_warns(self):
        with mock.patch.object(
            storage_health.shutil, "disk_usage", side_effect=OSError(5, "EIO")
        ):
            self.assertEqual(self.rows()[0]["level"], storage_health.WARN)

    def test_disk_past_the_failure_line_fails(self):
        with mock.patch.object(
            storage_health.shutil, "disk_usage", return_value=_Usage(100, 97, 3)
        ):
            self.assertEqual(self.rows()[0]["level"], storage_health.FAIL)

    def test_placement_marks_off_disk_backends(self):
        self.assertEqual([r["off_disk"] for r in self.rows()], [False, False, True])

    def test_unknown_backends_are_numbered(self):
        self.assertEqual(
            [r["label"] for r in self.rows()], ["backend 1", "backend 2", "backend 3"]
        )


class UnconfiguredSlotsTest(TestCase):
    def test_object_store_configured_but_left_out_fails(self):
        with override_settings(
            EXTERNAL_STORAGE_B=None, MINIO_STORAGE_ENDPOINT="minio.example.com"
        ):
            slots = {s["label"]: s for s in storage_health.unconfigured_slots()}
        self.assertEqual(slots["EXTERNAL_STORAGE_B"]["level"], storage_health.FAIL)

    def test_slot_never_configured_is_off(self):
        with override_settings(EXTERNAL_STORAGE_C=None, MINIO_STORAGE_ENDPOINT=None):
            slots = {s["label"]: s for s in storage_health.unconfigured_slots()}
        self.assertEqual(slots["EXTERNAL_STORAGE_C"]["level"], storage_health.OFF)


class SampleRecentFilesTest(_StorageTestCase):
    def sample(self, **kwargs):
        return storage_health.sample_recent_files(self.rows(), **kwargs)

    def remove_copy(self, row, index):
        os.remove(os.path.join(self.dirs[index], row.plan_document_enc.name))

    def test_document_with_every_copy_is_safe(self):
        self.plan_document()
        self.assertEqual(self.sample()["states"][storage_health.SAFE], 1)

    def test_document_missing_its_off_disk_copy_is_local_only(self):
        self.remove_copy(self.plan_document(), 2)
        self.assertEqual(self.sample()["states"][storage_health.LOCAL_ONLY], 1)

    def test_document_with_no_copy_is_missing(self):
        row = self.plan_document()
        for index in range(3):
            self.remove_copy(row, index)
        self.assertEqual(self.sample()["states"][storage_health.MISSING], 1)

    def test_unanswering_backend_is_skipped_not_waited_on(self):
        self.plan_document()
        shutil.rmtree(self.dirs[2])
        flagged = self.sample()["flagged"]
        self.assertEqual(flagged[0]["copies"]["backend 3"], "skipped")

    def test_no_copy_and_an_unanswering_backend_is_unknown(self):
        row = self.plan_document()
        self.remove_copy(row, 0)
        self.remove_copy(row, 1)
        shutil.rmtree(self.dirs[2])
        self.assertEqual(self.sample()["states"][storage_health.UNKNOWN], 1)

    def test_missing_copy_is_counted_against_its_backend(self):
        self.remove_copy(self.plan_document(), 1)
        self.assertEqual(self.sample()["per_backend"]["backend 2"]["no"], 1)

    def test_past_the_deadline_documents_are_not_checked(self):
        self.plan_document()
        sample = self.sample(deadline_s=-1)
        self.assertEqual(sample["states"][storage_health.NOT_CHECKED], 1)

    def test_unchecked_documents_are_not_counted_as_checked(self):
        self.plan_document()
        fields = {f["label"]: f for f in self.sample(deadline_s=-1)["fields"]}
        self.assertEqual(fields["PlanDocuments.plan_document_enc"]["sampled"], 0)

    def test_backends_reached_after_the_deadline_are_skipped(self):
        """The deadline is checked before each backend, not once per document."""
        self.plan_document()
        clock = [0.0]

        def slow_copy(backend, name):
            clock[0] += 5.0
            return "yes"

        fake_time = mock.Mock(monotonic=lambda: clock[0])
        with mock.patch.object(storage_health, "time", fake_time), mock.patch.object(
            storage_health, "_copy", side_effect=slow_copy
        ):
            sample = self.sample(deadline_s=1)
        self.assertEqual(
            list(sample["flagged"][0]["copies"].values()), ["yes", "skipped", "skipped"]
        )

    def test_rows_with_files_are_counted_per_field(self):
        self.plan_document()
        fields = {f["label"]: f for f in self.sample()["fields"]}
        self.assertEqual(fields["PlanDocuments.plan_document_enc"]["files"], 1)

    def test_only_the_newest_documents_are_sampled(self):
        oldest = self.plan_document()
        for _ in range(2):
            self.plan_document()
        self.remove_copy(oldest, 2)
        sample = self.sample(per_field=2)
        self.assertNotIn(oldest.pk, {doc["pk"] for doc in sample["flagged"]})


class ProblemsTest(_StorageTestCase):
    def levels(self, sample=None, unconfigured=()):
        found = storage_health.problems(self.rows(), list(unconfigured), sample)
        return [p["level"] for p in found]

    def test_healthy_storage_has_no_problems(self):
        self.assertEqual(self.levels(), [])

    def test_no_backend_in_the_write_path_fails(self):
        with mock.patch.object(self.storage, "backends", []):
            self.assertEqual(self.levels(), [storage_health.FAIL])

    def test_every_copy_on_the_app_disk_warns(self):
        self.set_off_disk()
        self.assertEqual(self.levels(), [storage_health.WARN])

    def test_failing_slot_sorts_first(self):
        shutil.rmtree(self.dirs[0])
        slot = {"label": "EXTERNAL_STORAGE_B", "level": "fail", "note": "down"}
        self.assertEqual(self.levels(unconfigured=[slot])[0], storage_health.FAIL)

    def write_results(self, *oks):
        return [
            {"label": f"backend {i + 1}", "ok": ok, "error": None if ok else "EROFS"}
            for i, ok in enumerate(oks)
        ]

    def test_every_backend_failing_the_write_test_fails(self):
        results = self.write_results(False, False, False)
        found = storage_health.problems(self.rows(), [], None, results)
        self.assertEqual(found[0]["level"], storage_health.FAIL)

    def test_one_backend_failing_the_write_test_only_warns(self):
        results = self.write_results(False, True, True)
        found = storage_health.problems(self.rows(), [], None, results)
        self.assertEqual([p["level"] for p in found], [storage_health.WARN])

    def test_unreadable_disk_is_a_problem(self):
        with mock.patch.object(
            storage_health.shutil, "disk_usage", side_effect=OSError(5, "EIO")
        ):
            found = storage_health.problems(self.rows(), [], None)
        self.assertIn("could not be read", found[0]["text"])

    def test_unchecked_documents_warn(self):
        self.plan_document()
        sample = storage_health.sample_recent_files(self.rows(), deadline_s=-1)
        self.assertEqual(self.levels(sample=sample), [storage_health.WARN])

    def test_off_disk_backend_missing_a_safe_documents_copy_warns(self):
        """Safe on one off-disk backend still means another lost its copy."""
        self.set_off_disk(self.backends[1], self.backends[2])
        row = self.plan_document()
        os.remove(os.path.join(self.dirs[1], row.plan_document_enc.name))
        sample = storage_health.sample_recent_files(self.rows())
        found = storage_health.problems(self.rows(), [], sample)
        self.assertIn("backend 2 answers but has no copy of 1", found[0]["text"])

    def test_local_only_documents_warn_when_an_off_disk_backend_answers(self):
        row = self.plan_document()
        os.remove(os.path.join(self.dirs[2], row.plan_document_enc.name))
        sample = storage_health.sample_recent_files(self.rows())
        self.assertEqual(self.levels(sample=sample), [storage_health.WARN])


class WriteTestTest(_StorageTestCase):
    def test_every_backend_passes(self):
        results = storage_health.write_test(self.rows())
        self.assertEqual([r["ok"] for r in results], [True] * 3)

    def test_no_probe_is_left_behind(self):
        storage_health.write_test(self.rows())
        self.assertEqual([os.listdir(d) for d in self.dirs], [[], [], []])

    def test_one_failing_backend_does_not_stop_the_others(self):
        with mock.patch.object(
            self.backends[1], "save", side_effect=PermissionError("read-only")
        ):
            results = storage_health.write_test(self.rows())
        self.assertEqual([r["ok"] for r in results], [True, False, True])

    def test_bytes_that_do_not_read_back_fail(self):
        with mock.patch.object(storage_health, "_read", return_value=b"other"):
            results = storage_health.write_test(self.rows())
        self.assertIn("different bytes", results[0]["error"])

    def test_a_failed_read_removes_the_probe(self):
        with mock.patch.object(storage_health, "_read", side_effect=OSError("eio")):
            storage_health.write_test(self.rows())
        self.assertEqual([os.listdir(d) for d in self.dirs], [[], [], []])

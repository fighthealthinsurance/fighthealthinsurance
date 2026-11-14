"""The admin's HumanJudge controls: upload from the Chooser Task list (the
action, and the "upload N unsent cases" button) and from a case's own page,
upload again from the submissions, download and CSV export on the verdicts.
chooser_humanjudge itself is tested in tests/async/test_chooser_humanjudge.py;
here its two calls are stood in for, so nothing reaches HumanJudge."""

import csv
import io
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance import chooser_humanjudge
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    HumanJudgeSubmission,
    HumanJudgeVerdict,
)

TASKS = reverse("admin:fighthealthinsurance_choosertask_changelist")
SUBMISSIONS = reverse("admin:fighthealthinsurance_humanjudgesubmission_changelist")
VERDICTS = reverse("admin:fighthealthinsurance_humanjudgeverdict_changelist")
DOWNLOAD = reverse("admin:fighthealthinsurance_humanjudgeverdict_download")
UPLOAD_UNSENT = reverse(
    "admin:fighthealthinsurance_choosertask_humanjudge_upload_unsent"
)

# A fake key only: upload_tasks is always stood in for, so nothing is sent.
ON = override_settings(CHOOSER_HUMANJUDGE=True, HUMANJUDGE_API_KEY="gj_sk_test_fake")


def _case(status="READY", source="synthetic", task_type="appeal", **fields):
    task = ChooserTask.objects.create(
        task_type=task_type, status=status, source=source, **fields
    )
    ChooserCandidate.objects.create(
        task=task,
        candidate_index=0,
        kind="appeal_letter",
        model_name="model-a",
        content="Dear insurer",
    )
    return task


class HumanJudgeAdminTest(TestCase):
    def setUp(self):
        admin = get_user_model().objects.create_superuser(
            username="admin", password="pw123", email="admin@example.com"
        )
        self.client.force_login(admin)
        self.task = ChooserTask.objects.create(
            task_type="appeal", status="READY", source="synthetic"
        )
        self.candidate = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-a",
            content="Dear insurer",
        )

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def submission(self, status="SENT"):
        return HumanJudgeSubmission.objects.create(
            task=self.task,
            candidate=self.candidate,
            inference_id="gj_inf_1_abc",
            status=status,
        )

    @ON
    def test_upload_action_sends_the_selected_cases(self):
        upload = AsyncMock(return_value=chooser_humanjudge.UploadResult(sent=1))
        with patch.object(chooser_humanjudge, "upload_tasks", upload):
            self.client.post(
                TASKS,
                {"action": "upload_to_humanjudge", "_selected_action": [self.task.pk]},
            )
        upload.assert_awaited_once_with([self.task.pk])

    @ON
    def test_upload_action_reports_what_went(self):
        result = chooser_humanjudge.UploadResult(sent=3, not_sendable=1)
        with patch.object(
            chooser_humanjudge, "upload_tasks", AsyncMock(return_value=result)
        ):
            response = self.client.post(
                TASKS,
                {"action": "upload_to_humanjudge", "_selected_action": [self.task.pk]},
                follow=True,
            )
        self.assertIn("Uploaded 3 draft(s) to HumanJudge.", self.messages(response)[0])

    def test_upload_action_says_so_when_humanjudge_is_not_configured(self):
        response = self.client.post(
            TASKS,
            {"action": "upload_to_humanjudge", "_selected_action": [self.task.pk]},
            follow=True,
        )
        self.assertTrue(self.messages(response)[0].startswith("Nothing uploaded"))

    @ON
    def test_upload_again_sends_the_submissions_cases(self):
        submission = self.submission(status="FAILED")
        upload = AsyncMock(return_value=chooser_humanjudge.UploadResult(sent=1))
        with patch.object(chooser_humanjudge, "upload_tasks", upload):
            self.client.post(
                SUBMISSIONS,
                {"action": "upload_again", "_selected_action": [submission.pk]},
            )
        upload.assert_awaited_once_with([self.task.pk])

    def test_uploaded_filter_lists_only_cases_with_a_sent_draft(self):
        unsent = ChooserTask.objects.create(task_type="appeal", status="READY")
        self.submission()
        response = self.client.get(TASKS, {"humanjudge": "sent"})
        shown = [obj.pk for obj in response.context["cl"].result_list]
        self.assertEqual((shown, unsent.pk in shown), ([self.task.pk], False))

    def test_verdict_list_shows_the_download_button(self):
        response = self.client.get(VERDICTS)
        self.assertContains(response, 'id="humanjudge-download"')

    def test_download_needs_a_post(self):
        self.assertEqual(self.client.get(DOWNLOAD).status_code, 405)

    def test_download_fetches_verdicts_and_reports_them(self):
        result = chooser_humanjudge.DownloadResult(new_verdicts=4, task_ids={1, 2})
        download = AsyncMock(return_value=result)
        with patch.object(chooser_humanjudge, "download_verdicts", download):
            response = self.client.post(DOWNLOAD, follow=True)
        self.assertIn("Downloaded 4 new verdict(s) on 2 case(s)", self.messages(response)[0])

    def test_download_says_so_when_humanjudge_is_not_configured(self):
        response = self.client.post(DOWNLOAD, follow=True)
        self.assertTrue(self.messages(response)[0].startswith("Nothing downloaded"))

    def test_csv_export_names_the_model_behind_each_verdict(self):
        verdict = HumanJudgeVerdict.objects.create(
            submission=self.submission(),
            task=self.task,
            candidate=self.candidate,
            external_key="id:1",
            verdict="flag",
            flag_category="Wrong",
        )
        response = self.client.post(
            VERDICTS, {"action": "export_csv", "_selected_action": [verdict.pk]}
        )
        rows = list(csv.DictReader(io.StringIO(response.content.decode())))
        self.assertEqual(
            (rows[0]["model_name"], rows[0]["verdict"]), ("model-a", "flag")
        )


class HumanJudgeUploadButtonsTest(TestCase):
    """The two buttons: "upload N unsent cases" on the list, and "Upload to
    HumanJudge" on a case's page."""

    def setUp(self):
        admin = get_user_model().objects.create_superuser(
            username="admin", password="pw123", email="admin@example.com"
        )
        self.client.force_login(admin)
        self.upload = AsyncMock(return_value=chooser_humanjudge.UploadResult(sent=1))

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def uploaded_ids(self):
        return sorted(self.upload.await_args.args[0])

    def test_the_list_shows_the_upload_button_with_its_count(self):
        response = self.client.get(TASKS)
        self.assertContains(response, 'id="humanjudge-upload-unsent"')

    @ON
    def test_upload_unsent_sends_that_many_cases(self):
        tasks = [_case() for _ in range(3)]
        with patch.object(chooser_humanjudge, "upload_tasks", self.upload):
            self.client.post(UPLOAD_UNSENT, {"count": 2})
        self.assertEqual(self.uploaded_ids(), sorted(t.id for t in tasks[1:]))

    @ON
    def test_upload_unsent_skips_cases_already_on_humanjudge(self):
        sent, waiting = _case(), _case()
        candidate = sent.candidates.get()
        HumanJudgeSubmission.objects.create(
            task=sent, candidate=candidate, inference_id="gj_inf_1", status="SENT"
        )
        with patch.object(chooser_humanjudge, "upload_tasks", self.upload):
            self.client.post(UPLOAD_UNSENT, {"count": 5})
        self.assertEqual(self.uploaded_ids(), [waiting.id])

    @ON
    def test_upload_unsent_rejects_a_count_past_the_cap(self):
        _case()
        with patch.object(chooser_humanjudge, "upload_tasks", self.upload):
            response = self.client.post(
                UPLOAD_UNSENT,
                {"count": chooser_humanjudge.MAX_TASKS_PER_UPLOAD + 1},
                follow=True,
            )
        self.assertEqual(
            (self.upload.await_count, "whole number" in self.messages(response)[0]),
            (0, True),
        )

    @ON
    def test_upload_unsent_with_nothing_waiting_says_so(self):
        response = self.client.post(UPLOAD_UNSENT, {"count": 5}, follow=True)
        self.assertIn("Nothing to upload", self.messages(response)[0])

    def test_upload_unsent_needs_a_post(self):
        self.assertEqual(self.client.get(UPLOAD_UNSENT).status_code, 405)

    def test_upload_unsent_needs_staff(self):
        self.client.logout()
        response = self.client.post(UPLOAD_UNSENT, {"count": 1})
        self.assertEqual(
            (response.status_code, "/login/" in response["Location"]), (302, True)
        )

    def test_a_sendable_case_page_shows_the_upload_button(self):
        task = _case()
        response = self.client.get(
            reverse("admin:fighthealthinsurance_choosertask_change", args=[task.id])
        )
        self.assertContains(response, 'id="humanjudge-upload-one"')

    def test_a_case_that_may_not_go_has_no_upload_button(self):
        task = _case(source="real_denial")
        response = self.client.get(
            reverse("admin:fighthealthinsurance_choosertask_change", args=[task.id])
        )
        self.assertNotContains(response, 'id="humanjudge-upload-one"')

    @ON
    def test_the_case_button_uploads_that_case(self):
        task = _case()
        with patch.object(chooser_humanjudge, "upload_tasks", self.upload):
            self.client.post(
                reverse(
                    "admin:fighthealthinsurance_choosertask_humanjudge_upload",
                    args=[task.id],
                )
            )
        self.assertEqual(self.uploaded_ids(), [task.id])


@ON
class UploadSelectionTest(TestCase):
    """Which cases "upload N unsent" picks, and when it waits."""

    def test_undecided_cases_go_before_newer_ones(self):
        undecided = _case(status="EXHAUSTED", settled_reason="undecided")
        _case()
        self.assertEqual(chooser_humanjudge.unsent_task_ids(1), [undecided.id])

    def test_cases_that_may_not_go_are_never_picked(self):
        for kwargs in (
            {"source": "real_denial"},
            {"task_type": "chat"},
            {"status": "DISABLED"},
            {"status": "QUEUED"},
        ):
            _case(**kwargs)
        self.assertEqual(chooser_humanjudge.unsent_task_ids(10), [])

    def test_a_small_upload_waits_and_reports(self):
        upload = AsyncMock(return_value=chooser_humanjudge.UploadResult(sent=2))
        with patch.object(chooser_humanjudge, "upload_tasks", upload):
            result = chooser_humanjudge.start_upload([1, 2])
        self.assertEqual(result.sent, 2)

    def test_a_big_upload_runs_in_the_background(self):
        from fighthealthinsurance.utils import join_fire_and_forget_threads

        ids = list(range(1, chooser_humanjudge.INLINE_UPLOAD_LIMIT + 2))
        upload = AsyncMock(return_value=chooser_humanjudge.UploadResult())
        with patch.object(chooser_humanjudge, "upload_tasks", upload):
            result = chooser_humanjudge.start_upload(ids)
            join_fire_and_forget_threads(timeout=10)
        self.assertEqual((result, upload.await_args.args[0]), (None, ids))

    def test_more_than_the_cap_is_refused_before_anything_starts(self):
        ids = list(range(1, chooser_humanjudge.MAX_TASKS_PER_UPLOAD + 2))
        with self.assertRaises(chooser_humanjudge.HumanJudgeError):
            chooser_humanjudge.start_upload(ids)

"""The admin's HumanJudge controls: upload from the Chooser Task list,
upload again from the submissions, download and CSV export on the verdicts.
chooser_humanjudge itself is tested in tests/async/test_chooser_humanjudge.py;
here its two calls are stood in for, so nothing reaches HumanJudge."""

import csv
import io
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
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

    def test_upload_action_sends_the_selected_cases(self):
        upload = AsyncMock(return_value=chooser_humanjudge.UploadResult(sent=1))
        with patch.object(chooser_humanjudge, "upload_tasks", upload):
            self.client.post(
                TASKS,
                {"action": "upload_to_humanjudge", "_selected_action": [self.task.pk]},
            )
        upload.assert_awaited_once_with([self.task.pk])

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

"""The admin's HumanJudge arena controls: the export action on the Chooser
Task list and its setup page, and the arena verdicts' download and CSV.
humanjudge_arena itself is tested in tests/async/test_humanjudge_arena.py;
here the router's models and the backfill are stood in for, so no model is
asked for a letter."""

import csv
import io
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance import chooser_tasks, humanjudge_arena
from fighthealthinsurance.models import (
    ChooserTask,
    HumanJudgeArenaAnswer,
    HumanJudgeArenaCase,
    HumanJudgeArenaVerdict,
)

TASKS = reverse("admin:fighthealthinsurance_choosertask_changelist")
VERDICTS = reverse("admin:fighthealthinsurance_humanjudgearenaverdict_changelist")
DOWNLOAD = reverse("admin:fighthealthinsurance_humanjudgearenaverdict_download")
CONTEXT = {"procedure": "MRI", "diagnosis": "back pain", "facts": ["PT failed"]}


class FakeModel:
    context_only = False

    def __init__(self, name):
        self.name = name


class HumanJudgeArenaAdminTest(TestCase):
    def setUp(self):
        admin = get_user_model().objects.create_superuser(
            username="admin", password="pw123", email="admin@example.com"
        )
        self.client.force_login(admin)
        self.task = ChooserTask.objects.create(
            task_type="appeal", status="READY", source="synthetic", context_json=CONTEXT
        )
        models = patch.object(
            chooser_tasks,
            "_comparable_backends",
            MagicMock(return_value=[FakeModel("fhi-local")]),
        )
        models.start()
        self.addCleanup(models.stop)
        backfill = patch.object(humanjudge_arena, "start_backfill")
        self.start_backfill = backfill.start()
        self.addCleanup(backfill.stop)

    def export(self):
        return self.client.post(
            TASKS,
            {"action": "export_to_humanjudge_arena", "_selected_action": [self.task.id]},
        )

    def test_export_page_holds_the_case_text_to_paste(self):
        response = self.export()
        prompt = HumanJudgeArenaCase.objects.get(task=self.task).prompt
        self.assertContains(response, prompt.splitlines()[0])

    def test_export_page_lists_each_model_with_its_request_template(self):
        response = self.export()
        self.assertContains(response, "&quot;model&quot;: &quot;fhi-local&quot;")

    @override_settings(HUMAN_JUDGE_OAI_CALLBACK="test-callback-not-real")
    def test_export_starts_writing_the_missing_answers_when_the_endpoint_is_on(self):
        self.export()
        case = HumanJudgeArenaCase.objects.get(task=self.task)
        self.start_backfill.assert_called_once_with([case.id])

    def test_export_warns_and_writes_nothing_while_the_endpoint_is_off(self):
        response = self.export()
        self.assertContains(response, 'id="arena-endpoint-off"')
        self.start_backfill.assert_not_called()

    def test_verdict_list_shows_the_download_button(self):
        self.assertContains(self.client.get(VERDICTS), 'id="humanjudge-arena-download"')

    def test_download_says_so_when_no_arena_is_configured(self):
        response = self.client.post(DOWNLOAD, follow=True)
        self.assertContains(response, "Nothing downloaded")

    def test_csv_export_names_the_model_and_how_its_answer_was_made(self):
        case = HumanJudgeArenaCase.objects.create(
            task=self.task, prompt="case", prompt_sha256="a" * 64
        )
        answer = HumanJudgeArenaAnswer.objects.create(
            case=case,
            prompt_sha256="a" * 64,
            model_name="fhi-local",
            content="letter",
            content_sha256="b" * 64,
            source="generated",
        )
        verdict = HumanJudgeArenaVerdict.objects.create(
            answer=answer, case=case, external_key="k", verdict="flag"
        )
        response = self.client.post(
            VERDICTS, {"action": "export_csv", "_selected_action": [verdict.id]}
        )
        row = list(csv.DictReader(io.StringIO(response.content.decode())))[0]
        self.assertEqual(
            (row["model_name"], row["answer_source"], row["verdict"]),
            ("fhi-local", "generated", "flag"),
        )

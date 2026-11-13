"""Settling Arena cases through the API and the management command: a case
whose votes show a clear winner, or that is clearly undecided, stops being
served (chooser_settlement)."""

import io
import json
from unittest.mock import patch

from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance import chooser_settlement
from fighthealthinsurance.chooser_tokens import candidate_token
from fighthealthinsurance.rest_views import ChooserViewSet
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserSkip,
    ChooserTask,
    ChooserVote,
)


class ChooserSettlementAPITest(APITestCase):
    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.task = ChooserTask.objects.create(task_type="appeal", status="READY")
        self.a, self.b, self.c = (
            ChooserCandidate.objects.create(
                task=self.task,
                candidate_index=i,
                kind="appeal_letter",
                model_name=f"model-{i}",
                content=f"Content {i}",
            )
            for i in range(3)
        )
        self.ids = [self.a.id, self.b.id, self.c.id]

    def _stored_votes(self, chosen, count):
        """``count`` votes for ``chosen`` from other judges."""
        for i in range(count):
            ChooserVote.objects.create(
                task=self.task,
                chosen_candidate=chosen,
                presented_candidate_ids=self.ids,
                session_key=f"other-voter-{chosen.id}-{i}",
            )

    def _stored_skips(self, count, served=True):
        for i in range(count):
            ChooserSkip.objects.create(
                task=self.task,
                session_key=f"skipper-{i}",
                served_at=timezone.now() if served else None,
            )

    def _serve(self):
        response = self.client.get(reverse("chooser-next-appeal"))
        self.assertEqual(response.json()["task_id"], self.task.id)

    def _vote(self, chosen):
        return self.client.post(
            reverse("chooser-vote"),
            json.dumps(
                {
                    "task_id": self.task.id,
                    "chosen_candidate_id": candidate_token(chosen.id),
                    "presented_candidate_ids": [candidate_token(i) for i in self.ids],
                }
            ),
            content_type="application/json",
        )

    def _skip(self):
        return self.client.post(
            reverse("chooser-skip"),
            json.dumps({"task_id": self.task.id}),
            content_type="application/json",
        )

    def test_the_vote_that_makes_a_clear_winner_settles_the_task(self):
        self._stored_votes(self.a, 4)

        self.assertEqual(self._vote(self.a).status_code, status.HTTP_200_OK)

        self.task.refresh_from_db()
        self.assertEqual(
            (self.task.status, self.task.settled_reason), ("EXHAUSTED", "clear_winner")
        )

    def test_a_contested_vote_leaves_the_task_ready(self):
        self._stored_votes(self.a, 3)
        self._stored_votes(self.b, 1)

        self._vote(self.b)

        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.settled_reason), ("READY", ""))

    def test_the_skip_that_tips_the_share_settles_the_task_undecided(self):
        self._stored_votes(self.a, 2)
        self._stored_skips(2)
        self._serve()

        self.assertEqual(self._skip().status_code, status.HTTP_200_OK)

        self.task.refresh_from_db()
        self.assertEqual(
            (self.task.status, self.task.settled_reason), ("EXHAUSTED", "undecided")
        )

    def test_a_skip_of_a_case_never_served_does_not_settle_it(self):
        self._stored_votes(self.a, 2)
        self._stored_skips(2)

        self._skip()

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "READY")

    def test_a_skip_of_a_case_never_served_still_hides_it_from_the_judge(self):
        self._skip()

        self.assertTrue(ChooserSkip.objects.filter(task=self.task).exists())

    def test_skips_without_a_recorded_serve_do_not_count(self):
        self._stored_votes(self.a, 2)
        self._stored_skips(3, served=False)

        self.assertEqual(chooser_settlement.counted_skips(self.task.id), 0)

    def test_a_vote_drops_the_same_judges_earlier_skip(self):
        self._serve()
        self._skip()

        self._vote(self.a)

        self.assertFalse(ChooserSkip.objects.filter(task=self.task).exists())

    def test_a_cookieless_vote_drops_a_skip_from_the_same_address(self):
        client_key = ChooserViewSet.client_key_for("203.0.113.7")
        ChooserSkip.objects.create(
            task=self.task,
            session_key="lost-session",
            client_key=client_key,
            served_at=timezone.now(),
        )
        self.client.cookies.clear()

        with patch.object(
            ChooserViewSet, "_client_address", return_value="203.0.113.7"
        ):
            self._vote(self.a)

        self.assertEqual(chooser_settlement.counted_skips(self.task.id), 0)

    def test_a_vote_leaves_another_sessions_skip_from_the_same_address(self):
        client_key = ChooserViewSet.client_key_for("203.0.113.7")
        ChooserSkip.objects.create(
            task=self.task,
            session_key="another-browser",
            client_key=client_key,
            served_at=timezone.now(),
        )
        self._serve()

        with patch.object(
            ChooserViewSet, "_client_address", return_value="203.0.113.7"
        ):
            self._vote(self.a)

        self.assertEqual(chooser_settlement.counted_skips(self.task.id), 1)

    def test_a_judge_who_skipped_then_voted_counts_as_a_reviewer_only(self):
        self._stored_skips(2)
        ChooserVote.objects.create(
            task=self.task,
            chosen_candidate=self.a,
            presented_candidate_ids=self.ids,
            session_key="skipper-0",
        )

        self.assertEqual(chooser_settlement.counted_skips(self.task.id), 1)

    def test_a_settled_task_is_not_served_to_a_new_judge(self):
        self._stored_votes(self.a, 5)
        ChooserTask.objects.filter(id=self.task.id).update(
            status="EXHAUSTED", settled_reason="clear_winner"
        )

        with patch(
            "fighthealthinsurance.chooser_tasks.trigger_prefill_async",
            return_value=True,
        ):
            response = self.client.get(reverse("chooser-next-appeal"))

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_a_judge_served_the_task_before_it_settled_can_still_vote(self):
        ChooserTask.objects.filter(id=self.task.id).update(
            status="EXHAUSTED", settled_reason="clear_winner"
        )

        self.assertEqual(self._vote(self.b).status_code, status.HTTP_200_OK)

    def test_an_exhausted_task_that_was_not_settled_still_refuses_votes(self):
        ChooserTask.objects.filter(id=self.task.id).update(status="EXHAUSTED")

        self.assertEqual(self._vote(self.b).status_code, status.HTTP_400_BAD_REQUEST)

    @override_settings(CHOOSER_SETTLE_TASKS=False)
    def test_settling_can_be_switched_off(self):
        self._stored_votes(self.a, 4)

        self._vote(self.a)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "READY")

    def test_a_settlement_error_does_not_lose_the_vote(self):
        self._stored_votes(self.a, 4)

        with patch(
            "fighthealthinsurance.chooser_settlement.task_verdict",
            side_effect=RuntimeError("boom"),
        ):
            response = self._vote(self.a)

        self.assertEqual(response.status_code, status.HTTP_200_OK)


class SettleChooserTasksCommandTest(APITestCase):
    def setUp(self):
        self.task = ChooserTask.objects.create(task_type="appeal", status="READY")
        self.a, self.b = (
            ChooserCandidate.objects.create(
                task=self.task,
                candidate_index=i,
                kind="appeal_letter",
                model_name=f"model-{i}",
                content=f"Content {i}",
            )
            for i in range(2)
        )
        for i in range(5):
            ChooserVote.objects.create(
                task=self.task,
                chosen_candidate=self.a,
                presented_candidate_ids=[self.a.id, self.b.id],
                session_key=f"voter-{i}",
            )

    def _run(self, *args):
        call_command("settle_chooser_tasks", *args, stdout=io.StringIO())
        self.task.refresh_from_db()

    def test_settles_a_ready_task_voted_on_before_settling_existed(self):
        self._run()

        self.assertEqual(
            (self.task.status, self.task.settled_reason), ("EXHAUSTED", "clear_winner")
        )

    def test_dry_run_stores_nothing(self):
        self._run("--dry-run")

        self.assertEqual(self.task.status, "READY")

    @override_settings(CHOOSER_SETTLE_MIN_REVIEWS=50)
    def test_reopen_serves_again_a_task_the_rules_no_longer_settle(self):
        ChooserTask.objects.filter(id=self.task.id).update(
            status="EXHAUSTED", settled_reason="clear_winner"
        )

        self._run("--reopen")

        self.assertEqual((self.task.status, self.task.settled_reason), ("READY", ""))

    def test_without_reopen_a_settled_task_is_left_alone(self):
        ChooserTask.objects.filter(id=self.task.id).update(
            status="EXHAUSTED", settled_reason="undecided"
        )

        self._run()

        self.assertEqual(self.task.settled_reason, "undecided")

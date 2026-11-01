"""Test the Chooser (Best-Of Selection) API functionality"""

import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from rest_framework import status
from rest_framework.test import APITestCase

from fhi_users.models import ExtraUserProperties, ProfessionalUser, UserDomain
from fighthealthinsurance.chooser_tasks import (
    CHOOSER_GENERATION_CLAIM_SECONDS,
    _generation_claim_key,
    annotate_evidence_verdicts,
)
from fighthealthinsurance.chooser_tokens import candidate_token, resolve_candidate
from fighthealthinsurance.ml import research_judging
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserSkip,
    ChooserTask,
    ChooserVote,
    Denial,
    ModelHealthAlertState,
    PubMedArticleJudgment,
)

User = get_user_model()


class ChooserTaskModelTest(APITestCase):
    """Test ChooserTask model lifecycle."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def test_create_chooser_task(self):
        """Test creating a ChooserTask."""
        task = ChooserTask.objects.create(
            task_type="appeal",
            status="QUEUED",
            source="synthetic",
            num_candidates_expected=4,
        )
        self.assertEqual(task.task_type, "appeal")
        self.assertEqual(task.status, "QUEUED")
        self.assertEqual(task.source, "synthetic")
        self.assertEqual(task.num_candidates_expected, 4)
        self.assertEqual(task.num_candidates_generated, 0)

    def test_task_with_context_json(self):
        """Test creating a task with JSON context."""
        context = {
            "procedure": "Physical Therapy",
            "diagnosis": "Lower back pain",
            "denial_text_preview": "Your claim has been denied.",
        }
        task = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
            context_json=context,
        )
        self.assertEqual(task.context_json["procedure"], "Physical Therapy")
        self.assertEqual(task.context_json["diagnosis"], "Lower back pain")


class ChooserCandidateModelTest(APITestCase):
    """Test ChooserCandidate model."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def test_create_candidate(self):
        """Test creating a ChooserCandidate."""
        task = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
        )
        candidate = ChooserCandidate.objects.create(
            task=task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="test-model",
            content="This is a test appeal letter.",
            metadata={"source": "synthetic"},
        )
        self.assertEqual(candidate.task, task)
        self.assertEqual(candidate.candidate_index, 0)
        self.assertEqual(candidate.kind, "appeal_letter")
        self.assertTrue(candidate.is_active)
        # Candidates default to not-synthesized.
        self.assertFalse(candidate.synthesized)

    def test_create_synthesized_candidate(self):
        """A candidate can be marked as synthesized from multiple inputs."""
        task = ChooserTask.objects.create(task_type="appeal", status="READY")
        candidate = ChooserCandidate.objects.create(
            task=task,
            candidate_index=2,
            kind="appeal_letter",
            model_name="synthesized",
            synthesized=True,
            content="A synthesized appeal letter.",
        )
        self.assertTrue(candidate.synthesized)
        self.assertEqual(candidate.model_name, "synthesized")

    def test_candidate_unique_together(self):
        """Test that candidate_index is unique per task."""
        task = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
        )
        ChooserCandidate.objects.create(
            task=task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="test-model",
            content="First candidate",
        )
        # Creating another candidate with the same index should fail
        with self.assertRaises(Exception):
            ChooserCandidate.objects.create(
                task=task,
                candidate_index=0,
                kind="appeal_letter",
                model_name="test-model-2",
                content="Duplicate index candidate",
            )


class ChooserVoteModelTest(APITestCase):
    """Test ChooserVote model."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def test_create_vote(self):
        """Test creating a ChooserVote."""
        task = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
        )
        candidate = ChooserCandidate.objects.create(
            task=task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="test-model",
            content="Test content",
        )
        vote = ChooserVote.objects.create(
            task=task,
            chosen_candidate=candidate,
            presented_candidate_ids=[candidate.id],
            session_key="test-session-123",
        )
        self.assertEqual(vote.task, task)
        self.assertEqual(vote.chosen_candidate, candidate)
        self.assertEqual(vote.session_key, "test-session-123")

    def test_vote_unique_per_session(self):
        """Test that a session can only vote once per task."""
        task = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
        )
        candidate = ChooserCandidate.objects.create(
            task=task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="test-model",
            content="Test content",
        )
        ChooserVote.objects.create(
            task=task,
            chosen_candidate=candidate,
            presented_candidate_ids=[candidate.id],
            session_key="test-session-123",
        )
        # Creating another vote with the same session key should fail
        with self.assertRaises(Exception):
            ChooserVote.objects.create(
                task=task,
                chosen_candidate=candidate,
                presented_candidate_ids=[candidate.id],
                session_key="test-session-123",
            )


class ChooserNextTaskAPITest(APITestCase):
    """Test the GET /api/chooser/next/{type} endpoints."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Create a READY task with candidates
        self.task = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
            context_json={
                "procedure": "Physical Therapy",
                "diagnosis": "Lower back pain",
            },
        )
        self.candidate1 = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-a",
            content="Dear Insurance Company, I am writing to appeal...",
        )
        self.candidate2 = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=1,
            kind="appeal_letter",
            model_name="model-b",
            content="To Whom It May Concern, This letter serves as...",
        )

    def test_get_next_appeal_task(self):
        """Test getting the next appeal task."""
        url = reverse("chooser-next-appeal")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertEqual(data["task_id"], self.task.id)
        self.assertEqual(data["task_type"], "appeal")
        self.assertEqual(len(data["candidates"]), 2)
        self.assertEqual(data["task_context"]["procedure"], "Physical Therapy")
        self.assertEqual(set(data["candidates"][0]), {"id", "kind", "content", "tool_calls"})

    def test_next_task_is_blind_to_the_author(self):
        """The payload names no model and carries no synthesized flag,
        metadata, row id or candidate_index: the last two run in generation
        order (internal first, synthesized last), so a voter who opened the
        network tab could tell the drafts apart by them. Each draft goes out
        as an opaque token instead. Authorship is joined back in when the
        votes are counted."""
        synthesized = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=2,
            kind="appeal_letter",
            model_name="synthesized",
            synthesized=True,
            content="A blend of the other drafts into one letter.",
            metadata={"source": "synthetic", "synthesized": True},
        )
        response = self.client.get(reverse("chooser-next-appeal"))
        body = response.content.decode()
        for leak in (
            "model_name",
            "synthesized",
            "metadata",
            "candidate_index",
            "model-a",
            "model-b",
        ):
            self.assertNotIn(leak, body)
        ids = [c["id"] for c in response.json()["candidates"]]
        # DRF renders compact JSON ("id":5, no space).
        for cid in (self.candidate1.id, self.candidate2.id, synthesized.id):
            self.assertNotIn(f'"id":{cid}', body)
        self.assertTrue(all(isinstance(i, str) and not i.isdigit() for i in ids))
        self.assertEqual(
            {resolve_candidate(t) for t in ids},
            {self.candidate1.id, self.candidate2.id, synthesized.id},
        )

    def test_tokens_differ_between_presentations(self):
        """Two fetches of the same candidate yield different tokens, so
        tokens cannot be compared across sessions or sorted into an order."""
        first = {
            resolve_candidate(c["id"]): c["id"]
            for c in self.client.get(reverse("chooser-next-appeal")).json()[
                "candidates"
            ]
        }
        second = {
            resolve_candidate(c["id"]): c["id"]
            for c in self.client.get(reverse("chooser-next-appeal")).json()[
                "candidates"
            ]
        }
        self.assertEqual(set(first), set(second))
        for cid in first:
            self.assertNotEqual(first[cid], second[cid])

    def test_next_task_serves_candidates_in_random_order(self):
        """The voter must be blind to model class: the stored order puts the
        internal model first and the synthesized draft last, so the served
        order is shuffled (the client persists its render order on the vote)."""
        url = reverse("chooser-next-appeal")
        with patch(
            "fighthealthinsurance.rest_views.random.shuffle",
            side_effect=lambda seq: seq.reverse(),
        ):
            data = self.client.get(url).json()
        served = [resolve_candidate(c["id"]) for c in data["candidates"]]
        self.assertEqual(served, [self.candidate2.id, self.candidate1.id])

    def test_next_task_serves_a_synthesized_candidate_like_any_other(self):
        """A synthesized draft is one more blind contender: served, and not
        marked out from the single-model drafts."""
        synthesized = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=2,
            kind="appeal_letter",
            model_name="synthesized",
            synthesized=True,
            content="An AI-synthesized appeal combining the other drafts.",
        )
        url = reverse("chooser-next-appeal")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        candidates = response.json()["candidates"]
        self.assertIn(synthesized.id, [resolve_candidate(c["id"]) for c in candidates])
        self.assertEqual(len({frozenset(c) for c in candidates}), 1)

    def test_session_exclusion(self):
        """Test that tasks already voted on are excluded."""
        # Force session creation first
        self.client.session.create()
        session_key = self.client.session.session_key

        # Create a vote for this task with the client's session
        ChooserVote.objects.create(
            task=self.task,
            chosen_candidate=self.candidate1,
            presented_candidate_ids=[self.candidate1.id, self.candidate2.id],
            session_key=session_key,
        )

        # Keep the background prefill from starting a real generation; it
        # reports one started, so the answer is "wait for it" (202).
        with patch(
            "fighthealthinsurance.chooser_tasks.trigger_prefill_async",
            return_value=True,
        ) as mock_prefill:
            # Only one task, already voted on: nothing to serve yet.
            url = reverse("chooser-next-appeal")
            response = self.client.get(url)

            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
            self.assertTrue(response.json()["generating"])
        # The pool is not empty, so the prefill has to be told this session
        # used the appeals up or it would never make another.
        mock_prefill.assert_called_once_with(exhausted="appeal")


class ChooserNextTaskDoesNotGenerateInlineTest(APITestCase):
    """An empty pool hands generation to the throttled background prefill.

    The endpoint is anonymous and unthrottled; generating a task inside the
    request held a worker for minutes and drove paid provider calls on demand.

    While a task is being written (the prefill it set off, or the process
    holding the type's generation claim) the answer is 202 with when to ask
    again, so the client waits for the task; it used to be 404, which the
    client read as "come back in a few minutes" and gave up on at once. 404
    now means nothing is underway.
    """

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def test_empty_pool_triggers_prefill_and_says_to_wait(self):
        ChooserTask.objects.all().delete()

        with patch(
            "fighthealthinsurance.chooser_tasks.trigger_prefill_async",
            return_value=True,
        ) as mock_prefill, patch(
            "fighthealthinsurance.chooser_tasks._generate_single_task"
        ) as mock_generate:
            response = self.client.get(reverse("chooser-next-appeal"))

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        body = response.json()
        self.assertTrue(body["generating"])
        self.assertEqual(body["task_type"], "appeal")
        self.assertGreater(body["retry_after"], 0)
        mock_prefill.assert_called_once()
        mock_generate.assert_not_called()

    def test_an_empty_pool_with_nothing_underway_is_404(self):
        ChooserTask.objects.all().delete()

        # The throttle held the prefill off (one ran within the window) and
        # no process holds the claim: nothing is coming.
        with patch(
            "fighthealthinsurance.chooser_tasks.trigger_prefill_async",
            return_value=False,
        ):
            response = self.client.get(reverse("chooser-next-appeal"))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        body = response.json()
        self.assertIn("No tasks available", body["message"])
        self.assertFalse(body["generating"])

    def test_a_claim_held_elsewhere_says_to_wait(self):
        ChooserTask.objects.all().delete()
        # Another process (the refill actor, an earlier prefill) holds the
        # appeal generation claim.
        ModelHealthAlertState.objects.create(
            key=_generation_claim_key("appeal"), last_alert_sent=timezone.now()
        )

        with patch(
            "fighthealthinsurance.chooser_tasks.trigger_prefill_async",
            return_value=False,
        ):
            appeal = self.client.get(reverse("chooser-next-appeal"))
            chat = self.client.get(reverse("chooser-next-chat"))

        self.assertEqual(appeal.status_code, status.HTTP_202_ACCEPTED)
        # The claim is per type: nothing is writing a chat task.
        self.assertEqual(chat.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_lapsed_claim_is_nothing_underway(self):
        ChooserTask.objects.all().delete()
        ModelHealthAlertState.objects.create(
            key=_generation_claim_key("appeal"),
            last_alert_sent=timezone.now()
            - datetime.timedelta(seconds=CHOOSER_GENERATION_CLAIM_SECONDS + 1),
        )

        with patch(
            "fighthealthinsurance.chooser_tasks.trigger_prefill_async",
            return_value=False,
        ):
            response = self.client.get(reverse("chooser-next-appeal"))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_failing_prefill_trigger_still_answers_404(self):
        ChooserTask.objects.all().delete()

        with patch(
            "fighthealthinsurance.chooser_tasks.trigger_prefill_async",
            side_effect=RuntimeError("no threads"),
        ):
            response = self.client.get(reverse("chooser-next-appeal"))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class ChooserTaskSelectionOrderingTest(APITestCase):
    """Test the task selection ordering logic."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Create multiple tasks with different vote counts
        self.task_no_votes = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
            context_json={"description": "No votes"},
        )
        ChooserCandidate.objects.create(
            task=self.task_no_votes,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-a",
            content="Content A",
        )

        self.task_one_vote = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
            context_json={"description": "One vote"},
        )
        candidate = ChooserCandidate.objects.create(
            task=self.task_one_vote,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-b",
            content="Content B",
        )
        ChooserVote.objects.create(
            task=self.task_one_vote,
            chosen_candidate=candidate,
            presented_candidate_ids=[candidate.id],
            session_key="other-session-123",
        )

        self.task_two_votes = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
            context_json={"description": "Two votes"},
        )
        candidate2 = ChooserCandidate.objects.create(
            task=self.task_two_votes,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-c",
            content="Content C",
        )
        ChooserVote.objects.create(
            task=self.task_two_votes,
            chosen_candidate=candidate2,
            presented_candidate_ids=[candidate2.id],
            session_key="other-session-456",
        )
        ChooserVote.objects.create(
            task=self.task_two_votes,
            chosen_candidate=candidate2,
            presented_candidate_ids=[candidate2.id],
            session_key="other-session-789",
        )

    def test_zero_vote_tasks_preferred(self):
        """Test that tasks with zero votes are selected first."""
        url = reverse("chooser-next-appeal")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["task_id"], self.task_no_votes.id)

    def test_newer_case_version_served_before_fewer_votes(self):
        """A case from the newest pipeline (summaries and an interview) is
        served first, even over an older case nobody has voted on yet."""
        ChooserTask.objects.filter(pk=self.task_two_votes.pk).update(case_version=2)

        response = self.client.get(reverse("chooser-next-appeal"))

        self.assertEqual(response.json()["task_id"], self.task_two_votes.id)


class ChooserVoteAPITest(APITestCase):
    """Test the POST /api/chooser/vote endpoint."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.task = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
            context_json={"procedure": "Test"},
        )
        self.candidate1 = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-a",
            content="Content A",
        )
        self.candidate2 = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=1,
            kind="appeal_letter",
            model_name="model-b",
            content="Content B",
        )

    def test_submit_vote(self):
        """Test submitting a valid vote."""
        url = reverse("chooser-vote")
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(self.candidate1.id),
            "presented_candidate_ids": [
                candidate_token(self.candidate1.id),
                candidate_token(self.candidate2.id),
            ],
        }
        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["success"])
        self.assertIn("vote_id", response.json())

        # Verify vote was created
        self.assertEqual(ChooserVote.objects.filter(task=self.task).count(), 1)

    def test_vote_with_the_served_tokens(self):
        """The whole blind round trip: the tokens the next-task endpoint
        served come back on the vote and resolve to the stored ids."""
        served = self.client.get(reverse("chooser-next-appeal")).json()["candidates"]
        by_id = {resolve_candidate(c["id"]): c["id"] for c in served}
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": by_id[self.candidate1.id],
            "presented_candidate_ids": [c["id"] for c in served],
            "flagged_candidate_ids": [by_id[self.candidate2.id]],
        }
        response = self.client.post(
            reverse("chooser-vote"), json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        vote = ChooserVote.objects.get(task=self.task)
        self.assertEqual(vote.chosen_candidate_id, self.candidate1.id)
        self.assertEqual(
            set(vote.presented_candidate_ids), {self.candidate1.id, self.candidate2.id}
        )
        self.assertEqual(vote.flagged_candidate_ids, [self.candidate2.id])

    def test_vote_with_a_bad_token_is_rejected(self):
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": "not-a-token",
            "presented_candidate_ids": [
                candidate_token(self.candidate1.id),
                candidate_token(self.candidate2.id),
            ],
        }
        response = self.client.post(
            reverse("chooser-vote"), json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid candidate", response.json()["error"])
        self.assertEqual(ChooserVote.objects.count(), 0)

    def test_vote_does_not_take_plain_ids(self):
        """A plain id is refused like any bad token: accepting it would let
        a client learn a token's id by guessing, since the endpoint answers
        differently for an id inside the task and one outside it."""
        for guess in (self.candidate1.id, str(self.candidate1.id), 99999):
            data = {
                "task_id": self.task.id,
                "chosen_candidate_id": candidate_token(self.candidate1.id),
                "presented_candidate_ids": [guess, candidate_token(self.candidate2.id)],
            }
            response = self.client.post(
                reverse("chooser-vote"),
                json.dumps(data),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn("Invalid candidate", response.json()["error"])
        self.assertEqual(ChooserVote.objects.count(), 0)

    def test_a_vote_with_one_presented_candidate_is_rejected(self):
        """The page offers no vote when a task has one draft left, so a
        single-candidate ballot did not come from the page."""
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(self.candidate1.id),
            "presented_candidate_ids": [candidate_token(self.candidate1.id)],
        }
        response = self.client.post(
            reverse("chooser-vote"), json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("at least two", response.json()["error"])
        self.assertEqual(ChooserVote.objects.count(), 0)

    def test_vote_records_fabrication_flags(self):
        """A vote may flag presented drafts as making things up; the flags
        are stored with the vote, deduplicated."""
        url = reverse("chooser-vote")
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(self.candidate1.id),
            "presented_candidate_ids": [
                candidate_token(self.candidate1.id),
                candidate_token(self.candidate2.id),
            ],
            "flagged_candidate_ids": [
                candidate_token(self.candidate2.id),
                candidate_token(self.candidate2.id),
            ],
        }
        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        vote = ChooserVote.objects.get(task=self.task)
        self.assertEqual(vote.flagged_candidate_ids, [self.candidate2.id])

    def test_vote_without_flags_stores_an_empty_list(self):
        url = reverse("chooser-vote")
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(self.candidate1.id),
            "presented_candidate_ids": [
                candidate_token(self.candidate1.id),
                candidate_token(self.candidate2.id),
            ],
        }
        self.client.post(url, json.dumps(data), content_type="application/json")

        self.assertEqual(
            ChooserVote.objects.get(task=self.task).flagged_candidate_ids, []
        )

    def test_vote_cannot_flag_the_chosen_candidate(self):
        """A foul cannot also be the winner."""
        url = reverse("chooser-vote")
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(self.candidate1.id),
            "presented_candidate_ids": [
                candidate_token(self.candidate1.id),
                candidate_token(self.candidate2.id),
            ],
            "flagged_candidate_ids": [candidate_token(self.candidate1.id)],
        }
        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("flagged", response.json()["error"])
        self.assertEqual(ChooserVote.objects.count(), 0)

    def test_vote_cannot_flag_a_candidate_that_was_not_presented(self):
        """Flags count against a model on the public rankings, so a flag on
        a draft the voter never saw (another task's, or one out of rotation)
        is rejected like a presented id would be."""
        other_task = ChooserTask.objects.create(task_type="appeal", status="READY")
        elsewhere = ChooserCandidate.objects.create(
            task=other_task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-c",
            content="Content C",
        )
        url = reverse("chooser-vote")
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(self.candidate1.id),
            "presented_candidate_ids": [
                candidate_token(self.candidate1.id),
                candidate_token(self.candidate2.id),
            ],
            "flagged_candidate_ids": [candidate_token(elsewhere.id)],
        }
        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("presented", response.json()["error"])
        self.assertEqual(ChooserVote.objects.count(), 0)

    def test_vote_invalid_task(self):
        """Test voting on a non-existent task."""
        url = reverse("chooser-vote")
        data = {
            "task_id": 99999,
            "chosen_candidate_id": candidate_token(self.candidate1.id),
            "presented_candidate_ids": [candidate_token(self.candidate1.id)],
        }
        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_vote_invalid_candidate(self):
        """Test voting for an invalid candidate."""
        url = reverse("chooser-vote")
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(99999),
            "presented_candidate_ids": [candidate_token(99999)],
        }
        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_vote_prevented(self):
        """Test that duplicate votes from same session are prevented."""
        url = reverse("chooser-vote")
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(self.candidate1.id),
            "presented_candidate_ids": [
                candidate_token(self.candidate1.id),
                candidate_token(self.candidate2.id),
            ],
        }

        # First vote should succeed
        response1 = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )
        self.assertEqual(response1.status_code, status.HTTP_200_OK)

        # Second vote should fail
        response2 = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )
        self.assertEqual(response2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already voted", response2.json()["error"])

    def test_vote_candidate_not_in_presented(self):
        """Test that chosen candidate must be in presented_candidate_ids."""
        url = reverse("chooser-vote")
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(self.candidate1.id),
            "presented_candidate_ids": [
                candidate_token(self.candidate2.id)
            ],  # candidate1 not included
        }
        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not in the presented", response.json()["error"])

    def test_vote_when_logged_in_as_admin(self):
        """Test that voting works when logged in as an admin user."""
        # Create an admin user
        admin_user = User.objects.create_superuser(
            username="admin",
            email="admin@test.com",
            password="adminpass123",
        )

        # Log in as admin
        self.client.force_authenticate(user=admin_user)

        url = reverse("chooser-vote")
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(self.candidate1.id),
            "presented_candidate_ids": [
                candidate_token(self.candidate1.id),
                candidate_token(self.candidate2.id),
            ],
        }
        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        # This should succeed (200 OK) but currently fails
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["success"])
        self.assertIn("vote_id", response.json())

        # Verify vote was created
        self.assertEqual(ChooserVote.objects.filter(task=self.task).count(), 1)


class ChooserTieVoteAPITest(APITestCase):
    """Votes that call a tie between two or more drafts."""

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
        # The display order the page reported: C, A, B.
        self.presented = [candidate_token(c.id) for c in (self.c, self.a, self.b)]

    def _vote(self, chosen, tied, flagged=()):
        return self.client.post(
            reverse("chooser-vote"),
            json.dumps(
                {
                    "task_id": self.task.id,
                    "chosen_candidate_id": candidate_token(chosen.id),
                    "presented_candidate_ids": self.presented,
                    "tied_candidate_ids": [candidate_token(c.id) for c in tied],
                    "flagged_candidate_ids": [candidate_token(c.id) for c in flagged],
                }
            ),
            content_type="application/json",
        )

    def test_a_tie_is_stored_in_display_order(self):
        response = self._vote(self.a, [self.a, self.c], flagged=[self.b])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        vote = ChooserVote.objects.get(task=self.task)
        self.assertEqual(vote.tied_candidate_ids, [self.c.id, self.a.id])

    def test_a_tie_is_stood_for_by_its_first_draft_shown(self):
        """The client named A; C was shown first, so C stands for the tie."""
        self._vote(self.a, [self.a, self.c])

        vote = ChooserVote.objects.get(task=self.task)
        self.assertEqual(vote.chosen_candidate_id, self.c.id)

    def test_a_vote_with_one_winner_stores_no_tie(self):
        self._vote(self.a, [])

        self.assertEqual(ChooserVote.objects.get(task=self.task).tied_candidate_ids, [])

    def test_a_tie_of_one_is_rejected(self):
        response = self._vote(self.a, [self.a])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("at least two", response.json()["error"])
        self.assertEqual(ChooserVote.objects.count(), 0)

    def test_a_flagged_draft_cannot_be_tied_for_best(self):
        response = self._vote(self.a, [self.a, self.b], flagged=[self.b])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("flagged", response.json()["error"])
        self.assertEqual(ChooserVote.objects.count(), 0)

    def test_the_chosen_draft_must_be_in_the_tie(self):
        response = self._vote(self.a, [self.b, self.c])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("tied", response.json()["error"])
        self.assertEqual(ChooserVote.objects.count(), 0)

    def test_a_tie_with_a_draft_not_shown_is_rejected(self):
        self.presented = [candidate_token(c.id) for c in (self.a, self.b)]

        response = self._vote(self.a, [self.a, self.c])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("presented", response.json()["error"])
        self.assertEqual(ChooserVote.objects.count(), 0)


class ChooserChatHistoryTest(APITestCase):
    """Test chat tasks with conversation history."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Create a chat task with history
        self.task = ChooserTask.objects.create(
            task_type="chat",
            status="READY",
            context_json={
                "prompt": "What documents do I need for my appeal?",
                "history": [
                    {"role": "user", "content": "My insurance denied my MRI request."},
                    {
                        "role": "assistant",
                        "content": "I'm sorry to hear that. Can you tell me more about the denial reason?",
                    },
                ],
            },
        )
        self.candidate1 = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=0,
            kind="chat_response",
            model_name="model-a",
            content="You'll need your denial letter and medical records.",
            metadata={"source": "synthetic", "has_history": True},
        )
        self.candidate2 = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=1,
            kind="chat_response",
            model_name="model-b",
            content="The key documents include your EOB and doctor's notes.",
            metadata={"source": "synthetic", "has_history": True},
        )

    def test_get_chat_task_with_history(self):
        """Test getting a chat task that includes conversation history."""
        url = reverse("chooser-next-chat")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        self.assertEqual(data["task_id"], self.task.id)
        self.assertEqual(data["task_type"], "chat")
        self.assertIn("history", data["task_context"])
        self.assertEqual(len(data["task_context"]["history"]), 2)
        self.assertEqual(data["task_context"]["history"][0]["role"], "user")
        self.assertEqual(
            data["task_context"]["prompt"], "What documents do I need for my appeal?"
        )


class ChooserSessionFallbackTest(APITestCase):
    """Test session fallback key persistence."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.task = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
            context_json={"procedure": "Test"},
        )
        self.candidate1 = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-a",
            content="Content A",
        )
        self.candidate2 = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=1,
            kind="appeal_letter",
            model_name="model-b",
            content="Content B",
        )

    def test_fallback_session_key_persists(self):
        """Test that fallback session key is persisted and reused."""
        # Submit a vote which will create a session key
        url = reverse("chooser-vote")
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(self.candidate1.id),
            "presented_candidate_ids": [
                candidate_token(self.candidate1.id),
                candidate_token(self.candidate2.id),
            ],
        }
        response1 = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )
        self.assertEqual(response1.status_code, status.HTTP_200_OK)

        # Try to vote again - should fail because session key is persisted
        response2 = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )
        self.assertEqual(response2.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already voted", response2.json()["error"])


class ChooserJudgeIdentityTest(APITestCase):
    """A case a judge has already judged is not served to them again: not in
    the same session, not to the same signed-in user from another session,
    and, when the request has no session cookie, not to the same address."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]
    ADDRESS = "203.0.113.7"

    def setUp(self):
        self.task = ChooserTask.objects.create(
            task_type="appeal", status="READY", context_json={"procedure": "Test"}
        )
        self.candidate1 = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-a",
            content="Content A",
        )
        self.candidate2 = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=1,
            kind="appeal_letter",
            model_name="model-b",
            content="Content B",
        )
        self.user = User.objects.create_user(
            username="judge", email="judge@example.com", password="pw-judge-123"
        )
        # Keep the prefill a used-up pool sets off from generating anything.
        patcher = patch(
            "fighthealthinsurance.chooser_tasks.trigger_prefill_async",
            return_value=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _vote_elsewhere(self, **voter):
        ChooserVote.objects.create(
            task=self.task,
            chosen_candidate=self.candidate1,
            presented_candidate_ids=[self.candidate1.id, self.candidate2.id],
            session_key=voter.pop("session_key", "another-session"),
            **voter,
        )

    def _next(self, **extra):
        return self.client.get(reverse("chooser-next-appeal"), **extra)

    def _vote_data(self):
        return json.dumps(
            {
                "task_id": self.task.id,
                "chosen_candidate_id": candidate_token(self.candidate1.id),
                "presented_candidate_ids": [
                    candidate_token(self.candidate1.id),
                    candidate_token(self.candidate2.id),
                ],
            }
        )

    def test_a_signed_in_user_is_not_served_a_case_they_judged_in_another_session(
        self,
    ):
        self._vote_elsewhere(user=self.user)
        self.client.force_login(self.user)
        self.assertEqual(self._next().status_code, status.HTTP_202_ACCEPTED)

    def test_a_signed_in_user_is_not_served_a_case_they_skipped_in_another_session(
        self,
    ):
        ChooserSkip.objects.create(
            task=self.task, session_key="another-session", user=self.user
        )
        self.client.force_login(self.user)
        self.assertEqual(self._next().status_code, status.HTTP_202_ACCEPTED)

    def test_another_users_vote_does_not_hide_the_case(self):
        other = User.objects.create_user(
            username="other", email="other@example.com", password="pw-other-123"
        )
        self._vote_elsewhere(user=other)
        self.client.force_login(self.user)
        self.assertEqual(self._next().status_code, status.HTTP_200_OK)

    def test_a_signed_in_user_cannot_vote_twice_from_another_session(self):
        self._vote_elsewhere(user=self.user)
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("chooser-vote"),
            self._vote_data(),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_vote_records_the_signed_in_user(self):
        self.client.force_login(self.user)
        self.client.post(
            reverse("chooser-vote"),
            self._vote_data(),
            content_type="application/json",
        )
        self.assertEqual(ChooserVote.objects.get(task=self.task).user, self.user)

    def test_a_vote_records_a_digest_of_the_address_not_the_address(self):
        self.client.post(
            reverse("chooser-vote"),
            self._vote_data(),
            content_type="application/json",
            HTTP_CF_CONNECTING_IP=self.ADDRESS,
        )
        vote = ChooserVote.objects.get(task=self.task)
        self.assertNotIn(self.ADDRESS, vote.client_key)
        self.assertRegex(vote.client_key, r"^[0-9a-f]{64}$")

    def test_a_request_without_a_session_is_not_served_a_case_judged_from_its_address(
        self,
    ):
        from fighthealthinsurance.rest_views import ChooserViewSet

        self._vote_elsewhere(client_key=ChooserViewSet.client_key_for(self.ADDRESS))
        response = self._next(HTTP_CF_CONNECTING_IP=self.ADDRESS)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_a_request_without_a_session_is_served_a_case_judged_elsewhere(self):
        from fighthealthinsurance.rest_views import ChooserViewSet

        self._vote_elsewhere(client_key=ChooserViewSet.client_key_for("198.51.100.9"))
        response = self._next(HTTP_CF_CONNECTING_IP=self.ADDRESS)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_session_is_not_held_to_other_sessions_at_its_address(self):
        from fighthealthinsurance.rest_views import ChooserViewSet

        # The first request sets the session cookie; after that the session,
        # not the shared address, says what this judge has seen.
        self._next(HTTP_CF_CONNECTING_IP=self.ADDRESS)
        self._vote_elsewhere(client_key=ChooserViewSet.client_key_for(self.ADDRESS))
        response = self._next(HTTP_CF_CONNECTING_IP=self.ADDRESS)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


    def test_a_session_keeps_hiding_what_its_address_judged_before_it_began(self):
        from fighthealthinsurance.rest_views import ChooserViewSet

        self._vote_elsewhere(client_key=ChooserViewSet.client_key_for(self.ADDRESS))
        self._next(HTTP_CF_CONNECTING_IP=self.ADDRESS)
        # The second request carries the new session's cookie.
        response = self._next(HTTP_CF_CONNECTING_IP=self.ADDRESS)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_a_stale_session_cookie_counts_as_no_session(self):
        from django.conf import settings
        from fighthealthinsurance.rest_views import ChooserViewSet

        self.client.cookies[settings.SESSION_COOKIE_NAME] = "expired-session-key"
        self._vote_elsewhere(client_key=ChooserViewSet.client_key_for(self.ADDRESS))
        response = self._next(HTTP_CF_CONNECTING_IP=self.ADDRESS)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_a_skip_without_a_csrf_token_does_not_count_for_the_user(self):
        from rest_framework.test import APIClient

        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.user)
        client.post(
            reverse("chooser-skip"),
            json.dumps({"task_id": self.task.id}),
            content_type="application/json",
        )
        self.assertIsNone(ChooserSkip.objects.get(task=self.task).user)

    def test_a_user_skipping_in_two_sessions_leaves_one_skip(self):
        ChooserSkip.objects.create(
            task=self.task, session_key="another-session", user=self.user
        )
        self.client.force_login(self.user)
        self.client.post(
            reverse("chooser-skip"),
            json.dumps({"task_id": self.task.id}),
            content_type="application/json",
        )
        self.assertEqual(ChooserSkip.objects.filter(task=self.task).count(), 1)

    def test_a_vote_with_a_valid_csrf_token_records_the_signed_in_user(self):
        from django.conf import settings
        from django.middleware.csrf import _get_new_csrf_string
        from rest_framework.test import APIClient

        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.user)
        token = _get_new_csrf_string()
        client.cookies[settings.CSRF_COOKIE_NAME] = token
        client.post(
            reverse("chooser-vote"),
            self._vote_data(),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(ChooserVote.objects.get(task=self.task).user, self.user)

    def test_the_database_holds_one_skip_per_user_per_task(self):
        from django.db import IntegrityError, transaction

        ChooserSkip.objects.create(
            task=self.task, session_key="another-session", user=self.user
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            ChooserSkip.objects.create(
                task=self.task, session_key="a-third-session", user=self.user
            )

    def test_the_database_holds_one_vote_per_user_per_task(self):
        from django.db import IntegrityError, transaction

        self._vote_elsewhere(user=self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._vote_elsewhere(user=self.user, session_key="a-third-session")

class ChooserPrefillTest(APITestCase):
    """Test the prefill functionality."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def test_trigger_prefill_async_does_not_raise(self):
        """Test that trigger_prefill_async can be called without raising."""
        from fighthealthinsurance.chooser_tasks import trigger_prefill_async

        # Should not raise any exceptions
        try:
            trigger_prefill_async()
        except Exception as e:
            self.fail(f"trigger_prefill_async raised exception: {e}")

    def test_chooser_view_triggers_prefill(self):
        """Test that loading the chooser page triggers prefill."""
        with patch(
            "fighthealthinsurance.chooser_tasks.trigger_prefill_async"
        ) as mock_prefill:
            url = reverse("chooser")
            response = self.client.get(url)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            mock_prefill.assert_called_once()


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "chooser-prefill-throttle-test",
        }
    }
)
class ChooserPrefillThrottleTest(SimpleTestCase):
    """Page loads and empty next-task fetches share the prefill throttle, but
    a fetch from a session that used a type up must not be swallowed by a
    page load's prefill a moment before: that prefill found the pool stocked
    and made nothing."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        # No background prefill thread: only the throttle is under test.
        thread = patch("fighthealthinsurance.chooser_tasks.threading.Thread")
        thread.start()
        self.addCleanup(thread.stop)

    def test_a_page_load_prefill_does_not_hold_off_a_used_up_type(self):
        from fighthealthinsurance.chooser_tasks import trigger_prefill_async

        trigger_prefill_async()
        self.assertTrue(trigger_prefill_async(exhausted="appeal"))

    def test_a_used_up_type_is_still_throttled(self):
        from fighthealthinsurance.chooser_tasks import trigger_prefill_async

        trigger_prefill_async(exhausted="appeal")
        self.assertFalse(trigger_prefill_async(exhausted="appeal"))


class ChooserSkipModelTest(APITestCase):
    """Test ChooserSkip model."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def test_create_skip(self):
        """Test creating a ChooserSkip."""
        task = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
        )
        skip = ChooserSkip.objects.create(
            task=task,
            session_key="test-session-123",
        )
        self.assertEqual(skip.task, task)
        self.assertEqual(skip.session_key, "test-session-123")

    def test_skip_unique_per_session(self):
        """Test that a session can only skip once per task."""
        task = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
        )
        ChooserSkip.objects.create(
            task=task,
            session_key="test-session-123",
        )
        # Creating another skip with the same session key should fail
        with self.assertRaises(Exception):
            ChooserSkip.objects.create(
                task=task,
                session_key="test-session-123",
            )


class ChooserSkipAPITest(APITestCase):
    """Test the POST /api/chooser/skip endpoint."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        """
        Create a READY "appeal" ChooserTask and two associated appeal_letter candidates.

        Sets up a ChooserTask with context_json {"procedure": "Test"} and two ChooserCandidate records:
        candidate_index 0 and 1 with different model_name and content values.
        """
        self.task = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
            context_json={"procedure": "Test"},
        )
        self.candidate1 = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-a",
            content="Content A",
        )
        self.candidate2 = ChooserCandidate.objects.create(
            task=self.task,
            candidate_index=1,
            kind="appeal_letter",
            model_name="model-b",
            content="Content B",
        )

    def test_submit_skip(self):
        """Test skipping a task."""
        url = reverse("chooser-skip")
        data = {"task_id": self.task.id}
        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response.json()["message"], "Task skipped successfully")

        # Verify skip was created
        self.assertEqual(ChooserSkip.objects.filter(task=self.task).count(), 1)

    def test_skip_invalid_task(self):
        """Test skipping a non-existent task."""
        url = reverse("chooser-skip")
        data = {"task_id": 99999}
        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_duplicate_skip_allowed(self):
        """Test that duplicate skips from same session are handled gracefully."""
        url = reverse("chooser-skip")
        data = {"task_id": self.task.id}

        # First skip should succeed
        response1 = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )
        self.assertEqual(response1.status_code, status.HTTP_200_OK)

        # Second skip should also succeed (get_or_create handles duplicates)
        response2 = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )
        self.assertEqual(response2.status_code, status.HTTP_200_OK)

        # Only one skip should exist (get_or_create doesn't create duplicates)
        self.assertEqual(ChooserSkip.objects.filter(task=self.task).count(), 1)

    def test_skip_excludes_task_from_next(self):
        """Test that skipped tasks are excluded from next task queries."""
        # Create a second task
        task2 = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
            context_json={"procedure": "Test 2"},
        )
        ChooserCandidate.objects.create(
            task=task2,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-a",
            content="Content A2",
        )

        # Skip the first task
        skip_url = reverse("chooser-skip")
        skip_data = {"task_id": self.task.id}
        self.client.post(
            skip_url, json.dumps(skip_data), content_type="application/json"
        )

        # Mock the task generation to prevent on-demand generation
        with patch(
            "fighthealthinsurance.chooser_tasks._generate_single_task"
        ) as mock_generate:
            mock_generate.return_value = None

            # Get next task - should return task2, not the skipped task
            next_url = reverse("chooser-next-appeal")
            response = self.client.get(next_url)

            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.json()["task_id"], task2.id)


class ChooserTaskContextTest(APITestCase):
    """Test task context for different task types."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def test_appeal_task_context_fields(self):
        """Test appeal task has expected context fields."""
        task = ChooserTask.objects.create(
            task_type="appeal",
            status="READY",
            context_json={
                "procedure": "Lumbar MRI",
                "diagnosis": "Chronic lower back pain",
                "insurance_company": "Test Insurance Co",
                "denial_text_preview": "Not medically necessary",
            },
        )
        ChooserCandidate.objects.create(
            task=task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-a",
            content="Appeal content",
        )

        url = reverse("chooser-next-appeal")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        context = response.json()["task_context"]

        self.assertEqual(context["procedure"], "Lumbar MRI")
        self.assertEqual(context["diagnosis"], "Chronic lower back pain")
        self.assertEqual(context["insurance_company"], "Test Insurance Co")
        self.assertEqual(context["denial_text_preview"], "Not medically necessary")

    def test_chat_task_context_fields(self):
        """Test chat task has expected context fields including history."""
        task = ChooserTask.objects.create(
            task_type="chat",
            status="READY",
            context_json={
                "prompt": "How do I appeal a prior auth denial?",
                "history": [
                    {"role": "user", "content": "My prior auth was denied"},
                    {"role": "assistant", "content": "I can help with that."},
                ],
            },
        )
        ChooserCandidate.objects.create(
            task=task,
            candidate_index=0,
            kind="chat_response",
            model_name="model-a",
            content="Chat response content",
        )

        url = reverse("chooser-next-chat")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        context = response.json()["task_context"]

        self.assertEqual(context["prompt"], "How do I appeal a prior auth denial?")
        self.assertIn("history", context)
        self.assertEqual(len(context["history"]), 2)


# Research judging on: its key and its switch. Stored judgments are shown
# only while it is on (the switch is their kill switch too).
RESEARCH_ON = dict(TYPESAFE_API_KEY="k", TYPESAFE_RESEARCH_JUDGING_ENABLED=True)
# A judgment our own model made for the arena where Jev gave none.
OWN_SCORER = f"fhi/model-local/research-rubric-{research_judging.RUBRIC_VERSION}"


class ChooserEvidenceVerdictTest(APITestCase):
    """Jev's stored verdict on each article rides along with an appeal case
    as it is served (task_context.evidence[].judgment), a referee aid read
    at serve time and never written to the case."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.context = {
            "procedure": "MRI",
            "diagnosis": "Lower back pain",
            "evidence": [
                {"pmid": "1", "title": "For", "url": "https://pubmed.ncbi.nlm.nih.gov/1/"},
                {"pmid": "2", "title": "Against", "summary": "It did not help."},
                {"pmid": "3", "title": "Unjudged"},
            ],
        }
        self.task = ChooserTask.objects.create(
            task_type="appeal", status="READY", context_json=self.context
        )
        for index, name in enumerate(("model-a", "model-b")):
            ChooserCandidate.objects.create(
                task=self.task,
                candidate_index=index,
                kind="appeal_letter",
                model_name=name,
                content=f"Draft {index} cites PMID 1.",
            )
        self.key = research_judging.treatment_key("MRI", "Lower back pain")
        self._judge("1", self.key, on_topic=0.9, supports=0.85, undermines=0.05)
        self._judge("2", self.key, on_topic=0.9, supports=0.1, undermines=0.8)

    def _judge(self, pmid, key, scorer=research_judging.SCORER, **probabilities):
        PubMedArticleJudgment.objects.create(
            pmid=pmid, treatment_key=key, scorer=scorer, **probabilities
        )

    def _served_evidence(self):
        response = self.client.get(reverse("chooser-next-appeal"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()["task_context"]["evidence"]

    def test_each_judged_article_carries_its_verdict(self):
        with override_settings(**RESEARCH_ON):
            evidence = self._served_evidence()
        self.assertEqual(
            evidence[0]["judgment"],
            {"verdict": "supports", "on_topic": 0.9, "supports": 0.85, "undermines": 0.05},
        )
        self.assertEqual(evidence[1]["judgment"]["verdict"], "against")
        # The rest of the article is served as stored.
        self.assertEqual(evidence[1]["summary"], "It did not help.")
        self.assertNotIn("judgment", evidence[2])

    def test_the_stored_case_is_not_changed(self):
        with override_settings(**RESEARCH_ON):
            self._served_evidence()
        self.task.refresh_from_db()
        self.assertEqual(self.task.context_json, self.context)

    def test_switched_off_no_verdict_is_shown(self):
        evidence = self._served_evidence()
        self.assertFalse(any("judgment" in item for item in evidence))

    def test_a_judgment_for_another_pair_is_not_shown(self):
        other = research_judging.treatment_key("MRI", "Neck pain")
        self._judge("3", other, on_topic=0.9, supports=0.9, undermines=0.0)
        with override_settings(**RESEARCH_ON):
            evidence = self._served_evidence()
        self.assertNotIn("judgment", evidence[2])

    def test_a_judgment_under_another_rubric_is_not_shown(self):
        self._judge(
            "3",
            self.key,
            scorer="typesafe/jev-1.13.0/research-rubric-0",
            on_topic=0.9,
            supports=0.9,
            undermines=0.0,
        )
        with override_settings(**RESEARCH_ON):
            evidence = self._served_evidence()
        self.assertNotIn("judgment", evidence[2])

    def test_our_own_models_verdict_is_shown_while_the_arena_judges(self):
        """Where Jev gave none, our own model's verdict (made by the arena's
        fallback, chooser_tasks._judge_evidence) is shown, with research
        judging off; the arena's switch is its kill switch."""
        self._judge("3", self.key, scorer=OWN_SCORER, on_topic=0.9, supports=0.9, undermines=0.0)
        with override_settings(CHOOSER_JUDGE_EVIDENCE=True):
            evidence = self._served_evidence()
        self.assertEqual(evidence[2]["judgment"]["verdict"], "supports")
        # Jev's stay hidden while research judging is off.
        self.assertNotIn("judgment", evidence[0])
        with override_settings(CHOOSER_JUDGE_EVIDENCE=False):
            evidence = self._served_evidence()
        self.assertNotIn("judgment", evidence[2])

    def test_jevs_verdict_wins_over_our_own_models(self):
        self._judge("1", self.key, scorer=OWN_SCORER, on_topic=0.05, supports=0.0, undermines=0.0)
        with override_settings(CHOOSER_JUDGE_EVIDENCE=True, **RESEARCH_ON):
            evidence = self._served_evidence()
        self.assertEqual(evidence[0]["judgment"]["verdict"], "supports")

    def test_a_case_without_evidence_is_served_as_it_is(self):
        context = {"procedure": "MRI", "diagnosis": "Lower back pain", "evidence": []}
        with override_settings(**RESEARCH_ON):
            self.assertIs(annotate_evidence_verdicts(context), context)

    def test_annotation_leaves_the_given_context_alone(self):
        context = json.loads(json.dumps(self.context))
        with override_settings(**RESEARCH_ON):
            annotated = annotate_evidence_verdicts(context)
        self.assertEqual(context, self.context)
        self.assertIn("judgment", annotated["evidence"][0])


class ChooserVoteIntegrityTest(APITestCase):
    """Presented ids are the usage dashboard's denominator, so they must be
    this task's active candidates."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.task = ChooserTask.objects.create(
            task_type="appeal", status="READY", context_json={"procedure": "MRI"}
        )
        self.mine = [
            ChooserCandidate.objects.create(
                task=self.task,
                candidate_index=i,
                kind="appeal_letter",
                model_name=f"model-{i}",
                content=f"Candidate {i} content long enough to matter.",
            )
            for i in range(3)
        ]
        other_task = ChooserTask.objects.create(
            task_type="appeal", status="READY", context_json={"procedure": "CT"}
        )
        self.foreign = ChooserCandidate.objects.create(
            task=other_task,
            candidate_index=0,
            kind="appeal_letter",
            model_name="model-elsewhere",
            content="A candidate that belongs to another task.",
        )

    def test_presented_ids_from_another_task_are_rejected(self):
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(self.mine[0].id),
            "presented_candidate_ids": [
                candidate_token(self.mine[0].id),
                candidate_token(self.foreign.id),
            ],
        }
        response = self.client.post(
            reverse("chooser-vote"), json.dumps(data), content_type="application/json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("belong", response.json()["error"])
        self.assertEqual(ChooserVote.objects.count(), 0)

    def test_a_presented_candidate_out_of_rotation_is_rejected(self):
        """The next-task endpoint serves active candidates only, so an
        inactive one in the report was never on anyone's screen."""
        retired = self.mine[2]
        retired.is_active = False
        retired.save()
        data = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(self.mine[0].id),
            "presented_candidate_ids": [
                candidate_token(self.mine[0].id),
                candidate_token(retired.id),
            ],
        }
        response = self.client.post(
            reverse("chooser-vote"), json.dumps(data), content_type="application/json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(ChooserVote.objects.count(), 0)


CAPTCHA_ON = dict(
    RECAPTCHA_TESTING=False,
    RECAPTCHA_PUBLIC_KEY="test-public-key",
    RECAPTCHA_PRIVATE_KEY="test-private-key",
)


def _google_says(is_valid, error_codes=()):
    """What django-recaptcha's client returns for a tick."""
    return MagicMock(is_valid=is_valid, error_codes=list(error_codes))


class ChooserCaptchaGateTest(APITestCase):
    """The "I'm not a robot" check at the start of the Arena: on the same
    terms as the site's other forms (both keys set, testing off), the first
    next-task request carries the tick's token, a good one marks the
    session, and a vote or skip needs that mark too."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]
    CAPTCHA_HEADER = "HTTP_X_RECAPTCHA_RESPONSE"

    def setUp(self):
        self.task = ChooserTask.objects.create(
            task_type="appeal", status="READY", context_json={"procedure": "PT"}
        )
        for i, name in enumerate(("model-a", "model-b")):
            ChooserCandidate.objects.create(
                task=self.task,
                candidate_index=i,
                kind="appeal_letter",
                model_name=name,
                content=f"Draft {name}",
            )
        self.next_url = reverse("chooser-next-appeal")
        self.skip_url = reverse("chooser-skip")

    def test_off_in_testing_mode_no_tick_is_asked_for(self):
        """RECAPTCHA_TESTING is on in the test settings, as in dev."""
        response = self.client.get(self.next_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_settings(**CAPTCHA_ON)
    def test_without_a_tick_the_arena_asks_for_one(self):
        with patch("fighthealthinsurance.forms.recaptcha_client.submit") as submit:
            response = self.client.get(self.next_url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["error"], "captcha_required")
        self.assertEqual(response.json()["code"], "required")
        submit.assert_not_called()

    @override_settings(**CAPTCHA_ON)
    def test_a_good_tick_opens_the_session_once(self):
        with patch(
            "fighthealthinsurance.forms.recaptcha_client.submit",
            return_value=_google_says(True),
        ) as submit:
            first = self.client.get(self.next_url, **{self.CAPTCHA_HEADER: "tick-1"})
            # The next round carries no token: the session remembers.
            second = self.client.get(self.next_url)

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        submit.assert_called_once()
        self.assertEqual(submit.call_args.kwargs["recaptcha_response"], "tick-1")
        self.assertTrue(self.client.session["chooser_captcha_passed"])

    @override_settings(**CAPTCHA_ON)
    def test_a_refused_tick_is_told_apart_from_an_expired_one(self):
        with patch(
            "fighthealthinsurance.forms.recaptcha_client.submit",
            return_value=_google_says(False, ["invalid-input-response"]),
        ):
            refused = self.client.get(self.next_url, **{self.CAPTCHA_HEADER: "bad"})
        with patch(
            "fighthealthinsurance.forms.recaptcha_client.submit",
            return_value=_google_says(False, ["timeout-or-duplicate"]),
        ):
            expired = self.client.get(self.next_url, **{self.CAPTCHA_HEADER: "old"})

        self.assertEqual(refused.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(refused.json()["code"], "captcha_invalid")
        self.assertEqual(expired.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(expired.json()["code"], "captcha_expired")
        self.assertIn("Tick it again", expired.json()["message"])
        self.assertNotIn("chooser_captcha_passed", self.client.session)

    @override_settings(**CAPTCHA_ON)
    def test_google_out_of_reach_is_a_retry_not_a_refusal(self):
        with patch(
            "fighthealthinsurance.forms.recaptcha_client.submit",
            side_effect=OSError("no route"),
        ):
            response = self.client.get(self.next_url, **{self.CAPTCHA_HEADER: "tick"})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["code"], "captcha_error")

    @override_settings(**CAPTCHA_ON)
    def test_a_skip_or_vote_needs_the_session_mark_too(self):
        body = {"task_id": self.task.id}
        before = self.client.post(self.skip_url, body, format="json")
        self.assertFalse(ChooserSkip.objects.filter(task=self.task).exists())
        with patch(
            "fighthealthinsurance.forms.recaptcha_client.submit",
            return_value=_google_says(True),
        ):
            self.client.get(self.next_url, **{self.CAPTCHA_HEADER: "tick"})
        after = self.client.post(self.skip_url, body, format="json")

        self.assertEqual(before.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(before.json()["error"], "captcha_required")
        self.assertEqual(after.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.client.post(reverse("chooser-vote"), {}, format="json").status_code,
            status.HTTP_400_BAD_REQUEST,
            "a vote with a bad body is a bad request even once the mark is set",
        )

    @override_settings(**CAPTCHA_ON)
    def test_a_well_formed_vote_after_the_mark_is_stored(self):
        a, b = ChooserCandidate.objects.filter(task=self.task).order_by("candidate_index")
        body = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(a.id),
            "presented_candidate_ids": [candidate_token(a.id), candidate_token(b.id)],
        }
        with patch(
            "fighthealthinsurance.forms.recaptcha_client.submit",
            return_value=_google_says(True),
        ):
            self.client.get(self.next_url, **{self.CAPTCHA_HEADER: "tick"})
        response = self.client.post(reverse("chooser-vote"), body, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(ChooserVote.objects.filter(task=self.task).count(), 1)

    @override_settings(
        **CAPTCHA_ON,
        # The test settings use DummyCache, which would count nothing.
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "chooser-captcha-cap",
            }
        },
    )
    def test_too_many_checks_from_one_address_are_told_to_wait(self):
        """Each token is a call to Google; the cap stops a flood of junk."""
        from django.core.cache import cache

        from fighthealthinsurance.rest_views import ChooserViewSet

        cache.clear()
        with patch(
            "fighthealthinsurance.forms.recaptcha_client.submit",
            return_value=_google_says(False, ["invalid-input-response"]),
        ) as submit, patch.object(ChooserViewSet, "CAPTCHA_MAX_CHECKS", 2):
            answers = [
                self.client.get(
                    self.next_url,
                    **{self.CAPTCHA_HEADER: f"junk-{i}"},
                    # The address Cloudflare saw; a forged X-Forwarded-For
                    # does not move the count.
                    HTTP_CF_CONNECTING_IP="203.0.113.9",
                    HTTP_X_FORWARDED_FOR=f"198.51.100.{i}",
                )
                for i in range(3)
            ]

        self.assertEqual([r.status_code for r in answers], [403, 403, 403])
        self.assertEqual(
            [r.json()["code"] for r in answers],
            ["captcha_invalid", "captcha_invalid", "captcha_rate_limited"],
        )
        self.assertEqual(submit.call_count, 2, "the third token never reached Google")

    @override_settings(**CAPTCHA_ON)
    def test_a_well_formed_vote_without_the_mark_is_refused_and_not_stored(self):
        """The gate sits after the body check, so a vote that would count
        is still turned away, and nothing is written."""
        a, b = ChooserCandidate.objects.filter(task=self.task).order_by("candidate_index")
        body = {
            "task_id": self.task.id,
            "chosen_candidate_id": candidate_token(a.id),
            "presented_candidate_ids": [candidate_token(a.id), candidate_token(b.id)],
        }
        with patch("fighthealthinsurance.forms.recaptcha_client.submit") as submit:
            response = self.client.post(reverse("chooser-vote"), body, format="json")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.json()["error"], "captcha_required")
        self.assertEqual(ChooserVote.objects.filter(task=self.task).count(), 0)
        submit.assert_not_called()


class _ArenaOrderingBase(APITestCase):
    """Arena tasks of a given complexity, evidence and case version, votes
    by this client's session and by others, and the next task served."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    _EVIDENCE = [{"pmid": "1", "title": "A study", "url": "https://example.org/1"}]

    def _task(
        self, complexity, evidence=True, case_version=2, task_type="appeal", context=...
    ):
        if context is ...:
            context = {"procedure": "MRI", "diagnosis": "Low back pain"}
            if evidence:
                context["evidence"] = list(self._EVIDENCE)
            if task_type == "chat":
                context = {"prompt": "What now?", "history": []}
        task = ChooserTask.objects.create(
            task_type=task_type,
            status="READY",
            context_json=context,
            case_version=case_version,
            complexity=complexity,
        )
        for index in range(2):
            ChooserCandidate.objects.create(
                task=task,
                candidate_index=index,
                kind="appeal_letter" if task_type == "appeal" else "chat_response",
                model_name=f"model-{index}",
                content=f"Draft {index}",
            )
        return task

    def _session_key(self):
        """The client's session key, the one the endpoint will see; reading
        the session creates and stores one when the client has none yet."""
        return self.client.session.session_key

    def _vote(self, task, session_key):
        candidate = task.candidates.first()
        ChooserVote.objects.create(
            task=task,
            chosen_candidate=candidate,
            presented_candidate_ids=[c.id for c in task.candidates.all()],
            session_key=session_key,
        )

    def _vote_as_this_session(self, task):
        """A vote by the client's own session on ``task``: one round judged."""
        self._vote(task, self._session_key())

    def _votes_by_others(self, task, count):
        for index in range(count):
            self._vote(task, f"other-session-{index}")

    def _next(self, task_type="appeal", difficulty=None):
        url = reverse(f"chooser-next-{task_type}")
        params = {"difficulty": difficulty} if difficulty is not None else {}
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()["task_id"]

class ChooserWarmupOrderingTest(_ArenaOrderingBase):
    """A new judge is started on the simplest cases that have evidence, and
    the complexity of what they are served rises with their votes
    (chooser_complexity.ordered_for_judge). An unscored task is served as
    the hardest and stays unscored."""

    def test_a_new_judge_gets_the_simplest_case_that_has_evidence(self):
        self._task(complexity=1, evidence=False)
        self._task(complexity=3)
        simplest_with_evidence = self._task(complexity=2)

        self.assertEqual(self._next(), simplest_with_evidence.id)

    def test_every_stored_shape_of_no_evidence_waits(self):
        # What production writes when a case has no evidence: an empty list
        # (_evidence_for found nothing), a null context (a task that never
        # got a scenario), a null list, and no key at all.
        for context in ({"evidence": []}, None, {"evidence": None}, {}):
            self._task(complexity=1, context=context)
        with_evidence = self._task(complexity=5)

        self.assertEqual(self._next(), with_evidence.id)

    def test_an_unscored_case_is_served_as_the_hardest_and_stays_unscored(self):
        unscored = self._task(complexity=None)
        scored = self._task(complexity=4)

        self.assertEqual(self._next(), scored.id)
        unscored.refresh_from_db()
        self.assertIsNone(unscored.complexity)

    def test_the_newest_pipeline_still_comes_first_while_warming_up(self):
        # The ramp runs within the newest case version; an older version's
        # simpler case stays behind it, as the usual order has it.
        newest = self._task(complexity=4, case_version=2)
        self._task(complexity=2, case_version=1)

        self.assertEqual(self._next(), newest.id)

    def test_a_much_judged_simple_case_yields_to_a_less_judged_harder_one(self):
        judged_enough = self._task(complexity=1)
        self._votes_by_others(judged_enough, 5)
        harder = self._task(complexity=3)

        self.assertEqual(self._next(), harder.id)

    @override_settings(CHOOSER_WARMUP_SATURATION_VOTES=6)
    def test_the_saturation_point_is_a_setting(self):
        judged_some = self._task(complexity=1)
        self._votes_by_others(judged_some, 5)
        self._task(complexity=3)

        self.assertEqual(self._next(), judged_some.id)

    @override_settings(CHOOSER_WARMUP_ROUNDS_PER_LEVEL=1)
    def test_the_level_rises_with_the_judges_votes(self):
        # The judged case is hard and evidence-less, so it never competes.
        judged = self._task(complexity=5, evidence=False)
        level_two = self._task(complexity=2)
        level_one = self._task(complexity=1)

        # Nothing judged yet: the level is 1, and only level_one is at it.
        self.assertEqual(self._next(), level_one.id)

        # One round judged: level 2, where both are within reach and the
        # usual order (both version 2, no votes) serves the older one.
        self._vote_as_this_session(judged)
        self.assertEqual(self._next(), level_two.id)

    @override_settings(CHOOSER_WARMUP_ROUNDS_PER_LEVEL=1)
    def test_skips_are_not_rounds(self):
        skipped = self._task(complexity=1)
        self._task(complexity=2)
        level_one = self._task(complexity=1)
        ChooserSkip.objects.create(task=skipped, session_key=self._session_key())

        self.assertEqual(self._next(), level_one.id)

    @override_settings(CHOOSER_WARMUP_ROUNDS_PER_LEVEL=1)
    def test_votes_on_the_other_type_are_not_rounds(self):
        chat = self._task(complexity=1, task_type="chat")
        self._task(complexity=2)
        level_one = self._task(complexity=1)
        self._vote_as_this_session(chat)

        self.assertEqual(self._next(), level_one.id)

    @override_settings(CHOOSER_WARMUP_ROUNDS_PER_LEVEL=1)
    def test_a_warmed_up_judge_gets_the_usual_order(self):
        judged = [self._task(complexity=1) for _ in range(4)]
        self._task(complexity=1, evidence=True, case_version=1)
        newest_hardest = self._task(complexity=5, evidence=False, case_version=2)
        # Judged often, which only the warm-up holds against a case.
        self._votes_by_others(newest_hardest, 5)
        for task in judged:
            self._vote_as_this_session(task)

        # No evidence, much judged and the hardest: the warm-up would serve
        # the older simple case first; the usual order serves the newest.
        self.assertEqual(self._next(), newest_hardest.id)

    @override_settings(CHOOSER_WARMUP_ROUNDS_PER_LEVEL=0)
    def test_the_warm_up_can_be_turned_off(self):
        self._task(complexity=1, case_version=1)
        newest = self._task(complexity=5, evidence=False, case_version=2)

        self.assertEqual(self._next(), newest.id)

    def test_chat_cases_are_served_simplest_first_too(self):
        self._task(complexity=3, task_type="chat")
        simplest = self._task(complexity=1, task_type="chat")

        self.assertEqual(self._next("chat"), simplest.id)

    def test_a_case_without_evidence_is_still_served_when_nothing_else_is_left(self):
        only = self._task(complexity=5, evidence=False)

        self.assertEqual(self._next(), only.id)


class ChooserCasePreferenceTest(_ArenaOrderingBase):
    """After their first round a judge is asked whether they want complex or
    straightforward cases; the page sends the answer as the ``difficulty``
    parameter (chooser_complexity.ordered_for_judge)."""

    def test_complex_serves_the_most_complex_case_first(self):
        self._task(complexity=1)
        hardest = self._task(complexity=4)
        self._task(complexity=3)

        self.assertEqual(self._next(difficulty="complex"), hardest.id)

    def test_complex_counts_an_unscored_case_as_the_most_complex(self):
        self._task(complexity=4)
        unscored = self._task(complexity=None)

        self.assertEqual(self._next(difficulty="complex"), unscored.id)

    def test_complex_leaves_an_unscored_case_unscored(self):
        unscored = self._task(complexity=None)

        self._next(difficulty="complex")

        unscored.refresh_from_db()
        self.assertIsNone(unscored.complexity)

    def test_complex_cases_still_come_with_evidence_first(self):
        self._task(complexity=5, evidence=False)
        with_evidence = self._task(complexity=2)

        self.assertEqual(self._next(difficulty="complex"), with_evidence.id)

    def test_complex_still_serves_the_newest_pipeline_first(self):
        self._task(complexity=5, case_version=1)
        newest = self._task(complexity=2, case_version=2)

        self.assertEqual(self._next(difficulty="complex"), newest.id)

    def test_complex_spreads_votes_past_a_much_judged_case(self):
        judged_enough = self._task(complexity=5)
        self._votes_by_others(judged_enough, 5)
        less_judged = self._task(complexity=3)

        self.assertEqual(self._next(difficulty="complex"), less_judged.id)

    @override_settings(CHOOSER_WARMUP_ROUNDS_PER_LEVEL=1)
    def test_straightforward_holds_after_the_warm_up(self):
        # Four rounds judged: warmed up, so the usual order would serve the
        # older, harder case.
        for _ in range(4):
            self._vote_as_this_session(self._task(complexity=5, evidence=False))
        self._task(complexity=3)
        simplest = self._task(complexity=1)

        self.assertEqual(self._next(difficulty="straightforward"), simplest.id)

    @override_settings(CHOOSER_WARMUP_ROUNDS_PER_LEVEL=0)
    def test_straightforward_applies_with_the_warm_up_off(self):
        self._task(complexity=3)
        simplest = self._task(complexity=1)

        self.assertEqual(self._next(difficulty="straightforward"), simplest.id)

    def test_straightforward_serves_the_simplest_left_when_none_is_a_one(self):
        self._task(complexity=4)
        simplest_left = self._task(complexity=2)

        self.assertEqual(self._next(difficulty="straightforward"), simplest_left.id)

    @override_settings(CHOOSER_WARMUP_ROUNDS_PER_LEVEL=1)
    def test_an_unknown_difficulty_keeps_the_usual_order(self):
        # Warmed up, so with no preference the usual order serves the
        # oldest; straightforward would serve the 1, complex the 5.
        for _ in range(4):
            self._vote_as_this_session(self._task(complexity=5, evidence=False))
        oldest = self._task(complexity=3)
        self._task(complexity=1)
        self._task(complexity=5)

        self.assertEqual(self._next(difficulty="brutal"), oldest.id)

    def test_chat_cases_follow_the_preference_too(self):
        self._task(complexity=1, task_type="chat")
        hardest = self._task(complexity=4, task_type="chat")

        self.assertEqual(self._next("chat", difficulty="complex"), hardest.id)

"""Test the Chooser (Best-Of Selection) API functionality"""

import json

from django.urls import reverse
from django.contrib.auth import get_user_model

from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance.models import (
    ChooserTask,
    ChooserCandidate,
    ChooserVote,
    UserDomain,
    Denial,
)
from fhi_users.models import ProfessionalUser, ExtraUserProperties

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
        self.assertEqual(data["context"]["procedure"], "Physical Therapy")

    def test_get_next_task_no_tasks_available(self):
        """Test getting next task when none are available."""
        # Delete all tasks
        ChooserTask.objects.all().delete()

        url = reverse("chooser-next-appeal")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("No tasks available", response.json()["message"])

    def test_session_exclusion(self):
        """Test that tasks already voted on are excluded."""
        # Create a vote for this task
        ChooserVote.objects.create(
            task=self.task,
            chosen_candidate=self.candidate1,
            presented_candidate_ids=[self.candidate1.id, self.candidate2.id],
            session_key=self.client.session.session_key or "test-session",
        )

        # Since we only have one task, we should get 404
        url = reverse("chooser-next-appeal")
        # Force session creation
        self.client.session.create()
        session_key = self.client.session.session_key

        # Create another vote with this session
        ChooserVote.objects.filter(session_key="test-session").update(
            session_key=session_key
        )

        response = self.client.get(url)
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
            "chosen_candidate_id": self.candidate1.id,
            "presented_candidate_ids": [self.candidate1.id, self.candidate2.id],
        }
        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["success"])
        self.assertIn("vote_id", response.json())

        # Verify vote was created
        self.assertEqual(ChooserVote.objects.filter(task=self.task).count(), 1)

    def test_vote_invalid_task(self):
        """Test voting on a non-existent task."""
        url = reverse("chooser-vote")
        data = {
            "task_id": 99999,
            "chosen_candidate_id": self.candidate1.id,
            "presented_candidate_ids": [self.candidate1.id],
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
            "chosen_candidate_id": 99999,
            "presented_candidate_ids": [99999],
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
            "chosen_candidate_id": self.candidate1.id,
            "presented_candidate_ids": [self.candidate1.id, self.candidate2.id],
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
            "chosen_candidate_id": self.candidate1.id,
            "presented_candidate_ids": [self.candidate2.id],  # candidate1 not included
        }
        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not in the presented", response.json()["error"])

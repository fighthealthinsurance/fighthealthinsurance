"""Test the Chooser (Best-Of Selection) API functionality"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from django.contrib.auth import get_user_model
from django.urls import reverse

from rest_framework import status
from rest_framework.test import APITestCase

from fhi_users.models import ExtraUserProperties, ProfessionalUser, UserDomain
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    ChooserVote,
    Denial,
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
        """Test getting next task when none are available and generation fails."""
        # Delete all tasks
        ChooserTask.objects.all().delete()

        # Mock the task generation to simulate failure (no models available)
        with patch(
            "fighthealthinsurance.chooser_tasks._generate_single_task"
        ) as mock_generate:
            # Make generation not create any tasks
            mock_generate.return_value = None

            url = reverse("chooser-next-appeal")
            response = self.client.get(url)

            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
            self.assertIn("No tasks available", response.json()["message"])

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

        # Mock the task generation to prevent on-demand generation
        with patch(
            "fighthealthinsurance.chooser_tasks._generate_single_task"
        ) as mock_generate:
            mock_generate.return_value = None

            # Since we only have one task and already voted, should get 404
            url = reverse("chooser-next-appeal")
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
        self.assertIn("history", data["context"])
        self.assertEqual(len(data["context"]["history"]), 2)
        self.assertEqual(data["context"]["history"][0]["role"], "user")
        self.assertEqual(
            data["context"]["prompt"], "What documents do I need for my appeal?"
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
            "chosen_candidate_id": self.candidate1.id,
            "presented_candidate_ids": [self.candidate1.id, self.candidate2.id],
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
        context = response.json()["context"]

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
        context = response.json()["context"]

        self.assertEqual(context["prompt"], "How do I appeal a prior auth denial?")
        self.assertIn("history", context)
        self.assertEqual(len(context["history"]), 2)

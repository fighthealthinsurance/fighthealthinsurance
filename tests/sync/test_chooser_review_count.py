from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance.models import ChooserCandidate, ChooserTask, ChooserVote


class ChooserReviewCountPageTest(TestCase):
    def _page(self):
        with mock.patch("fighthealthinsurance.chooser_tasks.trigger_prefill_async"):
            response = self.client.get(reverse("chooser"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_anonymous_page_has_no_account_review_count(self):
        self.assertNotIn("data-review-count", self._page())

    def test_signed_in_page_carries_the_account_review_count(self):
        user = get_user_model().objects.create_user(
            username="page-judge", email="page-judge@example.com", password="pw-page-123"
        )
        for i, all_bad in enumerate((False, True)):
            task = ChooserTask.objects.create(task_type="appeal", status="READY")
            candidate = ChooserCandidate.objects.create(
                task=task,
                candidate_index=0,
                kind="appeal_letter",
                model_name="model-a",
                content="Draft",
            )
            ChooserVote.objects.create(
                task=task,
                chosen_candidate=None if all_bad else candidate,
                presented_candidate_ids=[candidate.id],
                all_bad=all_bad,
                session_key=f"old-session-{i}",
                user=user,
            )
        self.client.force_login(user)

        self.assertIn('data-review-count="2"', self._page())

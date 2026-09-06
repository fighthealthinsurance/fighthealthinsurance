"""Re-POSTing the scrub form reuses the denial the session already started.

The scrub form renders its next step directly instead of redirecting, so a
reload (or a back-then-resubmit, or a double click) re-POSTs it. Every such
POST used to INSERT a brand new Denial -- and with it a brand new multi-day
intake journey, a second speculative-appeal precompute, and a second set of
follow-up emails -- because the form carries no denial id and the session's
``denial_uuid`` was written but never read back.

These tests pin the reuse rule: the session's denial is updated in place, but
only for the same person, only while their journey is unfinished, and only
inside the recency window. Every other case must still create a new denial.
"""

import datetime

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.models import Denial, IntakeJourneyEvent
from fighthealthinsurance.views import DENIAL_SESSION_REUSE_WINDOW


class DenialSessionReuseTest(TestCase):
    """One browser session mid-intake == one Denial row."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    EMAIL = "reuse@example.com"
    OTHER_EMAIL = "someone-else@example.com"

    def setUp(self):
        self.client = Client()

    def _post(self, email=None, denial_text="Your claim has been denied."):
        """Submit the consumer scrub form on the current session."""
        return self.client.post(
            reverse("process"),
            {
                "email": email or self.EMAIL,
                "denial_text": denial_text,
                "pii": "on",
                "tos": "on",
                "privacy": "on",
            },
            follow=True,
        )

    def _first_submission(self) -> Denial:
        """The submission every test starts from, and the row it created."""
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Denial.objects.count(), 1)
        return Denial.objects.get()

    def test_resubmitting_the_form_does_not_create_a_second_denial(self):
        first = self._first_submission()

        self._post()

        self.assertEqual(Denial.objects.count(), 1)
        self.assertEqual(Denial.objects.get().denial_id, first.denial_id)

    def test_a_reused_denial_keeps_its_uuid_so_the_journey_id_collides(self):
        """The stable uuid is the whole point: ``intake-{uuid}`` only collides
        (and Temporal's REJECT_DUPLICATE only fires) if it does not change."""
        first = self._first_submission()

        self._post()

        reused = Denial.objects.get()
        self.assertEqual(reused.uuid, first.uuid)
        self.assertEqual(self.client.session["denial_uuid"], str(first.uuid))

    def test_resubmitting_updates_the_reused_denial_in_place(self):
        first = self._first_submission()

        self._post(denial_text="Your claim has been denied. Corrected text.")

        reused = Denial.objects.get()
        self.assertEqual(reused.denial_id, first.denial_id)
        self.assertEqual(
            reused.denial_text, "Your claim has been denied. Corrected text."
        )

    def test_a_different_email_never_reuses_the_session_denial(self):
        """A stale or shared session must not hand one person's denial to
        somebody else, so a mismatched email always starts a new row."""
        first = self._first_submission()

        self._post(email=self.OTHER_EMAIL)

        self.assertEqual(Denial.objects.count(), 2)
        newest = Denial.objects.exclude(denial_id=first.denial_id).get()
        self.assertEqual(newest.hashed_email, Denial.get_hashed_email(self.OTHER_EMAIL))

    def test_a_completed_journey_starts_a_new_denial(self):
        """Once the intake journey finished, the next submission is a new case."""
        first = self._first_submission()
        IntakeJourneyEvent.objects.create(
            denial=first, event_type=IntakeJourneyEvent.FORM_COMPLETED
        )

        self._post()

        self.assertEqual(Denial.objects.count(), 2)

    def test_an_unfinished_journey_still_reuses_the_denial(self):
        """Only FORM_COMPLETED blocks reuse -- the intake_started intent that
        every denial gets must not."""
        first = self._first_submission()
        IntakeJourneyEvent.objects.create(
            denial=first, event_type=IntakeJourneyEvent.INTAKE_STARTED
        )

        self._post()

        self.assertEqual(Denial.objects.count(), 1)
        self.assertEqual(Denial.objects.get().denial_id, first.denial_id)

    def test_a_denial_older_than_the_window_is_not_reused(self):
        """Coming back to the same session much later is a different denial."""
        first = self._first_submission()
        Denial.objects.filter(denial_id=first.denial_id).update(
            created=timezone.now()
            - DENIAL_SESSION_REUSE_WINDOW
            - datetime.timedelta(minutes=1)
        )

        self._post()

        self.assertEqual(Denial.objects.count(), 2)

    def test_a_denial_inside_the_window_is_still_reused(self):
        """The recency bound is a window, not an instant."""
        first = self._first_submission()
        Denial.objects.filter(denial_id=first.denial_id).update(
            created=timezone.now()
            - DENIAL_SESSION_REUSE_WINDOW
            + datetime.timedelta(minutes=5)
        )

        self._post()

        self.assertEqual(Denial.objects.count(), 1)
        self.assertEqual(Denial.objects.get().denial_id, first.denial_id)

    def test_a_denial_with_no_creation_timestamp_is_not_reused(self):
        """``created`` is nullable on rows predating the column; a row whose
        age cannot be established is not treatable as recent."""
        first = self._first_submission()
        Denial.objects.filter(denial_id=first.denial_id).update(created=None)

        self._post()

        self.assertEqual(Denial.objects.count(), 2)

    def test_a_session_without_a_denial_uuid_creates_a_new_denial(self):
        """The unchanged path: no session state, no reuse."""
        self._first_submission()

        fresh_browser = Client()
        fresh_browser.post(
            reverse("process"),
            {
                "email": self.EMAIL,
                "denial_text": "Your claim has been denied.",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
            },
            follow=True,
        )

        self.assertEqual(Denial.objects.count(), 2)

"""The health history page asks whether the history may go in the letter.

It never did. The column that governs it defaulted to False, no box was ever
rendered, and the drafting path ignored it, so the history went in either
way. These tests pin the question being asked, the answer being saved, and a
person's existing history not quietly changing meaning when they say nothing.
"""

from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance.denial_context import health_history_digest
from fighthealthinsurance.models import Denial

EMAIL = "consent@example.com"
SEMI_SEKRET = "sekret"
HISTORY = "Migraines since 2019, on Aimovig"


class TheHealthHistoryPageAsksTest(TestCase):
    def setUp(self):
        self.denial = Denial.objects.create(
            denial_id=7301,
            semi_sekret=SEMI_SEKRET,
            hashed_email=Denial.get_hashed_email(EMAIL),
            denial_text="Denied an MRI.",
            health_history=HISTORY,
        )

    def _ref(self):
        return {
            "denial_id": str(self.denial.denial_id),
            "email": EMAIL,
            "semi_sekret": SEMI_SEKRET,
        }

    def _post(self, **extra):
        payload = self._ref()
        payload["health_history"] = HISTORY
        payload["health_history_seen"] = health_history_digest(
            HISTORY, self.denial.denial_id
        )
        payload.update(extra)
        return self.client.post(reverse("hh"), payload)

    def _answer(self):
        self.denial.refresh_from_db()
        return self.denial.include_provided_health_history_in_appeal

    def test_the_box_is_on_the_page(self):
        response = self.client.get(reverse("hh"), self._ref())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "include_provided_health_history_in_appeal")
        self.assertContains(response, "Use this in my appeal letter")

    def test_the_box_starts_ticked(self):
        """What the site does with a history today, offered as a choice."""
        response = self.client.get(reverse("hh"), self._ref())

        body = response.content.decode()
        box = body[body.index("include_provided_health_history_in_appeal") :][:400]
        self.assertIn("checked", box)

    def test_the_page_says_where_the_words_go(self):
        response = self.client.get(reverse("hh"), self._ref())

        self.assertContains(response, "given to the AI that writes your letter")

    def test_unticking_is_saved_as_an_answer(self):
        """An unticked box is absent from the POST, and that is the answer."""
        self.assertTrue(self._answer())

        self._post()

        self.assertFalse(self._answer())

    def test_ticking_is_saved(self):
        self.denial.include_provided_health_history_in_appeal = False
        self.denial.save(update_fields=["include_provided_health_history_in_appeal"])

        self._post(include_provided_health_history_in_appeal="on")

        self.assertTrue(self._answer())

    def test_the_answer_survives_a_visit_that_changes_nothing_else(self):
        self._post(include_provided_health_history_in_appeal="on")
        self.denial.refresh_from_db()

        response = self.client.get(reverse("hh"), self._ref())

        body = response.content.decode()
        box = body[body.index("include_provided_health_history_in_appeal") :][:400]
        self.assertIn("checked", box)

    def test_the_history_itself_is_still_kept_when_the_answer_is_no(self):
        """Saying no leaves it out of the letter; it does not delete it."""
        self._post()

        self.denial.refresh_from_db()
        self.assertEqual(self.denial.health_history, HISTORY)
        self.assertFalse(self._answer())


class TheOtherFlagIsStillNotAskedAboutTest(TestCase):
    """health_history_anonymized has no box, so a Next must not answer it.

    It defaults to True and nothing in the codebase reads it, which is its own
    problem, but this page must not start deciding it by omission.
    """

    def test_a_submission_does_not_change_it(self):
        denial = Denial.objects.create(
            denial_id=7302,
            semi_sekret=SEMI_SEKRET,
            hashed_email=Denial.get_hashed_email(EMAIL),
            denial_text="Denied an MRI.",
            health_history=HISTORY,
        )
        before = denial.health_history_anonymized

        self.client.post(
            reverse("hh"),
            {
                "denial_id": str(denial.denial_id),
                "email": EMAIL,
                "semi_sekret": SEMI_SEKRET,
                "health_history": HISTORY,
                "health_history_seen": health_history_digest(HISTORY, denial.denial_id),
            },
        )

        denial.refresh_from_db()
        self.assertEqual(denial.health_history_anonymized, before)

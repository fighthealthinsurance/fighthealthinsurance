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
        return self.denial.health_history_consent

    def test_the_box_is_on_the_page(self):
        response = self.client.get(reverse("hh"), self._ref())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "health_history_consent")
        self.assertContains(response, "Use this in my appeal letter")

    def test_the_box_starts_ticked_while_nobody_has_answered(self):
        """A row nobody asked carries NULL, and the site uses the history,
        so the box shows what is actually happening."""
        response = self.client.get(reverse("hh"), self._ref())

        body = response.content.decode()
        box = body[body.index("health_history_consent") :][:400]
        self.assertIn("checked", box)

    def test_the_page_says_where_the_words_go(self):
        response = self.client.get(reverse("hh"), self._ref())

        self.assertContains(response, "given to the AI that writes your letter")

    def test_unticking_is_saved_as_an_answer(self):
        """An unticked box is absent from the POST, and that is the answer."""
        self.assertIsNone(self._answer(), "nobody has been asked yet")

        self._post()

        self.assertIs(self._answer(), False)

    def test_ticking_is_saved(self):
        self.denial.health_history_consent = False
        self.denial.save(update_fields=["health_history_consent"])

        self._post(health_history_consent="on")

        self.assertIs(self._answer(), True)

    def test_the_answer_survives_a_visit_that_changes_nothing_else(self):
        self._post(health_history_consent="on")
        self.denial.refresh_from_db()

        response = self.client.get(reverse("hh"), self._ref())

        body = response.content.decode()
        box = body[body.index("health_history_consent") :][:400]
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


class TheOtherColumnIsLeftAloneTest(TestCase):
    """include_provided_health_history_in_appeal is a different question.

    It decides whether the raw history is attached to the fax as its own
    document, which is a wider disclosure than using it to write the letter,
    and it is off unless a caller asks. The page never mentions it, so no
    submission here may change it in either direction.
    """

    def setUp(self):
        self.denial = Denial.objects.create(
            denial_id=7303,
            semi_sekret=SEMI_SEKRET,
            hashed_email=Denial.get_hashed_email(EMAIL),
            denial_text="Denied an MRI.",
            health_history=HISTORY,
        )

    def _post(self, **extra):
        payload = {
            "denial_id": str(self.denial.denial_id),
            "email": EMAIL,
            "semi_sekret": SEMI_SEKRET,
            "health_history": HISTORY,
            "health_history_seen": health_history_digest(
                HISTORY, self.denial.denial_id
            ),
        }
        payload.update(extra)
        return self.client.post(reverse("hh"), payload)

    def test_it_starts_off_and_a_submission_does_not_turn_it_on(self):
        self.assertFalse(self.denial.include_provided_health_history_in_appeal)

        self._post(health_history_consent="on")

        self.denial.refresh_from_db()
        self.assertFalse(self.denial.include_provided_health_history_in_appeal)

    def test_a_caller_who_set_it_keeps_it(self):
        """Set through the API on purpose; a Next here must not clear it."""
        Denial.objects.filter(denial_id=self.denial.denial_id).update(
            include_provided_health_history_in_appeal=True
        )

        self._post(health_history_consent="on")

        self.denial.refresh_from_db()
        self.assertTrue(self.denial.include_provided_health_history_in_appeal)

    def test_the_page_never_names_it(self):
        response = self.client.get(
            reverse("hh"),
            {
                "denial_id": str(self.denial.denial_id),
                "email": EMAIL,
                "semi_sekret": SEMI_SEKRET,
            },
        )

        self.assertNotContains(response, "include_provided_health_history_in_appeal")


class TheApiCannotRevokeByOmissionTest(TestCase):
    """An omitted BooleanField cleans to False, which is not an answer.

    The page renders a box, so there absence is a decision. An API caller
    who never mentions consent has decided nothing, and the update persists
    whatever it is handed, so the serializer has to drop what was not sent.
    """

    def test_the_serializer_drops_a_consent_it_was_not_given(self):
        from fighthealthinsurance.rest_serializers import (
            HealthHistoryFormSerializer,
        )

        self.assertIn(
            "health_history_consent",
            HealthHistoryFormSerializer.CALLER_MUST_ASK_FOR,
        )

    def test_a_submission_that_never_mentions_it_leaves_it_alone(self):
        denial = Denial.objects.create(
            denial_id=7306,
            semi_sekret=SEMI_SEKRET,
            hashed_email=Denial.get_hashed_email(EMAIL),
            denial_text="Denied an MRI.",
            health_history=HISTORY,
            health_history_consent=True,
        )

        from fighthealthinsurance.rest_serializers import (
            HealthHistoryFormSerializer,
        )

        serializer = HealthHistoryFormSerializer(
            data={
                "denial_id": str(denial.denial_id),
                "email": EMAIL,
                "semi_sekret": SEMI_SEKRET,
                "health_history": HISTORY,
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

        self.assertNotIn("health_history_consent", serializer.validated_data)


class WhatSayingNoDoesAndDoesNotDoTest(TestCase):
    """The answer governs what is written from here, and says so.

    It does not reach back into work already done. A draft written while the
    history was allowed can carry it, and so can the question and citation
    material cached on the row, and a model already running will finish and
    save. Retiring the rows that exist at the moment of the click looked
    like withdrawal and was not: it missed everything in flight and
    everything derived, while deleting drafts on cases that never had a
    history at all. Doing it properly needs each draft to record whether it
    used the history, so replay and synthesis can filter on that, and that
    is its own change.
    """

    def setUp(self):
        self.denial = Denial.objects.create(
            denial_id=7307,
            semi_sekret=SEMI_SEKRET,
            hashed_email=Denial.get_hashed_email(EMAIL),
            denial_text="Denied an MRI.",
            health_history=HISTORY,
        )

    def _post(self, **extra):
        payload = {
            "denial_id": str(self.denial.denial_id),
            "email": EMAIL,
            "semi_sekret": SEMI_SEKRET,
            "health_history": HISTORY,
            "health_history_seen": health_history_digest(
                HISTORY, self.denial.denial_id
            ),
        }
        payload.update(extra)
        return self.client.post(reverse("hh"), payload)

    def test_it_is_recorded(self):
        self._post()

        self.denial.refresh_from_db()
        self.assertIs(self.denial.health_history_consent, False)

    def test_the_history_itself_is_kept(self):
        """Saying no leaves it out of the letter; it does not delete it."""
        self._post()

        self.denial.refresh_from_db()
        self.assertEqual(self.denial.health_history, HISTORY)

    def test_nothing_already_written_is_deleted(self):
        """Stated as a limit, so nobody reads the checkbox as a recall."""
        from fighthealthinsurance.models import ProposedAppeal

        existing = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="Written while the history was allowed.",
            chosen=False,
        )

        self._post()

        self.assertTrue(ProposedAppeal.objects.filter(pk=existing.pk).exists())

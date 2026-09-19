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

    def test_an_api_update_that_omits_it_does_not_revoke_it(self):
        """The whole way through, not just the serializer's own dict.

        Asserting membership in the drop list passed even if nothing acted
        on that list. This runs a validated API payload with no consent in
        it through the update the REST view calls, and reads the column
        back.
        """
        from fighthealthinsurance import common_view_logic
        from fighthealthinsurance.rest_serializers import (
            HealthHistoryFormSerializer,
        )

        denial = Denial.objects.create(
            denial_id=7305,
            semi_sekret=SEMI_SEKRET,
            hashed_email=Denial.get_hashed_email(EMAIL),
            denial_text="Denied an MRI.",
            health_history=HISTORY,
            health_history_consent=True,
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

        common_view_logic.DenialCreatorHelper.update_denial(
            **serializer.validated_data
        )

        denial.refresh_from_db()
        self.assertIs(denial.health_history_consent, True)

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

    What it governs is what is written from here. The cached question and
    citation material is part of that: both are read back ahead of the
    consent check on the next run, and the citations go straight into the
    drafting prompt, so leaving them would take the history out and keep
    what was chosen because of it. They are dropped, and the next run
    recomputes them from the denial alone.

    It does not reach back into work already done. A draft written while the
    history was allowed can carry it, and a model already running will
    finish and save. Retiring the drafts that exist at the moment of the
    click looked like withdrawal and was not: it missed everything in
    flight, while deleting drafts on cases that never had a history at all.
    Doing it properly needs each draft to record whether it used the
    history, so replay and synthesis can filter on that, and that is its own
    change.
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

    def test_the_material_derived_from_it_is_dropped(self):
        """Cached model input chosen out of the history does not survive.

        Both caches are read back before the consent check, and the
        citations one is put straight into the next drafting prompt, so a
        refusal that left them in place would remove the history and keep
        the material that came from it.
        """
        Denial.objects.filter(pk=self.denial.pk).update(
            ml_citation_context=["Chosen because of the Aimovig history"],
            candidate_ml_citation_context=["Also chosen because of it"],
            generated_questions=[["How long on Aimovig?", ""]],
            generated_questions_for="abc123",
            candidate_generated_questions=[["And before that?", ""]],
        )

        self._post()

        self.denial.refresh_from_db()
        self.assertIsNone(self.denial.ml_citation_context)
        self.assertIsNone(self.denial.candidate_ml_citation_context)
        self.assertIsNone(self.denial.generated_questions)
        self.assertIsNone(self.denial.generated_questions_for)
        self.assertIsNone(self.denial.candidate_generated_questions)

    def test_clearing_the_box_and_refusing_in_one_submit_still_drops_it(self):
        """The page can do both at once, and that is the strongest refusal.

        The history is written before the consent is read, so by then the
        stored value is the empty box that was just saved, and the case
        looks like one that never had a history for anything to be derived
        from. The value from before the submit is what the question is
        about.
        """
        Denial.objects.filter(pk=self.denial.pk).update(
            ml_citation_context=["Chosen because of the Aimovig history"],
            generated_questions=[["How long on Aimovig?", ""]],
        )

        self.client.post(
            reverse("hh"),
            {
                "denial_id": str(self.denial.denial_id),
                "email": EMAIL,
                "semi_sekret": SEMI_SEKRET,
                "health_history": "",
                "health_history_seen": health_history_digest(
                    HISTORY, self.denial.denial_id
                ),
            },
        )

        self.denial.refresh_from_db()
        self.assertEqual(self.denial.health_history, "")
        self.assertIs(self.denial.health_history_consent, False)
        self.assertIsNone(self.denial.ml_citation_context)
        self.assertIsNone(self.denial.generated_questions)

    def test_a_cache_written_after_the_page_loaded_is_still_cleared(self):
        """The instance the request holds is not what has to be cleared.

        A run in flight can write its result between the row being read for
        this request and this request saving it. Clearing only the columns
        that look full on the copy in hand would leave that one behind, and
        it is the copy in hand that shows nothing.
        """
        from fighthealthinsurance import common_view_logic

        stale = Denial.objects.get(pk=self.denial.pk)
        self.assertIsNone(stale.ml_citation_context, "the copy in hand is empty")

        # The worker finishes here, after the request read the row.
        Denial.objects.filter(pk=self.denial.pk).update(
            ml_citation_context=["Written after this request read the row"],
            candidate_generated_questions=[["And this one too", ""]],
        )

        common_view_logic.DenialCreatorHelper._update_denial(
            stale, health_history_consent=False
        )

        self.denial.refresh_from_db()
        self.assertIsNone(self.denial.ml_citation_context)
        self.assertIsNone(self.denial.candidate_generated_questions)

    def test_a_history_deleted_in_an_earlier_visit_is_still_covered(self):
        """Clearing the box and refusing can be two visits, not one.

        Asking whether this case has a history at the moment of the refusal
        answers no for somebody who deleted it last week, and their cached
        citations and questions were derived from it while it was there. So
        every refusal clears, and the cost of clearing a cache that owes
        nothing to a history is that the next run recomputes it.
        """
        emptied = Denial.objects.create(
            denial_id=7309,
            semi_sekret=SEMI_SEKRET,
            hashed_email=Denial.get_hashed_email(EMAIL),
            denial_text="Denied an MRI.",
            health_history="",
            ml_citation_context=["Chosen because of a history since deleted"],
            generated_questions=[["How long on Aimovig?", ""]],
        )

        self.client.post(
            reverse("hh"),
            {
                "denial_id": str(emptied.denial_id),
                "email": EMAIL,
                "semi_sekret": SEMI_SEKRET,
                "health_history": "",
                "health_history_seen": health_history_digest("", emptied.denial_id),
            },
        )

        emptied.refresh_from_db()
        self.assertIs(emptied.health_history_consent, False)
        self.assertIsNone(emptied.ml_citation_context)
        self.assertIsNone(emptied.generated_questions)

    def test_a_second_refusal_clears_what_arrived_after_the_first(self):
        """A run in flight can write a cache back after the refusal.

        If only the first no cleared anything, the way to get rid of what
        landed afterwards would be to say yes and then no again.
        """
        self._post()
        Denial.objects.filter(pk=self.denial.pk).update(
            ml_citation_context=["Written by a run that was already going"],
        )

        self._post()

        self.denial.refresh_from_db()
        self.assertIsNone(self.denial.ml_citation_context)

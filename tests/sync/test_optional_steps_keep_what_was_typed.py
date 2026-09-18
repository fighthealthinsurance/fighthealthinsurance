"""The two optional steps must not lose what the person already gave us.

health_history.html rendered a hardcoded empty textarea, and the box is posted
on every Next as a CharField(required=False) that cleans to "", so entering the
page and pressing Next wrote a blank over the stored history. The fix is on the
render side: every path into the page now shows what is stored.

The save side is deliberately NOT guarded against blanks. Once the box shows
what is stored, an empty box means the person emptied it, and emptying it has
to work: generate_appeal.py's make_appeals feeds the column into the model
prompt without checking include_provided_health_history_in_appeal (only the PDF
attachment path in create_or_update_appeal checks it), and no other page can
remove it.

The textarea assertions here read the server's HTML only. They say nothing
about what the browser then does: formPersistence.ts restores from localStorage
into an EMPTY textarea on a GET, which is exactly the state a removal leaves
behind. That half is covered in
tests/async-unit/test_health_history_persistence_respects_removal.py.

The two views are crossed relative to their names: PlanDocumentsView renders
health_history.html under the URL name "hh", and DenialCollectedView renders
plan_documents.html under "dvc". Each test below names the template it is
really checking.
"""

import datetime
import html as html_module
import re

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import common_view_logic, forms as core_forms, views
from fighthealthinsurance.models import (
    Denial,
    InsuranceCompany,
    InsurancePlan,
    PlanDocuments,
)
from fhi_users.audit import TrackingInfo
from fhi_users.models import PatientUser, ProfessionalUser

EMAIL = "history@example.com"
SEMI_SEKRET = "sekret-for-the-optional-steps"
STORED = "Type 2 diabetes since 2019 and a fibromyalgia diagnosis in 2021."
TYPED = "Also an MS diagnosis this spring, which I just remembered."


def textarea_value(html: str) -> str:
    """Whatever the health history textarea holds in a rendered response."""
    match = re.search(
        r'<textarea[^>]*id="health_history"[^>]*>(.*?)</textarea>',
        html,
        re.DOTALL,
    )
    assert match is not None, "no health_history textarea in the response"
    return html_module.unescape(match.group(1))


class OptionalStepsTestCase(TestCase):
    """A denial mid-flow, with a health history already stored on it."""

    def setUp(self):
        self.client = Client()
        self.denial = Denial.objects.create(
            denial_text="Your claim has been denied.",
            hashed_email=Denial.get_hashed_email(EMAIL),
            semi_sekret=SEMI_SEKRET,
            health_history=STORED,
        )

    def denial_ref(self) -> dict:
        return {
            "denial_id": self.denial.denial_id,
            "email": EMAIL,
            "semi_sekret": SEMI_SEKRET,
        }

    def stored_history(self) -> str:
        return Denial.objects.get(denial_id=self.denial.denial_id).health_history


class HealthHistoryRendersWhatIsStoredTest(OptionalStepsTestCase):
    """health_history.html, on both of the two ways into it."""

    def test_back_navigation_renders_the_stored_history(self):
        """Entry path one: a GET on "hh" (PlanDocumentsView), which is where
        the Back link from the plan documents step lands."""
        response = self.client.get(reverse("hh"), self.denial_ref())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            textarea_value(response.content.decode()).strip(),
            STORED,
        )

    def test_the_render_after_the_upload_step_shows_the_stored_history(self):
        """Entry path two: InitialProcessView.form_valid renders this page
        itself. A second submission of the upload form reuses the denial
        already in the session, so there is history to show."""
        first = self.client.post(
            reverse("process"),
            {
                "email": EMAIL,
                "denial_text": "Your claim has been denied.",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
            },
            follow=True,
        )
        self.assertEqual(first.status_code, 200)
        reused_id = self.client.session["denial_id"]
        self.assertNotEqual(reused_id, self.denial.denial_id)
        Denial.objects.filter(denial_id=reused_id).update(health_history=STORED)

        second = self.client.post(
            reverse("process"),
            {
                "email": EMAIL,
                "denial_text": "Your claim has been denied.",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
            },
            follow=True,
        )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(textarea_value(second.content.decode()).strip(), STORED)

    def test_a_rejected_submission_redisplays_what_the_person_typed(self):
        """A POST that fails validation re-renders this page carrying the
        person's own words. Precedence over the stored value is pinned
        separately, on the template, below."""
        response = self.client.post(
            reverse("hh"),
            {
                "denial_id": self.denial.denial_id,
                # No email: HealthHistory requires it, so the form is invalid.
                "semi_sekret": SEMI_SEKRET,
                "health_history": TYPED,
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertEqual(textarea_value(body).strip(), TYPED)

    def test_the_template_prefers_the_submitted_text_over_the_stored_one(self):
        """A bound form carrying both: initial holds the stored history, data
        holds what was typed, and the box must show the typed text."""
        form = core_forms.HealthHistory(
            data={
                "denial_id": self.denial.denial_id,
                "email": EMAIL,
                "semi_sekret": SEMI_SEKRET,
                "health_history": TYPED,
            },
            initial={"health_history": STORED},
        )

        html = render_to_string("health_history.html", {"form": form})

        self.assertEqual(textarea_value(html).strip(), TYPED)
        self.assertNotIn(STORED, html)


class PressingNextKeepsTheHistoryTest(OptionalStepsTestCase):
    """The bug this branch exists to fix, end to end through the pages."""

    def test_stepping_back_and_pressing_next_keeps_the_stored_history(self):
        """Step back, press Next without touching the box. On main this left
        the column empty."""
        page = self.client.get(reverse("hh"), self.denial_ref())
        self.assertEqual(page.status_code, 200)
        shown = textarea_value(page.content.decode())
        self.assertEqual(shown.strip(), STORED)

        payload = self.denial_ref()
        # Exactly what that page would submit, untouched.
        payload["health_history"] = shown

        response = self.client.post(reverse("hh"), payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored_history().strip(), STORED)

    def test_a_caller_that_does_not_send_the_field_leaves_it_alone(self):
        """The remaining guard, and the only one: a caller that omits
        health_history passes None and must change nothing. The plan-documents
        and entity-extract steps both reach update_denial that way."""
        common_view_logic.DenialCreatorHelper._update_denial(
            Denial.objects.get(denial_id=self.denial.denial_id),
            include_provided_health_history_in_appeal=True,
        )

        self.assertEqual(self.stored_history(), STORED)

    def test_typing_a_new_history_still_replaces_the_old_one(self):
        """A real edit still lands."""
        payload = self.denial_ref()
        payload["health_history"] = TYPED

        response = self.client.post(reverse("hh"), payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored_history(), TYPED)

    def test_a_blank_next_with_nothing_stored_still_writes_the_blank(self):
        """Nothing stored, empty box, Next: the column holds "" rather than
        staying unset."""
        empty = Denial.objects.create(
            denial_text="Your claim has been denied.",
            hashed_email=Denial.get_hashed_email(EMAIL),
            semi_sekret=SEMI_SEKRET,
        )

        response = self.client.post(
            reverse("hh"),
            {
                "denial_id": empty.denial_id,
                "email": EMAIL,
                "semi_sekret": SEMI_SEKRET,
                "health_history": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            Denial.objects.get(denial_id=empty.denial_id).health_history,
            "",
        )


class ClearingTheBoxRemovesTheHistoryTest(OptionalStepsTestCase):
    """A person must be able to take back what they wrote about their health.

    The column is fed to the appeal model with no gate (generate_appeal.py)
    and no other page can remove it, so clearing the box and pressing Next has
    to end with the column empty, and the page has to say so.
    """

    def test_clearing_the_box_and_pressing_next_removes_the_stored_history(self):
        """Starts from a stored value, ends with it gone."""
        self.assertEqual(self.stored_history(), STORED)
        payload = self.denial_ref()
        payload["health_history"] = ""

        response = self.client.post(reverse("hh"), payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored_history(), "")

    def test_the_removed_history_is_gone_from_the_page_too(self):
        """The server half of the removal. What the browser does with its own
        copy is pinned in the async-unit test named in the module docstring."""
        payload = self.denial_ref()
        payload["health_history"] = ""
        self.client.post(reverse("hh"), payload)

        response = self.client.get(reverse("hh"), self.denial_ref())

        body = response.content.decode()
        self.assertEqual(textarea_value(body).strip(), "")
        self.assertNotIn(STORED, body)

    def test_the_page_says_how_to_remove_it_when_there_is_something_there(self):
        """The copy has to match what the code does."""
        response = self.client.get(reverse("hh"), self.denial_ref())

        self.assertIn(
            "To remove it, clear the box and press Next",
            response.content.decode(),
        )

    def test_the_page_says_nothing_about_removing_an_empty_box(self):
        Denial.objects.filter(denial_id=self.denial.denial_id).update(health_history="")

        response = self.client.get(reverse("hh"), self.denial_ref())

        self.assertNotIn("To remove it", response.content.decode())


class PlanDocumentsPageCountsWhatIsThereTest(OptionalStepsTestCase):
    """plan_documents.html, on both of the two ways into it."""

    def setUp(self):
        super().setUp()
        PlanDocuments.objects.create(denial=self.denial)
        PlanDocuments.objects.create(denial=self.denial)

    def test_back_navigation_shows_the_count(self):
        """Entry path one: a GET on "dvc" (DenialCollectedView)."""
        response = self.client.get(reverse("dvc"), self.denial_ref())

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("2 plan documents already added", body)

    def test_the_render_after_the_history_step_shows_the_count(self):
        """Entry path two: PlanDocumentsView.form_valid renders this page."""
        payload = self.denial_ref()
        payload["health_history"] = STORED

        response = self.client.post(reverse("hh"), payload)

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("2 plan documents already added", body)

    def test_one_document_reads_as_one_document(self):
        PlanDocuments.objects.filter(denial=self.denial).first().delete()

        response = self.client.get(reverse("dvc"), self.denial_ref())

        body = response.content.decode()
        self.assertIn("1 plan document already added", body)
        self.assertNotIn("1 plan documents already added", body)

    def test_a_case_with_no_documents_says_nothing(self):
        PlanDocuments.objects.filter(denial=self.denial).delete()

        response = self.client.get(reverse("dvc"), self.denial_ref())

        self.assertNotIn("already added", response.content.decode())


class StepSavesWriteOnlyTheirOwnColumnsTest(OptionalStepsTestCase):
    """A column another writer set between the load and the save survives.

    Asserted by writing that column behind a loaded instance, never by
    inspecting an update_fields list: the list is the mechanism, this is the
    behaviour it exists for.
    """

    def test_the_optional_step_save_does_not_revert_another_writers_column(self):
        stale = Denial.objects.get(denial_id=self.denial.denial_id)
        Denial.objects.filter(denial_id=self.denial.denial_id).update(
            procedure="colonoscopy"
        )

        common_view_logic.DenialCreatorHelper._update_denial(
            stale, health_history=TYPED
        )

        fresh = Denial.objects.get(denial_id=self.denial.denial_id)
        self.assertEqual(fresh.procedure, "colonoscopy")
        self.assertEqual(fresh.health_history, TYPED)

    def test_a_resubmitted_upload_does_not_revert_another_writers_column(self):
        """create_or_update_denial's update branch, which the upload step runs
        again whenever someone goes back and resubmits their denial letter."""
        stale = Denial.objects.get(denial_id=self.denial.denial_id)
        Denial.objects.filter(denial_id=self.denial.denial_id).update(
            procedure="colonoscopy"
        )

        common_view_logic.DenialCreatorHelper.create_or_update_denial(
            email=EMAIL,
            denial_text=stale.denial_text,
            zip="",
            denial=stale,
        )

        fresh = Denial.objects.get(denial_id=self.denial.denial_id)
        self.assertEqual(fresh.procedure, "colonoscopy")

    def test_the_employer_name_save_does_not_revert_another_writers_column(self):
        """The second save in create_or_update_denial, the one that stores an
        employer name pulled out of the letter."""
        letter = "Group Name: Widgets, INC  Your claim has been denied."
        stale = Denial.objects.create(
            denial_text=letter,
            hashed_email=Denial.get_hashed_email(EMAIL),
            semi_sekret=SEMI_SEKRET,
        )
        stale = Denial.objects.get(denial_id=stale.denial_id)
        Denial.objects.filter(denial_id=stale.denial_id).update(procedure="colonoscopy")

        common_view_logic.DenialCreatorHelper.create_or_update_denial(
            email=EMAIL,
            denial_text=letter,
            zip="",
            denial=stale,
        )

        fresh = Denial.objects.get(denial_id=stale.denial_id)
        self.assertEqual(fresh.employer_name, "Widgets")
        self.assertEqual(fresh.procedure, "colonoscopy")


class StepSavesPersistTheColumnsTheyAssignTest(OptionalStepsTestCase):
    """The other direction, which the class above cannot see.

    A column left out of ``update_fields`` is written nowhere and raises
    nothing: the assignment stays in the source, the save succeeds, and the
    value is silently dropped. So each converted save gets a test that every
    column it assigns comes back, not a sample. The employer-name save is
    covered by
    test_the_employer_name_save_does_not_revert_another_writers_column above,
    which already asserts employer_name landed.
    """

    def test_the_optional_step_save_persists_every_column_it_assigns(self):
        # Set opposite to what the save below writes, so a dropped column is
        # visible. Stated here rather than taken from the model defaults,
        # which have changed once already.
        Denial.objects.filter(denial_id=self.denial.denial_id).update(
            health_history_anonymized=True,
            include_provided_health_history_in_appeal=False,
        )
        loaded = Denial.objects.get(denial_id=self.denial.denial_id)
        self.assertTrue(loaded.health_history_anonymized)
        self.assertFalse(loaded.include_provided_health_history_in_appeal)

        common_view_logic.DenialCreatorHelper._update_denial(
            loaded,
            health_history=TYPED,
            health_history_anonymized=False,
            include_provided_health_history_in_appeal=True,
        )

        fresh = Denial.objects.get(denial_id=self.denial.denial_id)
        self.assertEqual(fresh.health_history, TYPED)
        self.assertFalse(fresh.health_history_anonymized)
        self.assertTrue(fresh.include_provided_health_history_in_appeal)

    def test_a_resubmitted_upload_persists_the_columns_it_assigns(self):
        """The resubmission branch of create_or_update_denial. Every optional
        argument is passed, because a column is assigned only when its
        argument is not None, so an unpassed one proves nothing."""
        users = get_user_model()
        creating = ProfessionalUser.objects.create(
            user=users.objects.create_user(
                username="creating-pro", email="creating@clinic.example"
            ),
            active=True,
        )
        primary = ProfessionalUser.objects.create(
            user=users.objects.create_user(
                username="primary-pro", email="primary@clinic.example"
            ),
            active=True,
        )
        patient = PatientUser.objects.create(
            user=users.objects.create_user(
                username="the-patient", email="patient@example.org"
            ),
            active=True,
        )
        company = InsuranceCompany.objects.create(name="Widgets Health")
        plan = InsurancePlan.objects.create(
            insurance_company=company, plan_name="Widgets Gold PPO"
        )
        # patient_visible defaults to True, so the assertion below would pass
        # without the save. Start it at False so the save has to write it.
        Denial.objects.filter(denial_id=self.denial.denial_id).update(
            patient_visible=False
        )
        loaded = Denial.objects.get(denial_id=self.denial.denial_id)
        self.assertFalse(loaded.patient_visible)
        # Likewise: a different address, so hashed_email has to change.
        new_email = "moved@example.com"
        self.assertNotEqual(loaded.hashed_email, Denial.get_hashed_email(new_email))

        common_view_logic.DenialCreatorHelper.create_or_update_denial(
            email=new_email,
            denial_text="Your claim has been denied, again.",
            zip="",
            denial=loaded,
            health_history=TYPED,
            # Retains the raw address, which is what raw_email holds.
            store_raw_email=True,
            use_external_models=False,
            creating_professional=creating,
            primary_professional=primary,
            patient_user=patient,
            insurance_company="Widgets Health",
            insurance_company_obj=company,
            insurance_plan_obj=plan,
            patient_visible=True,
            microsite_slug="widgets",
            referral_source="Search Engine",
            referral_source_details="a friend linked it",
            tracking_info=TrackingInfo(
                user_agent="Mozilla/5.0 (tests)",
                ip_address="203.0.113.7",
                asn="AS64496",
                asn_name="EXAMPLE-AS",
            ),
        )

        fresh = Denial.objects.get(denial_id=self.denial.denial_id)
        self.assertEqual(fresh.denial_text, "Your claim has been denied, again.")
        self.assertEqual(fresh.hashed_email, Denial.get_hashed_email(new_email))
        self.assertEqual(fresh.raw_email, new_email)
        # No health_history assertion: the tail call to _update_denial lists
        # the column too, so one here would pass with health_history removed
        # from THIS save's update_fields.
        self.assertFalse(fresh.use_external)
        self.assertEqual(fresh.creating_professional_id, creating.id)
        self.assertEqual(fresh.primary_professional_id, primary.id)
        self.assertEqual(fresh.patient_user_id, patient.id)
        self.assertEqual(fresh.insurance_company, "Widgets Health")
        self.assertEqual(fresh.insurance_company_obj_id, company.id)
        self.assertEqual(fresh.insurance_plan_obj_id, plan.id)
        self.assertTrue(fresh.patient_visible)
        self.assertEqual(fresh.microsite_slug, "widgets")
        self.assertEqual(fresh.referral_source, "Search Engine")
        self.assertEqual(fresh.referral_source_details, "a friend linked it")
        self.assertEqual(fresh.user_agent, "Mozilla/5.0 (tests)")
        self.assertEqual(fresh.ip_address, "203.0.113.7")
        self.assertEqual(fresh.asn, "AS64496")
        self.assertEqual(fresh.asn_name, "EXAMPLE-AS")


class PressingNextDecidesNothingItCannotAskTest(OptionalStepsTestCase):
    """An unrendered BooleanField is a silent decision made for the patient.

    An unchecked box is simply absent from the POST, so a declared
    BooleanField(required=False) cleans to False whether the patient unticked
    it or the page never offered it. _update_denial writes any non-None value,
    so declaring a field health_history.html does not render turned every Next
    into a reset of that column.

    The page renders a box for ``health_history_consent``, so its absence IS
    an answer and is saved as one. The two older flags still have no box
    anywhere, so the rule above holds for both and this class guards them.
    """

    def test_a_plain_next_does_not_reset_the_flags_nobody_asks_about(self):
        Denial.objects.filter(denial_id=self.denial.denial_id).update(
            health_history_anonymized=True,
            include_provided_health_history_in_appeal=True,
        )
        payload = self.denial_ref()
        payload["health_history"] = STORED

        response = self.client.post(reverse("hh"), payload)

        self.assertEqual(response.status_code, 200)
        fresh = Denial.objects.get(denial_id=self.denial.denial_id)
        self.assertTrue(fresh.health_history_anonymized)
        self.assertTrue(
            fresh.include_provided_health_history_in_appeal,
            "a Next cleared the fax attachment flag, which this page never "
            "asks about",
        )

    def test_a_next_with_the_box_unticked_is_that_answer(self):
        """The page asks about consent now, so saying nothing means no."""
        Denial.objects.filter(denial_id=self.denial.denial_id).update(
            health_history_consent=True,
        )
        payload = self.denial_ref()
        payload["health_history"] = STORED

        self.client.post(reverse("hh"), payload)

        fresh = Denial.objects.get(denial_id=self.denial.denial_id)
        self.assertIs(fresh.health_history_consent, False)

    def test_a_field_the_page_cannot_render_is_dropped_before_the_save(self):
        """The rule behind the test above.

        The form cannot simply refuse to declare these: the REST serializer is
        built from it and drf_braces strips whatever the form omits, so taking
        them off stopped the API revoking a consent it was told to revoke. They
        stay declared, and the view drops the ones its own page never rendered.
        So the invariant is not "the form declares nothing the page omits", it
        is "nothing the page omits reaches the save".
        """
        rendered = self.client.get(reverse("hh"), self.denial_ref()).content.decode()
        unrendered = [
            name
            for name in core_forms.HealthHistory().fields
            if f'name="{name}"' not in rendered
        ]
        self.assertTrue(
            unrendered,
            "health_history.html now renders every field the form declares, so "
            "this test no longer guards anything; delete it or the view's drop "
            "list with it",
        )
        for name in unrendered:
            self.assertIn(
                name,
                views.PlanDocumentsView.UNRENDERED_CONSENT_FIELDS,
                f"HealthHistory declares {name}, health_history.html never "
                "renders it, and the view does not drop it, so every "
                "submission decides it by omission",
            )


class TheOptionalStepSaveTouchesTheRowTest(OptionalStepsTestCase):
    """last_interaction is auto_now, so it moves only when update_fields
    lists it. Scoping the save without listing it freezes the column."""

    def test_the_optional_step_save_moves_last_interaction(self):
        stale = timezone.now() - datetime.timedelta(days=3)
        Denial.objects.filter(denial_id=self.denial.denial_id).update(
            last_interaction=stale
        )
        loaded = Denial.objects.get(denial_id=self.denial.denial_id)

        common_view_logic.DenialCreatorHelper._update_denial(
            loaded, health_history=TYPED
        )

        fresh = Denial.objects.get(denial_id=self.denial.denial_id)
        self.assertGreater(fresh.last_interaction, stale)


class WhatThePageShowsIsGatedTest(OptionalStepsTestCase):
    """The page now shows stored medical history, so what it takes as
    proof of who is asking has to be what the save takes."""

    def test_the_right_id_and_secret_with_someone_elses_email_show_nothing(self):
        """A reference that does not resolve is sent to the upload page with
        an explanation, not rendered as a blank form: nothing of the case is
        shown either way."""
        ref = self.denial_ref()
        ref["email"] = "someone-else@example.com"

        response = self.client.get(reverse("hh"), ref)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"].split("?")[0], reverse("scan"))
        self.assertNotIn(STORED, response.content.decode())

    def test_a_rejected_submission_is_not_described_as_saved(self):
        response = self.client.post(
            reverse("hh"),
            {
                "denial_id": self.denial.denial_id,
                # No email: the form is invalid, nothing is saved.
                "semi_sekret": SEMI_SEKRET,
                "health_history": TYPED,
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertEqual(textarea_value(body).strip(), TYPED)
        self.assertNotIn("This is what is saved", body)
        self.assertEqual(self.stored_history(), STORED)

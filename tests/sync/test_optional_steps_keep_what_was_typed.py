"""The two optional steps must not lose what the person already gave us.

health_history.html hardcoded an empty textarea and plan_documents.html said
nothing about what had already been uploaded, so every entry to the health
history page arrived blank. The box is posted on every Next, and
``health_history`` is a CharField(required=False) that cleans to "", so the
blank box was then written straight over the stored history.

The fix is on the render side: every path into the page now shows what is
stored, so pressing Next posts the history back rather than a blank. The save
side is deliberately NOT guarded against blanks. Once the box shows what is
stored, an empty box means the person emptied it, and emptying it has to
work: this is their own health history, generate_appeal.py feeds the column
to the model with no gate, and no other page can remove it. Refusing the
blank would make deletion unreachable while the page still promises the step
is optional, which is worse than the data loss above.

Both properties are covered here: the value the server renders, on both entry
paths to each page, and a clear-and-Next that ends with the column empty. The
textarea assertions read the server's HTML, so they are about the
server-rendered value and never about the localStorage restore in
formPersistence.ts (which in any case declines to run once the field already
has a value).

The two views are crossed relative to their names: PlanDocumentsView renders
health_history.html under the URL name "hh", and DenialCollectedView renders
plan_documents.html under "dvc". Each test below names the template it is
really checking.
"""

import html as html_module
import re

from django.template.loader import render_to_string
from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance import common_view_logic, forms as core_forms
from fighthealthinsurance.models import Denial, PlanDocuments
from fhi_users.audit import TrackingInfo

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
        # The row that submission created, named by the session the next
        # submission will reuse it from.
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
        """A POST that fails validation re-renders this page. It must come
        back carrying the person's own words.

        This asserts that the BOUND value renders, not that it beats the
        stored one: an invalid POST is missing one of denial_id/email/
        semi_sekret, which are exactly what get_denial_ref_from_request needs,
        so the stored lookup returns "" here and there is nothing to beat.
        The precedence itself is pinned on the template below.
        """
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
        """The precedence the re-render depends on, pinned on the template
        directly. A bound form is what form_invalid re-renders; its initial
        carries the stored history and its data carries what was typed, and
        the box must show the typed text."""
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
    """The bug this branch exists to fix, end to end through the pages.

    The render half is what stops the loss: the box comes back holding the
    stored history, so Next posts that history rather than a blank.
    """

    def test_stepping_back_and_pressing_next_keeps_the_stored_history(self):
        """Type it, step forward, step back, press Next without touching the
        box. On main this left the column empty."""
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
        health_history entirely passes None and must change nothing. The
        plan-documents and entity-extract steps both go through
        update_denial with forms that have no health_history field."""
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

    The page calls the step optional and skippable, the column is fed to the
    appeal model with no gate (generate_appeal.py), and no other page can
    remove it. So clearing the box and pressing Next has to end with the
    column empty, and the page has to say so.
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
        """Not just the column: coming back must not redisplay it, or the
        person has no way to tell the removal worked."""
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
        Denial.objects.filter(denial_id=self.denial.denial_id).update(
            health_history=""
        )

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
        Denial.objects.filter(denial_id=stale.denial_id).update(
            procedure="colonoscopy"
        )

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
    value is silently dropped. Asserting only that another writer's column
    survives would pass just as happily against an update_fields list that had
    lost half its entries. So each converted save also gets a test that the
    columns it assigns come back. (The employer-name save is covered by
    test_the_employer_name_save_does_not_revert_another_writers_column above,
    which already asserts employer_name landed.)
    """

    def test_the_optional_step_save_persists_every_column_it_assigns(self):
        loaded = Denial.objects.get(denial_id=self.denial.denial_id)
        # Opposite to where they start, so a dropped column is visible.
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
        """The resubmission branch of create_or_update_denial, including the
        four columns TrackingInfo.update_model_fields assigns."""
        loaded = Denial.objects.get(denial_id=self.denial.denial_id)

        common_view_logic.DenialCreatorHelper.create_or_update_denial(
            email=EMAIL,
            denial_text=loaded.denial_text,
            zip="",
            denial=loaded,
            use_external_models=False,
            insurance_company="Widgets Health",
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
        self.assertFalse(fresh.use_external)
        self.assertEqual(fresh.insurance_company, "Widgets Health")
        self.assertTrue(fresh.patient_visible)
        self.assertEqual(fresh.microsite_slug, "widgets")
        self.assertEqual(fresh.referral_source, "Search Engine")
        self.assertEqual(fresh.referral_source_details, "a friend linked it")
        self.assertEqual(fresh.user_agent, "Mozilla/5.0 (tests)")
        self.assertEqual(fresh.ip_address, "203.0.113.7")
        self.assertEqual(fresh.asn, "AS64496")
        self.assertEqual(fresh.asn_name, "EXAMPLE-AS")

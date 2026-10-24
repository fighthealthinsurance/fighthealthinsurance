"""The share page asks what the plan costs, if people know.

Under "About the plan", three optional questions: what you pay for the plan,
how often, and who it covers. Leaving any of them out never stops a share: an
amount with no "how often" is kept with the period blank, and $0 is a real
answer. Each file shared together keeps the same answers, a share that is
asked to try again keeps them, staff see them in the admin, error reports
leave them out, and remove-my-data takes them with the files. The opt-in
boxes on other pages don't ask.
"""

from decimal import Decimal

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.urls import reverse

from fighthealthinsurance.chat_forms import SharePlanDocumentsForm
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.models import PolicyDocument
from tests.sync.test_shared_plan_documents_are_stored_encrypted_and_kept import (
    EMAIL,
    _ShareTestCase,
    pdf,
    txt,
)

PREMIUM_FIELDS = ("premium_amount", "premium_period", "who_is_covered")
HELP_TEXT = "If you know it. This helps us show what plans cost over time."


class _PremiumTestCase(_ShareTestCase):
    def soup(self, response):
        return BeautifulSoup(response.content, "html.parser")


class ThePremiumQuestionsAreOptionalTest(_PremiumTestCase):
    def test_none_of_them_is_required(self):
        form = SharePlanDocumentsForm()
        for name in PREMIUM_FIELDS:
            with self.subTest(field=name):
                self.assertFalse(form.fields[name].required)

    def test_they_fold_under_about_the_plan(self):
        soup = self.soup(self.client.get(self.url))
        about = soup.find("details", id="about-the-plan")
        for name in PREMIUM_FIELDS:
            with self.subTest(field=name):
                self.assertIsNotNone(about.find(attrs={"name": name}))
        self.assertTrue(
            set(PREMIUM_FIELDS) <= set(SharePlanDocumentsForm.ABOUT_THE_PLAN)
        )

    def test_the_amount_asks_for_your_share_of_the_premium(self):
        soup = self.soup(self.client.get(self.url))
        amount = soup.find("input", attrs={"name": "premium_amount"})
        label = soup.find("label", attrs={"for": amount["id"]})
        self.assertEqual(
            label.get_text(strip=True),
            "What you pay for the plan (your share of the premium), in dollars",
        )

    def test_the_amount_says_why_we_ask(self):
        self.assertEqual(
            SharePlanDocumentsForm().fields["premium_amount"].help_text, HELP_TEXT
        )
        soup = self.soup(self.client.get(self.url))
        amount = soup.find("input", attrs={"name": "premium_amount"})
        hint = soup.find(id=amount["aria-describedby"])
        self.assertEqual(hint.get_text(strip=True), HELP_TEXT)

    def test_the_choices_are_the_fixed_lists(self):
        soup = self.soup(self.client.get(self.url))

        def options(name):
            select = soup.find("select", attrs={"name": name})
            return [option.get_text(strip=True) for option in select.find_all("option")]

        self.assertEqual(
            options("premium_period"),
            [
                "Choose how often",
                "Each week",
                "Every two weeks",
                "Twice a month",
                "Each month",
                "Each year",
            ],
        )
        self.assertEqual(
            options("who_is_covered"),
            [
                "Choose who it covers",
                "Just you",
                "You and a spouse or partner",
                "You and your children",
                "Your whole family",
            ],
        )


class ThePremiumIsKeptWithTheFilesTest(_PremiumTestCase):
    def test_every_file_shared_together_keeps_the_answers(self):
        response = self.share(
            [pdf(), txt()],
            premium_amount="412.50",
            premium_period="biweekly",
            who_is_covered="self_and_children",
        )
        self.assertRedirects(response, self.url + "?shared=1")
        docs = list(PolicyDocument.objects.all())
        self.assertEqual(len(docs), 2)
        for doc in docs:
            with self.subTest(doc=doc.filename):
                self.assertEqual(doc.premium_amount, Decimal("412.50"))
                self.assertEqual(doc.premium_period, "biweekly")
                self.assertEqual(doc.who_is_covered, "self_and_children")

    def test_a_share_without_them_keeps_them_blank(self):
        self.share([pdf()])
        doc = PolicyDocument.objects.get()
        self.assertIsNone(doc.premium_amount)
        self.assertEqual(doc.premium_period, "")
        self.assertEqual(doc.who_is_covered, "")

    def test_a_plan_that_costs_nothing_needs_no_how_often(self):
        response = self.share([pdf()], premium_amount="0", who_is_covered="self_only")
        self.assertRedirects(response, self.url + "?shared=1")
        doc = PolicyDocument.objects.get()
        self.assertEqual(doc.premium_amount, Decimal("0"))
        self.assertEqual(doc.premium_period, "")

    def test_an_amount_with_no_how_often_is_kept_with_a_blank_period(self):
        response = self.share([pdf(), txt()], premium_amount="250")
        self.assertRedirects(response, self.url + "?shared=1")
        for doc in PolicyDocument.objects.all():
            with self.subTest(doc=doc.filename):
                self.assertEqual(doc.premium_amount, Decimal("250"))
                self.assertEqual(doc.premium_period, "")

    def test_who_it_covers_can_be_given_alone(self):
        self.share([pdf()], who_is_covered="family")
        self.assertEqual(PolicyDocument.objects.get().who_is_covered, "family")

    def test_removing_my_data_takes_the_answers_with_the_files(self):
        self.share(
            [pdf()],
            email=EMAIL,
            premium_amount="250",
            premium_period="monthly",
            who_is_covered="self_only",
        )
        RemoveDataHelper.remove_data_for_email(EMAIL)
        self.assertFalse(PolicyDocument.objects.exists())


class ThePremiumIsCheckedTest(_PremiumTestCase):
    def assertRefusedOn(self, field, response):
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PolicyDocument.objects.count(), 0)
        soup = self.soup(response)
        note = soup.find(id=f"id_{field}_error")
        self.assertTrue(note.get_text(strip=True))
        # The section opens so the person sees what to fix.
        self.assertTrue(soup.find("details", id="about-the-plan").has_attr("open"))
        return note.get_text(strip=True)

    def test_an_amount_below_zero_is_refused(self):
        response = self.share([pdf()], premium_amount="-5", premium_period="monthly")
        self.assertEqual(
            self.assertRefusedOn("premium_amount", response),
            "Enter an amount from 0 to 100,000 dollars.",
        )

    def test_an_amount_over_the_limit_is_refused(self):
        response = self.share(
            [pdf()], premium_amount="100000.01", premium_period="yearly"
        )
        self.assertRefusedOn("premium_amount", response)

    def test_the_limit_itself_is_taken(self):
        response = self.share(
            [pdf()],
            premium_amount=str(SharePlanDocumentsForm.MAX_PREMIUM),
            premium_period="yearly",
        )
        self.assertRedirects(response, self.url + "?shared=1")

    def test_an_amount_that_isnt_a_number_is_refused(self):
        response = self.share([pdf()], premium_amount="lots", premium_period="monthly")
        self.assertEqual(
            self.assertRefusedOn("premium_amount", response),
            "Enter an amount in dollars, like 250 or 250.50.",
        )

    def test_fractions_of_a_cent_are_refused(self):
        response = self.share(
            [pdf()], premium_amount="250.505", premium_period="monthly"
        )
        self.assertRefusedOn("premium_amount", response)


    def test_a_how_often_not_on_the_list_is_refused(self):
        response = self.share(
            [pdf()], premium_amount="250", premium_period="every_full_moon"
        )
        self.assertRefusedOn("premium_period", response)

    def test_a_who_it_covers_not_on_the_list_is_refused(self):
        response = self.share([pdf()], who_is_covered="the_neighbours")
        self.assertRefusedOn("who_is_covered", response)


class ThePremiumIsKeptWhenAskedToTryAgainTest(_PremiumTestCase):
    def test_a_share_asked_to_try_again_keeps_the_answers(self):
        response = self.share(
            [txt()],
            rendered="",
            premium_amount="412.50",
            premium_period="biweekly",
            who_is_covered="self_and_partner",
        )
        self.assertEqual(PolicyDocument.objects.count(), 0)
        soup = self.soup(response)
        self.assertEqual(soup.find(id="id_premium_amount")["value"], "412.50")
        for name, value in (
            ("premium_period", "biweekly"),
            ("who_is_covered", "self_and_partner"),
        ):
            with self.subTest(field=name):
                chosen = soup.find("select", attrs={"name": name}).find(
                    "option", selected=True
                )
                self.assertEqual(chosen["value"], value)


class ThePremiumIsKeptOutOfErrorReportsTest(_PremiumTestCase):
    def test_the_answers_are_marked_sensitive(self):
        response = self.share(
            [pdf()],
            premium_amount="250",
            premium_period="monthly",
            who_is_covered="family",
        )
        hidden = response.wsgi_request.sensitive_post_parameters
        for name in PREMIUM_FIELDS:
            with self.subTest(field=name):
                self.assertIn(name, hidden)


class StaffSeeThePremiumTest(_PremiumTestCase):
    def test_the_admin_shows_the_answers(self):
        self.share(
            [pdf()],
            premium_amount="412.50",
            premium_period="biweekly",
            who_is_covered="self_and_children",
        )
        doc = PolicyDocument.objects.get()
        admin = get_user_model().objects.create_superuser(
            username="mary_eliza_mahoney", password="pw-for-tests"
        )
        self.client.force_login(admin)
        page = self.client.get(
            reverse("admin:fighthealthinsurance_policydocument_change", args=[doc.pk])
        )
        soup = self.soup(page)
        self.assertEqual(soup.find(attrs={"name": "premium_amount"})["value"], "412.50")
        for name, value in (
            ("premium_period", "biweekly"),
            ("who_is_covered", "self_and_children"),
        ):
            with self.subTest(field=name):
                chosen = soup.find("select", attrs={"name": name}).find(
                    "option", selected=True
                )
                self.assertEqual(chosen["value"], value)

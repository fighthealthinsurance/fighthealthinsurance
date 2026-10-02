"""Cancelling a fax payment on Stripe brings the person back to their letter.

Stripe's cancel_url used to be the home page, so the letter the person had
just edited was gone from their screen. It now goes to a page that shows the
letter again from the staged fax, but only in the browser that staged it:
the address carries an encrypted reference, never an id, and nothing about
the page may be cached.
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance.helpers.fax_helpers import FaxHelperResults
from fighthealthinsurance.models import (
    Denial,
    FaxesToSend,
    PubMedArticleSummarized,
    PubMedQueryData,
)
from fighthealthinsurance import views as views_module
from fighthealthinsurance.views import (
    FAX_CANCEL_REF_TTL_SECONDS,
    issue_denial_ref_token,
    issue_fax_cancel_ref,
)

EMAIL = "patient@example.com"
LETTER = "Dear Insurer, please cover my treatment. Signed, Pat Example."


def mint(client: Client, fax_uuid: str, hashed_email: str, **choices) -> str:
    """A cancel reference minted in this client's session, as staging does."""
    session = client.session
    ref = issue_fax_cancel_ref(
        SimpleNamespace(session=session), fax_uuid, hashed_email, **choices
    )
    session.save()
    assert ref
    return ref


class FaxPaymentCancelledViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.hashed_email = Denial.get_hashed_email(EMAIL)
        self.denial = Denial.objects.create(
            denial_text="denied",
            semi_sekret="the-case-secret",
            hashed_email=self.hashed_email,
            insurance_company="Example Health",
            appeal_fax_number="15550000000",
            appeal_text="The draft {{Your Name}} chose.",
        )
        self.fax = FaxesToSend.objects.create(
            paid=True,
            hashed_email=self.hashed_email,
            email=EMAIL,
            appeal_text=LETTER,
            denial_id=self.denial,
            destination="15551234567",
            pmids=["111"],
        )
        self.url = reverse("fax_payment_cancelled")

    def get(self, ref, client=None):
        return (client or self.client).get(self.url, {"ref": ref})

    def test_shows_the_letter_with_the_fax_details_filled_in(self):
        response = self.get(mint(self.client, self.fax.uuid, self.hashed_email))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "appeal.html")
        page = response.content.decode()
        self.assertIn("nothing was charged", page)
        self.assertIn(LETTER, page)
        form = response.context["fax_form"]
        # The letter goes in the draft box; the finished box starts empty so
        # the page's script builds it from the draft and keeps it in step.
        self.assertEqual(response.context["appeal"], LETTER)
        self.assertNotIn("completed_appeal_text", form.initial)
        self.assertEqual(form.initial["fax_phone"], "15551234567")
        self.assertEqual(form.initial["insurance_company"], "Example Health")
        self.assertEqual(form.initial["email"], EMAIL)
        self.assertEqual(form.initial["semi_sekret"], "the-case-secret")
        self.assertEqual(form.initial["denial_id"], self.denial.denial_id)
        # The fax form, its pay-what-you-want choices with $0, and print.
        self.assertIn(reverse("stagefaxview"), page)
        self.assertIn('name="fax_pwyw" value="0"', page)
        self.assertIn('id="print_appeal"', page)

    def test_the_insurer_typed_and_the_history_choice_come_back(self):
        ref = mint(
            self.client,
            self.fax.uuid,
            self.hashed_email,
            include_history=True,
            insurer="Example Health of Ohio",
        )
        form = self.get(ref).context["fax_form"]
        self.assertEqual(form.initial["insurance_company"], "Example Health of Ohio")
        self.assertTrue(form.initial["include_provided_health_history"])

    def test_without_choices_the_history_box_starts_unticked(self):
        form = self.get(mint(self.client, self.fax.uuid, self.hashed_email)).context[
            "fax_form"
        ]
        self.assertFalse(form.initial["include_provided_health_history"])
        self.assertEqual(form.initial["insurance_company"], "Example Health")

    def test_says_the_fax_is_still_going_out(self):
        page = self.get(mint(self.client, self.fax.uuid, self.hashed_email))
        page = page.content.decode()
        self.assertIn("Your fax is still on its way", page)
        self.assertIn("this sends a second copy", page)
        self.assertIn("Fax It Again", page)

    def test_a_fax_that_went_through_says_so(self):
        self.fax.sent = True
        self.fax.fax_success = True
        self.fax.save()
        page = self.get(mint(self.client, self.fax.uuid, self.hashed_email))
        page = page.content.decode()
        self.assertIn("already gone through", page)
        self.assertIn(LETTER, page)

    def test_a_fax_that_failed_offers_another_try(self):
        self.fax.sent = True
        self.fax.fax_success = False
        self.fax.save()
        page = self.get(mint(self.client, self.fax.uuid, self.hashed_email))
        page = page.content.decode()
        self.assertIn("didn't go through", page)
        self.assertNotIn("this sends a second copy", page)

    def test_only_the_articles_the_person_kept_are_ticked(self):
        for pmid in ("111", "222"):
            PubMedArticleSummarized.objects.create(pmid=pmid, title=f"Study {pmid}")
        PubMedQueryData.objects.create(
            query="q", articles=json.dumps(["111", "222"]), denial_id=self.denial
        )
        response = self.get(mint(self.client, self.fax.uuid, self.hashed_email))
        fields = response.context["fax_form"].fields
        self.assertIs(fields["pubmed_111"].initial, True)
        self.assertIs(fields["pubmed_222"].initial, False)

    def test_never_cached(self):
        response = self.get(mint(self.client, self.fax.uuid, self.hashed_email))
        cache_control = response["Cache-Control"]
        self.assertIn("no-store", cache_control)
        self.assertIn("private", cache_control)
        self.assertNotIn("public", cache_control)

    def test_the_page_without_a_letter_is_not_cached_either(self):
        response = self.get("not-a-reference")
        self.assertIn("no-store", response["Cache-Control"])

    def test_another_browser_gets_no_letter(self):
        ref = mint(self.client, self.fax.uuid, self.hashed_email)
        response = self.get(ref, client=Client())
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "fax_payment_cancelled.html")
        page = response.content.decode()
        self.assertNotIn(LETTER, page)
        self.assertNotIn("the-case-secret", page)
        self.assertIn("nothing was charged", page)

    def test_a_reference_to_someone_elses_email_opens_nothing(self):
        other = Denial.get_hashed_email("someone-else@example.com")
        response = self.get(mint(self.client, self.fax.uuid, other))
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(LETTER, response.content.decode())

    def test_an_unknown_fax_opens_nothing(self):
        ref = mint(
            self.client, "00000000-0000-4000-8000-000000000000", self.hashed_email
        )
        response = self.get(ref)
        self.assertEqual(response.status_code, 404)

    def test_no_reference_or_a_tampered_one_opens_nothing(self):
        ref = mint(self.client, self.fax.uuid, self.hashed_email)
        for value in ("", "garbage", ref[:-4] + "AAAA"):
            response = self.get(value)
            self.assertEqual(response.status_code, 404, value)
            self.assertNotIn(LETTER, response.content.decode())
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)

    def test_a_back_link_reference_is_not_a_fax_reference(self):
        session = self.client.session
        back_ref = issue_denial_ref_token(
            SimpleNamespace(session=session),
            self.denial.denial_id,
            EMAIL,
            self.denial.semi_sekret,
        )
        session.save()
        response = self.get(back_ref)
        self.assertEqual(response.status_code, 404)

    def test_a_reference_older_than_a_day_opens_nothing(self):
        ref = mint(self.client, self.fax.uuid, self.hashed_email)
        later = time.time() + FAX_CANCEL_REF_TTL_SECONDS + 120
        with patch("cryptography.fernet.time") as clock:
            clock.time.return_value = later
            response = self.get(ref)
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(LETTER, response.content.decode())


class StageFaxCancelUrlTest(TestCase):
    """Stripe is sent to the cancel page, and the round trip shows the letter."""

    def setUp(self):
        self.client = Client()
        self.hashed_email = Denial.get_hashed_email(EMAIL)
        self.denial = Denial.objects.create(
            denial_text="denied",
            semi_sekret="the-case-secret",
            hashed_email=self.hashed_email,
            insurance_company="Example Health",
        )
        self.fax = FaxesToSend.objects.create(
            paid=True,
            hashed_email=self.hashed_email,
            email=EMAIL,
            appeal_text=LETTER,
            denial_id=self.denial,
            destination="15551234567",
        )

    def stage(self):
        checkout = MagicMock()
        checkout.url = "https://checkout.stripe.test/session"
        with (
            patch(
                "fighthealthinsurance.common_view_logic.AppealAssemblyHelper.create_or_update_appeal"
            ),
            patch(
                "fighthealthinsurance.fax_views.SendFaxHelper.stage_appeal_as_fax",
                return_value=FaxHelperResults(
                    uuid=str(self.fax.uuid), hashed_email=self.hashed_email
                ),
            ),
            patch(
                "fighthealthinsurance.fax_views.get_or_create_price",
                return_value=("prod_test", "price_test"),
            ),
            patch("stripe.checkout.Session.create", return_value=checkout) as create,
        ):
            response = self.client.post(
                reverse("stagefaxview"),
                {
                    "denial_id": self.denial.denial_id,
                    "email": EMAIL,
                    "semi_sekret": self.denial.semi_sekret,
                    "name": "Pat Example",
                    "insurance_company": "Example Health of Ohio",
                    "fax_phone": "15551234567",
                    "completed_appeal_text": LETTER,
                    "include_provided_health_history": "on",
                    "fax_pwyw": "5",
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, checkout.url)
        create.assert_called_once()
        return create.call_args.kwargs

    def test_cancel_url_is_the_cancel_page_with_no_ids_in_it(self):
        kwargs = self.stage()
        cancel = urlparse(kwargs["cancel_url"])
        self.assertEqual(cancel.path, reverse("fax_payment_cancelled"))
        self.assertEqual(list(parse_qs(cancel.query)), ["ref"])
        for private in (
            str(self.fax.uuid),
            self.hashed_email,
            EMAIL,
            "the-case-secret",
        ):
            self.assertNotIn(private, kwargs["cancel_url"])
        # Payment still lands on the send view, as before.
        self.assertIn(
            reverse(
                "sendfaxview",
                kwargs={"uuid": self.fax.uuid, "hashed_email": self.hashed_email},
            ),
            kwargs["success_url"],
        )

    def test_cancelling_on_stripe_comes_back_to_the_letter(self):
        cancel = urlparse(self.stage()["cancel_url"])
        response = self.client.get(f"{cancel.path}?{cancel.query}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(LETTER, response.content.decode())

    def test_cancelling_brings_back_the_insurer_typed_and_the_history_choice(self):
        cancel = urlparse(self.stage()["cancel_url"])
        form = self.client.get(f"{cancel.path}?{cancel.query}").context["fax_form"]
        self.assertEqual(form.initial["insurance_company"], "Example Health of Ohio")
        self.assertTrue(form.initial["include_provided_health_history"])

    def test_the_name_typed_never_goes_into_the_cancel_address(self):
        """The reference travels to Stripe; even encrypted it holds no name."""
        cancel_url = self.stage()["cancel_url"]
        self.assertNotIn("Pat", cancel_url)
        token = parse_qs(urlparse(cancel_url).query)["ref"][0]
        fernet = views_module._denial_ref_fernet(self.client.session, create=False)
        payload = json.loads(fernet.decrypt(token.encode()).decode())
        self.assertNotIn("Pat Example", json.dumps(payload))
        self.assertEqual(set(payload), {"k", "f", "h", "i", "n"})

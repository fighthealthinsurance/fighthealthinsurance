"""The pages a fax's (uuid, hashed_email) pair opens: the follow-up page the
fax email links to, and Stripe's success page for a fax payment.

The pair in the address is all it takes to open them, so they load no
third-party scripts, never put the address in a referrer and are never
cached. Neither does the 404 for an address under their paths.
The follow-up page re-sends a failed fax, or one with no number, for
FAX_RESEND_WINDOW after it was staged. A fax that went through stays sent.
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.helpers.fax_helpers import FAX_RESEND_WINDOW
from fighthealthinsurance.models import Denial, FaxesToSend

EMAIL = "patient@example.com"
LETTER = "Dear Insurer, please cover my treatment. Signed, Pat Example."
ON_FILE = "15551234567"
NEW_NUMBER = "15559876543"
TRACKERS = ("googletagmanager.com", "bat.bing.com", "uetq")
DISPATCH = "fighthealthinsurance.helpers.fax_helpers._dispatch_or_ray_fax"


class FaxLinkPageTestCase(TestCase):
    def setUp(self):
        self.client = Client()
        self.hashed_email = Denial.get_hashed_email(EMAIL)
        self.denial = Denial.objects.create(
            denial_text="denied",
            hashed_email=self.hashed_email,
            raw_email=EMAIL,
            health_history="",
        )
        # A send that failed: what the follow-up email offers to re-send.
        self.fax = self.make_fax()

    def make_fax(self, **overrides):
        fields = dict(
            paid=True,
            hashed_email=self.hashed_email,
            email=EMAIL,
            appeal_text=LETTER,
            denial_id=self.denial,
            destination=ON_FILE,
            sent=True,
            fax_success=False,
        )
        fields.update(overrides)
        return FaxesToSend.objects.create(**fields)

    def staged_ago(self, age):
        FaxesToSend.objects.filter(pk=self.fax.pk).update(date=timezone.now() - age)

    def deliver(self):
        FaxesToSend.objects.filter(pk=self.fax.pk).update(sent=True, fax_success=True)

    def followup_url(self, fax_uuid=None, hashed_email=None):
        return reverse(
            "fax-followup",
            kwargs={
                "uuid": fax_uuid or self.fax.uuid,
                "hashed_email": hashed_email or self.hashed_email,
            },
        )

    def resend(self, url=None, fax_phone=NEW_NUMBER, fax=None):
        fax = fax or self.fax
        with patch(DISPATCH) as dispatch:
            response = self.client.post(
                url or self.followup_url(),
                {
                    "fax_phone": fax_phone,
                    "uuid": str(fax.uuid),
                    "hashed_email": fax.hashed_email,
                },
            )
        return response, dispatch

    def assertNoThirdPartyScripts(self, response):
        html = response.content.decode()
        for tag in TRACKERS:
            with self.subTest(tag=tag):
                self.assertNotIn(tag, html)

    def assertNeverCached(self, response):
        self.assertIn("no-store", response["Cache-Control"])

    def assertUnchanged(self):
        fax = FaxesToSend.objects.get(pk=self.fax.pk)
        self.assertEqual(fax.destination, ON_FILE)
        self.assertTrue(fax.sent)


class FaxFollowUpPageTest(FaxLinkPageTestCase):
    def test_the_form_loads_no_third_party_scripts(self):
        response = self.client.get(self.followup_url())
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="fax_phone"', response.content.decode())
        self.assertNoThirdPartyScripts(response)

    def test_the_form_is_never_cached(self):
        self.assertNeverCached(self.client.get(self.followup_url()))

    def test_the_form_page_refers_with_the_site_origin_only(self):
        # strict-origin: a page a link here opens, which may load the
        # analytics tags, gets the origin and not this address. Not
        # no-referrer: the form would post with "Origin: null" and fail the
        # CSRF check. Not same-origin: the next page on this site would get
        # the whole address.
        response = self.client.get(self.followup_url())
        self.assertEqual(response["Referrer-Policy"], "strict-origin")

    def test_a_form_that_does_not_validate_comes_back_the_same_way(self):
        response, dispatch = self.resend(fax_phone="")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertNoThirdPartyScripts(response)
        self.assertNeverCached(response)
        self.assertEqual(response["Referrer-Policy"], "strict-origin")
        dispatch.assert_not_called()

    def test_a_fax_number_longer_than_the_destination_holds_is_a_form_error(self):
        response, dispatch = self.resend(fax_phone="1" * 21)
        self.assertIn("fax_phone", response.context["form"].errors)
        dispatch.assert_not_called()
        self.assertUnchanged()

    def test_a_failed_fax_is_sent_to_the_new_number(self):
        response, dispatch = self.resend()
        self.assertTemplateUsed(response, "fax_followup_thankyou.html")
        dispatch.assert_called_once()
        fax = FaxesToSend.objects.get(pk=self.fax.pk)
        self.assertEqual(fax.destination, NEW_NUMBER)
        self.assertFalse(fax.sent)

    def test_the_thank_you_page_loads_no_third_party_scripts(self):
        response, _ = self.resend()
        self.assertNoThirdPartyScripts(response)
        self.assertNeverCached(response)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def test_the_fax_sent_is_the_one_the_address_names(self):
        other = self.make_fax(destination="15550000000")
        self.resend(fax=other)
        self.assertEqual(
            FaxesToSend.objects.get(pk=self.fax.pk).destination, NEW_NUMBER
        )
        self.assertEqual(
            FaxesToSend.objects.get(pk=other.pk).destination, "15550000000"
        )

    def test_a_fax_with_no_number_can_be_given_one(self):
        FaxesToSend.objects.filter(pk=self.fax.pk).update(destination=None)
        _, dispatch = self.resend()
        dispatch.assert_called_once()
        self.assertEqual(
            FaxesToSend.objects.get(pk=self.fax.pk).destination, NEW_NUMBER
        )

    def test_a_delivered_fax_says_it_went_through_in_place_of_the_form(self):
        self.deliver()
        response = self.client.get(self.followup_url())
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Your fax went through", html)
        self.assertIn("support42@fighthealthinsurance.com", html)
        self.assertNotIn('name="fax_phone"', html)
        self.assertNoThirdPartyScripts(response)
        self.assertNeverCached(response)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def test_a_delivered_fax_stays_sent(self):
        self.deliver()
        response, dispatch = self.resend()
        self.assertIn("Your fax went through", response.content.decode())
        dispatch.assert_not_called()
        self.assertUnchanged()
        self.assertTrue(FaxesToSend.objects.get(pk=self.fax.pk).fax_success)

    def test_a_fax_past_the_window_says_the_link_no_longer_works(self):
        self.staged_ago(FAX_RESEND_WINDOW + timedelta(days=1))
        response = self.client.get(self.followup_url())
        self.assertEqual(response.status_code, 404)
        html = response.content.decode()
        self.assertIn("This link no longer works", html)
        self.assertIn(f"works for {FAX_RESEND_WINDOW.days} days", html)
        self.assertNotIn('name="fax_phone"', html)
        self.assertNoThirdPartyScripts(response)
        self.assertNeverCached(response)

    def test_a_fax_past_the_window_is_not_sent_again(self):
        self.staged_ago(FAX_RESEND_WINDOW + timedelta(days=1))
        response, dispatch = self.resend()
        self.assertIn("This link no longer works", response.content.decode())
        dispatch.assert_not_called()
        self.assertUnchanged()

    def test_a_fax_inside_the_window_can_still_be_sent_again(self):
        self.staged_ago(FAX_RESEND_WINDOW - timedelta(days=1))
        _, dispatch = self.resend()
        dispatch.assert_called_once()

    def test_an_unknown_pair_is_a_404(self):
        for url in (
            self.followup_url(hashed_email=Denial.get_hashed_email("x@example.com")),
            self.followup_url(fax_uuid=uuid.uuid4()),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
                response, dispatch = self.resend(url=url)
                self.assertEqual(response.status_code, 404)
                dispatch.assert_not_called()
                self.assertNoThirdPartyScripts(response)
                self.assertNeverCached(response)


class FaxAddressMatchingNoRouteTest(FaxLinkPageTestCase):
    """An address under the fax pages' paths that no route takes, such as
    one a mail client changed, still carries the pair."""

    def addresses(self):
        followup = self.followup_url()
        sendfax = reverse(
            "sendfaxview",
            kwargs={"uuid": self.fax.uuid, "hashed_email": self.hashed_email},
        )
        return (
            followup + ")",
            followup + ",",
            followup + "/more",
            followup.replace(str(self.fax.uuid), str(self.fax.uuid).upper()),
            sendfax + ")",
        )

    def test_gets_the_404_page_with_no_third_party_scripts(self):
        for url in self.addresses():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 404)
                self.assertTemplateUsed(response, "404.html")
                self.assertNoThirdPartyScripts(response)
                self.assertNeverCached(response)
                self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def test_another_unknown_address_gets_the_usual_404_page(self):
        response = self.client.get("/v0/faxfollowupx/" + str(self.fax.uuid))
        self.assertEqual(response.status_code, 404)
        self.assertIn("googletagmanager.com", response.content.decode())


class SendFaxPageTest(FaxLinkPageTestCase):
    """Stripe's success page for a fax payment."""

    def url(self, fax_uuid=None, hashed_email=None):
        return reverse(
            "sendfaxview",
            kwargs={
                "uuid": fax_uuid or self.fax.uuid,
                "hashed_email": hashed_email or self.hashed_email,
            },
        )

    def test_the_page_loads_no_third_party_scripts(self):
        with patch(DISPATCH):
            response = self.client.get(self.url())
        self.assertTemplateUsed(response, "fax_thankyou.html")
        self.assertNoThirdPartyScripts(response)
        self.assertNeverCached(response)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def test_the_already_sent_page_loads_no_third_party_scripts(self):
        self.deliver()
        with patch(DISPATCH) as dispatch:
            response = self.client.get(self.url())
        self.assertIn("Already received", response.content.decode())
        dispatch.assert_not_called()
        self.assertNoThirdPartyScripts(response)

    def test_an_unknown_pair_is_a_404(self):
        for url in (
            self.url(hashed_email=Denial.get_hashed_email("x@example.com")),
            self.url(fax_uuid=uuid.uuid4()),
        ):
            with self.subTest(url=url):
                with patch(DISPATCH) as dispatch:
                    response = self.client.get(url)
                self.assertEqual(response.status_code, 404)
                dispatch.assert_not_called()
                self.assertNoThirdPartyScripts(response)

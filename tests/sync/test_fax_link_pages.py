"""The fax links and the pages they open: the follow-up link the fax email
carries, and Stripe's success link for a fax payment.

A link's address carries the fax's (uuid, hashed_email) pair, which is all
it takes to use it. So the link keeps the fax in the session and redirects
to a page whose address carries nothing, and that page loads the site's
analytics tags like any other. Each link opened gets a random ref in the
session, which the page's form posts back, so a form re-sends the fax it
was shown for. The page says which fax it is, by the day it was sent and
the number on file, and fills the fax number box with that number. The
redirect, and the 404 for a pair that
matches no fax, are never cached and send no referrer, and that 404 loads no
analytics tags. The follow-up page re-sends a failed fax, or one with no
number, for FAX_RESEND_WINDOW after it was staged. A fax that went through
stays sent. Stripe's success link sends the fax once, then shows the sent
page however often it is reloaded.
"""

import re
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.template.defaultfilters import date as date_filter
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.fax_views import (
    FAX_FOLLOWUP_SESSION_KEY,
    FAX_FOLLOWUPS_KEPT,
    FAX_SENT_SESSION_KEY,
)
from fighthealthinsurance.helpers.fax_helpers import FAX_RESEND_WINDOW
from fighthealthinsurance.models import Denial, FaxesToSend

EMAIL = "patient@example.com"
LETTER = "Dear Insurer, please cover my treatment. Signed, Pat Example."
ON_FILE = "15551234567"
NEW_NUMBER = "15559876543"
TRACKERS = ("googletagmanager.com", "bat.bing.com", "uetq")
DISPATCH = "fighthealthinsurance.helpers.fax_helpers._dispatch_or_ray_fax"
FAX_REF = re.compile(r'name="fax_ref" value="([^"]+)"')
NUMBER_BOX = re.compile(r'<input[^>]*name="fax_phone"[^>]*>')


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
        # The ref on the follow-up page opened last, which resend() posts.
        self.shown_ref = None

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

    def followup_link(self, fax=None, fax_uuid=None, hashed_email=None, name=None):
        fax = fax or self.fax
        return reverse(
            name or "fax-followup",
            kwargs={
                "uuid": fax_uuid or fax.uuid,
                "hashed_email": hashed_email or fax.hashed_email,
            },
        )

    def sendfax_link(self, fax_uuid=None, hashed_email=None):
        return reverse(
            "sendfaxview",
            kwargs={
                "uuid": fax_uuid or self.fax.uuid,
                "hashed_email": hashed_email or self.hashed_email,
            },
        )

    def open_followup(self, fax=None, client=None):
        """Open the follow-up email's link and land on the page."""
        client = client or self.client
        response = client.get(self.followup_link(fax=fax), follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse("fax-followup-page"))
        self.shown_ref = self.ref_on(response)
        return response

    def ref_on(self, response):
        """The fax_ref the page's form posts back, or None with no form."""
        found = FAX_REF.search(response.content.decode())
        return found.group(1) if found else None

    def number_in_box(self, response):
        """What the page's fax number box holds, "" when it is empty."""
        box = NUMBER_BOX.search(response.content.decode())
        self.assertIsNotNone(box)
        value = re.search(r'value="([^"]*)"', box.group(0))
        return value.group(1) if value else ""

    def day_sent(self, fax=None):
        """The day the fax was sent, as the pages show it."""
        fax = FaxesToSend.objects.get(pk=(fax or self.fax).pk)
        return date_filter(fax.date, "F j")

    def which_fax(self, fax=None):
        """The line on the follow-up page that says which fax it is for."""
        fax = FaxesToSend.objects.get(pk=(fax or self.fax).pk)
        return (
            f"This is about the fax we tried to send on {self.day_sent(fax)} "
            f"to {fax.destination}."
        )

    def resend(self, fax_phone=NEW_NUMBER, ref=None):
        """Submit the re-send form, by default the one on the page opened
        last."""
        data = {"fax_phone": fax_phone}
        if ref or self.shown_ref:
            data["fax_ref"] = ref or self.shown_ref
        with patch(DISPATCH) as dispatch:
            response = self.client.post(reverse("fax-followup-page"), data)
        return response, dispatch

    def assertNoThirdPartyScripts(self, response):
        html = response.content.decode()
        for tag in TRACKERS:
            with self.subTest(tag=tag):
                self.assertNotIn(tag, html)

    def assertAnalyticsTags(self, response):
        html = response.content.decode()
        for tag in TRACKERS:
            with self.subTest(tag=tag):
                self.assertIn(tag, html)

    def assertNoPair(self, text, fax=None):
        fax = fax or self.fax
        self.assertNotIn(str(fax.uuid), text)
        self.assertNotIn(fax.hashed_email, text)

    def assertNeverCached(self, response):
        self.assertIn("no-store", response["Cache-Control"])

    def assertUnchanged(self, fax=None, destination=ON_FILE):
        fax = FaxesToSend.objects.get(pk=(fax or self.fax).pk)
        self.assertEqual(fax.destination, destination)
        self.assertTrue(fax.sent)


class FaxFollowUpLinkTest(FaxLinkPageTestCase):
    """The address in the fax email."""

    def test_redirects_to_the_page_with_no_ids_in_its_address(self):
        for name in (
            "fax-followup",
            "fax-followup-with-a-period",
            "fax-followup-with-trailing-slash",
        ):
            with self.subTest(name=name):
                response = self.client.get(self.followup_link(name=name))
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], reverse("fax-followup-page"))
                self.assertNoPair(response["Location"])

    def test_the_redirect_is_never_cached_and_sends_no_referrer(self):
        response = self.client.get(self.followup_link())
        self.assertNeverCached(response)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def test_keeps_the_fax_in_a_fresh_session(self):
        before = self.client.session.session_key
        self.client.get(self.followup_link())
        after = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.assertNotEqual(before, after)
        [[ref, pk]] = self.client.session[FAX_FOLLOWUP_SESSION_KEY]
        self.assertEqual(pk, self.fax.pk)
        self.assertNotIn(ref, (str(self.fax.uuid), self.fax.hashed_email))

    def test_keeps_only_the_links_opened_last(self):
        for _ in range(FAX_FOLLOWUPS_KEPT + 3):
            self.client.get(self.followup_link())
        kept = self.client.session[FAX_FOLLOWUP_SESSION_KEY]
        self.assertEqual(len(kept), FAX_FOLLOWUPS_KEPT)

    def test_an_unknown_pair_is_a_404_that_loads_no_third_party_scripts(self):
        for url in (
            self.followup_link(hashed_email=Denial.get_hashed_email("x@example.com")),
            self.followup_link(fax_uuid=uuid.uuid4()),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 404)
                self.assertTemplateUsed(response, "404.html")
                self.assertNoThirdPartyScripts(response)
                self.assertNeverCached(response)
                self.assertEqual(response["Referrer-Policy"], "no-referrer")
                self.assertNotIn(FAX_FOLLOWUP_SESSION_KEY, self.client.session)

    def test_a_post_to_the_link_redirects_to_the_page_and_sends_nothing(self):
        """As a re-send form rendered before the page moved would post."""
        with patch(DISPATCH) as dispatch:
            response = self.client.post(
                self.followup_link(),
                {
                    "fax_phone": NEW_NUMBER,
                    "uuid": str(self.fax.uuid),
                    "hashed_email": self.fax.hashed_email,
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("fax-followup-page"))
        dispatch.assert_not_called()
        self.assertUnchanged()


class FaxFollowUpPageTest(FaxLinkPageTestCase):
    """The page the link redirects to."""

    def test_the_form_loads_the_analytics_tags(self):
        response = self.open_followup()
        self.assertEqual(response.status_code, 200)
        self.assertIn('name="fax_phone"', response.content.decode())
        self.assertAnalyticsTags(response)

    def test_the_page_holds_no_ids(self):
        response = self.open_followup()
        html = response.content.decode()
        self.assertNoPair(html)
        self.assertIn(f'action="{reverse("fax-followup-page")}"', html)

    def test_the_page_is_never_cached(self):
        self.assertNeverCached(self.open_followup())

    def test_a_form_that_does_not_validate_comes_back(self):
        self.open_followup()
        response, dispatch = self.resend(fax_phone="")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertNoPair(response.content.decode())
        self.assertEqual(self.ref_on(response), self.shown_ref)
        dispatch.assert_not_called()

    def test_a_fax_number_longer_than_the_destination_holds_is_a_form_error(self):
        self.open_followup()
        response, dispatch = self.resend(fax_phone="1" * 21)
        self.assertIn("fax_phone", response.context["form"].errors)
        dispatch.assert_not_called()
        self.assertUnchanged()

    def test_a_failed_fax_is_sent_to_the_new_number(self):
        self.open_followup()
        response, dispatch = self.resend()
        self.assertTemplateUsed(response, "fax_followup_thankyou.html")
        dispatch.assert_called_once()
        fax = FaxesToSend.objects.get(pk=self.fax.pk)
        self.assertEqual(fax.destination, NEW_NUMBER)
        self.assertFalse(fax.sent)

    def test_each_form_re_sends_the_fax_it_was_shown_for(self):
        """Two links opened in one browser, each in its own tab, then the
        first tab's form is sent."""
        other = self.make_fax(destination="15550000000")
        self.open_followup()
        first_tab = self.shown_ref
        self.open_followup(fax=other)
        _, dispatch = self.resend(ref=first_tab)
        dispatch.assert_called_once()
        self.assertEqual(
            FaxesToSend.objects.get(pk=self.fax.pk).destination, NEW_NUMBER
        )
        self.assertUnchanged(fax=other, destination="15550000000")

    def test_the_page_shows_the_fax_of_the_link_opened_last(self):
        other = self.make_fax(destination="15550000000")
        self.open_followup()
        self.open_followup(fax=other)
        page = self.client.get(reverse("fax-followup-page"))
        self.resend(ref=self.ref_on(page))
        self.assertEqual(FaxesToSend.objects.get(pk=other.pk).destination, NEW_NUMBER)
        self.assertUnchanged()

    def test_the_page_says_which_fax_by_the_day_sent_and_number_on_file(self):
        self.staged_ago(timedelta(days=3))
        response = self.open_followup()
        self.assertIn(self.which_fax(), response.content.decode())

    def test_the_fax_number_box_holds_the_number_on_file(self):
        self.assertEqual(self.number_in_box(self.open_followup()), ON_FILE)

    def test_the_page_shows_nothing_from_the_letter(self):
        html = self.open_followup().content.decode()
        for words in ("Dear Insurer", "Pat Example", "cover my treatment"):
            with self.subTest(words=words):
                self.assertNotIn(words, html)

    def test_a_reload_after_two_links_names_the_newer_fax_and_fills_its_number(self):
        """Two links opened in one browser, then the first tab reloaded: the
        page is for the fax of the link opened last, and says so."""
        self.staged_ago(timedelta(days=5))
        other = self.make_fax(destination="15550000000")
        FaxesToSend.objects.filter(pk=other.pk).update(
            date=timezone.now() - timedelta(days=1)
        )
        first_tab = self.open_followup()
        self.assertIn(self.which_fax(), first_tab.content.decode())
        self.open_followup(fax=other)
        reloaded = self.client.get(reverse("fax-followup-page"))
        html = reloaded.content.decode()
        self.assertIn(self.which_fax(other), html)
        self.assertNotIn(self.which_fax(), html)
        self.assertEqual(self.number_in_box(reloaded), "15550000000")

    def test_a_form_that_does_not_validate_still_says_which_fax(self):
        self.open_followup()
        response, _ = self.resend(fax_phone="")
        self.assertIn(self.which_fax(), response.content.decode())

    def test_a_fax_with_no_number_says_none_is_on_file_and_leaves_the_box_empty(
        self,
    ):
        FaxesToSend.objects.filter(pk=self.fax.pk).update(destination=None)
        response = self.open_followup()
        html = response.content.decode()
        self.assertIn(
            f"This is about the fax you asked us to send on {self.day_sent()}. "
            "We don't have a fax number on file for it.",
            html,
        )
        self.assertEqual(self.number_in_box(response), "")

    def test_the_thank_you_page_says_which_fax_it_re_sent(self):
        self.staged_ago(timedelta(days=2))
        self.open_followup()
        response, _ = self.resend()
        self.assertTemplateUsed(response, "fax_followup_thankyou.html")
        self.assertIn(
            f"We're sending your fax from {self.day_sent()} again, to {NEW_NUMBER}.",
            response.content.decode(),
        )

    def test_a_form_whose_ref_this_session_does_not_hold_sends_nothing(self):
        another_browser = Client()
        self.open_followup(client=another_browser)
        their_ref = self.shown_ref
        self.open_followup()
        for ref in (None, "", "not-a-ref", their_ref):
            with self.subTest(ref=ref):
                data = {"fax_phone": NEW_NUMBER}
                if ref is not None:
                    data["fax_ref"] = ref
                with patch(DISPATCH) as dispatch:
                    response = self.client.post(reverse("fax-followup-page"), data)
                self.assertEqual(response.status_code, 404)
                self.assertIn("We couldn't find your fax", response.content.decode())
                dispatch.assert_not_called()
                self.assertUnchanged()

    def test_a_fax_with_no_number_can_be_given_one(self):
        FaxesToSend.objects.filter(pk=self.fax.pk).update(destination=None)
        self.open_followup()
        _, dispatch = self.resend()
        dispatch.assert_called_once()
        self.assertEqual(
            FaxesToSend.objects.get(pk=self.fax.pk).destination, NEW_NUMBER
        )

    def test_a_delivered_fax_says_it_went_through_in_place_of_the_form(self):
        self.deliver()
        response = self.open_followup()
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Your fax went through", html)
        self.assertIn("support42@fighthealthinsurance.com", html)
        self.assertNotIn('name="fax_phone"', html)

    def test_a_delivered_fax_stays_sent(self):
        self.open_followup()
        self.deliver()
        response, dispatch = self.resend()
        self.assertIn("Your fax went through", response.content.decode())
        dispatch.assert_not_called()
        self.assertUnchanged()
        self.assertTrue(FaxesToSend.objects.get(pk=self.fax.pk).fax_success)

    def test_a_fax_past_the_window_says_the_link_no_longer_works(self):
        self.staged_ago(FAX_RESEND_WINDOW + timedelta(days=1))
        response = self.open_followup()
        self.assertEqual(response.status_code, 404)
        html = response.content.decode()
        self.assertIn("This link no longer works", html)
        self.assertIn(f"works for {FAX_RESEND_WINDOW.days} days", html)
        self.assertNotIn('name="fax_phone"', html)

    def test_a_fax_past_the_window_is_not_sent_again(self):
        self.staged_ago(FAX_RESEND_WINDOW + timedelta(days=1))
        self.open_followup()
        # The page shows no form, so post the ref the session holds.
        [[ref, _]] = self.client.session[FAX_FOLLOWUP_SESSION_KEY]
        response, dispatch = self.resend(ref=ref)
        self.assertIn("This link no longer works", response.content.decode())
        dispatch.assert_not_called()
        self.assertUnchanged()

    def test_the_window_still_applies_to_a_fax_the_session_holds(self):
        self.open_followup()
        self.staged_ago(FAX_RESEND_WINDOW + timedelta(days=1))
        response, dispatch = self.resend()
        self.assertIn("This link no longer works", response.content.decode())
        dispatch.assert_not_called()
        self.assertUnchanged()

    def test_a_fax_inside_the_window_can_still_be_sent_again(self):
        self.staged_ago(FAX_RESEND_WINDOW - timedelta(days=1))
        self.open_followup()
        _, dispatch = self.resend()
        dispatch.assert_called_once()

    def test_with_no_fax_in_the_session_the_page_says_how_to_open_it(self):
        response = self.client.get(reverse("fax-followup-page"))
        self.assertEqual(response.status_code, 404)
        html = response.content.decode()
        self.assertIn("We couldn't find your fax", html)
        self.assertIn("support42@fighthealthinsurance.com", html)
        self.assertNotIn('name="fax_phone"', html)

    def test_a_post_with_no_fax_in_the_session_sends_nothing(self):
        response, dispatch = self.resend()
        self.assertEqual(response.status_code, 404)
        self.assertIn("We couldn't find your fax", response.content.decode())
        dispatch.assert_not_called()
        self.assertUnchanged()

    def test_a_fax_removed_since_the_link_was_opened_is_not_found(self):
        self.open_followup()
        FaxesToSend.objects.filter(pk=self.fax.pk).delete()
        response = self.client.get(reverse("fax-followup-page"))
        self.assertEqual(response.status_code, 404)
        self.assertIn("We couldn't find your fax", response.content.decode())


class FaxAddressMatchingNoRouteTest(FaxLinkPageTestCase):
    """An address under the fax links' paths that no route takes, such as
    one a mail client changed, still carries the pair."""

    def addresses(self):
        followup = self.followup_link()
        sendfax = self.sendfax_link()
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


class SendFaxLinkTest(FaxLinkPageTestCase):
    """Stripe's success link for a fax payment, and the sent page."""

    def test_sends_the_fax_and_redirects_to_the_sent_page(self):
        FaxesToSend.objects.filter(pk=self.fax.pk).update(paid=False)
        with patch(DISPATCH) as dispatch:
            response = self.client.get(self.sendfax_link())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("fax-sent"))
        self.assertNeverCached(response)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        dispatch.assert_called_once()
        fax = FaxesToSend.objects.get(pk=self.fax.pk)
        self.assertTrue(fax.paid)
        self.assertTrue(fax.should_send)

    def test_the_sent_page_loads_the_analytics_tags_and_holds_no_ids(self):
        with patch(DISPATCH):
            response = self.client.get(self.sendfax_link(), follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse("fax-sent"))
        self.assertTemplateUsed(response, "fax_thankyou.html")
        self.assertIn("Thank you!", response.content.decode())
        self.assertAnalyticsTags(response)
        self.assertNoPair(response.content.decode())

    def test_reloading_the_sent_page_does_not_send_again(self):
        with patch(DISPATCH) as dispatch:
            self.client.get(self.sendfax_link(), follow=True)
            for _ in range(2):
                response = self.client.get(reverse("fax-sent"))
                self.assertIn("Thank you!", response.content.decode())
        dispatch.assert_called_once()

    def test_an_already_delivered_fax_says_so(self):
        self.deliver()
        with patch(DISPATCH) as dispatch:
            response = self.client.get(self.sendfax_link(), follow=True)
        self.assertIn("Already received", response.content.decode())
        dispatch.assert_not_called()
        self.assertEqual(self.client.session[FAX_SENT_SESSION_KEY], "already_sent")

    def test_an_unknown_pair_is_a_404_that_loads_no_third_party_scripts(self):
        for url in (
            self.sendfax_link(hashed_email=Denial.get_hashed_email("x@example.com")),
            self.sendfax_link(fax_uuid=uuid.uuid4()),
        ):
            with self.subTest(url=url):
                with patch(DISPATCH) as dispatch:
                    response = self.client.get(url)
                self.assertEqual(response.status_code, 404)
                dispatch.assert_not_called()
                self.assertNoThirdPartyScripts(response)
                self.assertNeverCached(response)
                self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def test_a_link_past_the_window_sends_nothing(self):
        FaxesToSend.objects.filter(pk=self.fax.pk).update(paid=False)
        self.staged_ago(FAX_RESEND_WINDOW + timedelta(days=1))
        with patch(DISPATCH) as dispatch:
            response = self.client.get(self.sendfax_link(), follow=True)
        dispatch.assert_not_called()
        self.assertFalse(FaxesToSend.objects.get(pk=self.fax.pk).paid)
        self.assertEqual(response.status_code, 404)
        self.assertIn("We couldn't find your fax", response.content.decode())

    def test_with_nothing_in_the_session_the_sent_page_says_where_to_ask(self):
        response = self.client.get(reverse("fax-sent"))
        self.assertEqual(response.status_code, 404)
        html = response.content.decode()
        self.assertIn("We couldn't find your fax", html)
        self.assertIn("support42@fighthealthinsurance.com", html)
        self.assertNotIn("Thank you!", html)

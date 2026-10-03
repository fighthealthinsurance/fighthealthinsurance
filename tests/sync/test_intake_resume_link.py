"""The "you left before finishing" email links back to the case, safely.

The link is a random token whose digest is all the server keeps. It opens
nothing until the person types the email address the case was started with,
it works for 48 hours, and it stops working once the case moves on, the form
is finished, the journey closes or the person deletes their data. While the
intake journey is off nothing here runs at all.
"""

import hashlib
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from asgiref.sync import async_to_sync
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance import intake_resume, views
from fighthealthinsurance.denial_context import health_history_digest
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.intake_resume_views import RESUME_LINK_SESSION_KEY
from fighthealthinsurance.models import (
    Denial,
    IntakeJourneyEvent,
    IntakeResumePoint,
)

EMAIL = "person@example.com"
DENIAL_TEXT = "Coverage for the requested MRI was denied as not medically necessary."

INTAKE_ON = dict(
    TEMPORAL_ENABLED=True,
    TEMPORAL_APPEAL_JOURNEY_ENABLED=True,
    TEMPORAL_INTAKE_JOURNEY_ENABLED=True,
    TEMPORAL_APPEAL_TASK_QUEUE="q-appeal",
)


class IntakeResumeTestBase(TestCase):
    def setUp(self):
        # Turned on per test, inside the sync conftest's fixture that pins
        # every Temporal flag off, so this override is the one that applies.
        intake_on = override_settings(**INTAKE_ON)
        intake_on.enable()
        self.addCleanup(intake_on.disable)
        # The flow's saves hand intake events to Temporal; none is reachable
        # here, and none is needed to test what the views record.
        no_delivery = patch(
            "fighthealthinsurance.intake_outbox.deliver", return_value=False
        )
        no_delivery.start()
        self.addCleanup(no_delivery.stop)
        self.client = Client()
        self.denial = Denial.objects.create(
            denial_text=DENIAL_TEXT,
            hashed_email=Denial.get_hashed_email(EMAIL),
            raw_email=EMAIL,
            semi_sekret="the-case-secret",
        )

    def mint(self, denial=None) -> str:
        return async_to_sync(intake_resume.amint_link)(denial or self.denial)

    def follow_link(self, token: str, client=None):
        return (client or self.client).get(
            reverse("intake_resume_link", args=[token])
        )

    def open_case(self, token: str, email: str = EMAIL, client=None):
        client = client or self.client
        self.follow_link(token, client=client)
        return client.post(reverse("intake_resume"), {"email": email})

    def point(self):
        return IntakeResumePoint.objects.filter(denial=self.denial).first()

    def assertLinkIsDead(self, response, client=None):
        self.assertEqual(response.status_code, 404)
        self.assertTemplateUsed(response, "intake_resume.html")
        self.assertContains(response, "This link no longer works", status_code=404)
        self.assertNotIn("denial_id", (client or self.client).session)


class TheLinkTest(IntakeResumeTestBase):
    def test_only_a_digest_of_the_token_is_stored(self):
        token = self.mint()
        point = self.point()
        self.assertEqual(
            point.token_digest, hashlib.sha256(token.encode("utf-8")).hexdigest()
        )
        self.assertNotIn(token, str(point.__dict__))

    def test_the_link_carries_nothing_about_the_person_or_the_case(self):
        url = reverse("intake_resume_link", args=[self.mint()])
        for secret in (
            EMAIL,
            self.denial.hashed_email,
            str(self.denial.uuid),
            self.denial.semi_sekret,
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, url)
        self.assertNotIn(str(self.denial.denial_id), url.split("/"))
        self.assertNotIn("?", url)

    def test_the_link_takes_the_token_out_of_the_address_bar_before_anything_renders(
        self,
    ):
        response = self.follow_link(self.mint())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("intake_resume"))
        self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def test_the_page_behind_the_link_asks_for_the_email_and_shows_nothing_else(
        self,
    ):
        self.follow_link(self.mint())
        response = self.client.get(reverse("intake_resume"))
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn('name="email"', page)
        for detail in (EMAIL, DENIAL_TEXT, str(self.denial.uuid)):
            with self.subTest(detail=detail):
                self.assertNotIn(detail, page)

    def test_the_link_lives_exactly_as_long_as_the_journey_can_be_resumed(self):
        from fighthealthinsurance.workflows.intake_journey import (
            CLOSE_AFTER,
            NUDGE_AFTER,
        )

        self.assertEqual(intake_resume.RESUME_LINK_TTL, CLOSE_AFTER - NUDGE_AFTER)

    def test_a_link_that_was_never_minted_opens_nothing(self):
        self.follow_link("made-up-token")
        self.assertLinkIsDead(self.client.get(reverse("intake_resume")))


class OpeningTheCaseTest(IntakeResumeTestBase):
    def test_the_right_email_opens_the_case_at_the_step_it_reached(self):
        intake_resume.note_step(self.denial.denial_id, "dvc")
        response = self.open_case(self.mint())
        self.assertEqual(response.status_code, 302)
        target = urlparse(response.url)
        self.assertEqual(target.path, reverse("dvc"))
        self.assertEqual(
            list(parse_qs(target.query)), [views.DENIAL_REF_QUERY_PARAM]
        )
        page = self.client.get(response.url)
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "plan_documents.html")
        self.assertIn(
            f'name="denial_id" value="{self.denial.denial_id}"',
            page.content.decode().replace("\n", " "),
        )

    def test_a_case_with_no_step_recorded_opens_at_the_first_step(self):
        response = self.open_case(self.mint())
        self.assertEqual(urlparse(response.url).path, reverse("hh"))

    def test_the_email_address_is_matched_whatever_its_letter_case(self):
        response = self.open_case(self.mint(), email="Person@Example.COM")
        self.assertEqual(response.status_code, 302)

    def test_opening_a_case_gives_the_browser_a_new_session_key(self):
        token = self.mint()
        self.follow_link(token)
        before = self.client.session.session_key
        self.client.post(reverse("intake_resume"), {"email": EMAIL})
        self.assertNotEqual(self.client.session.session_key, before)

    def test_opening_a_case_binds_this_browser_to_it(self):
        self.open_case(self.mint())
        self.assertEqual(self.client.session["denial_id"], self.denial.denial_id)
        self.assertNotIn(RESUME_LINK_SESSION_KEY, self.client.session)

    def test_the_link_opens_again_on_a_second_device_until_the_case_moves_on(self):
        token = self.mint()
        self.assertEqual(self.open_case(token).status_code, 302)
        self.assertEqual(self.open_case(token, client=Client()).status_code, 302)


class RefusalsTest(IntakeResumeTestBase):
    def test_a_wrong_email_does_not_open_the_case(self):
        response = self.open_case(self.mint(), email="someone.else@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="resume-wrong-email"')
        self.assertNotIn("denial_id", self.client.session)

    def test_five_wrong_emails_revoke_the_link(self):
        token = self.mint()
        for _ in range(5):
            self.open_case(token, email="someone.else@example.com")
        self.assertLinkIsDead(self.open_case(token))

    def test_an_expired_link_does_not_open_the_case(self):
        token = self.mint()
        IntakeResumePoint.objects.filter(denial=self.denial).update(
            token_expires_at=timezone.now() - timezone.timedelta(seconds=1)
        )
        self.assertLinkIsDead(self.open_case(token))

    def test_a_link_reused_after_the_case_moved_on_does_not_open_it(self):
        token = self.mint()
        self.assertEqual(self.open_case(token).status_code, 302)
        # The person carries on from the health history step they landed on.
        self.client.post(
            reverse("hh"),
            {
                "denial_id": self.denial.denial_id,
                "email": EMAIL,
                "semi_sekret": self.denial.semi_sekret,
                "health_history": "",
                "health_history_seen": health_history_digest(
                    "", self.denial.denial_id
                ),
                "health_history_consent": "on",
            },
        )
        self.assertEqual(self.point().step, "dvc")
        laptop = Client()
        self.assertLinkIsDead(self.open_case(token, client=laptop), client=laptop)

    def test_a_link_stops_working_once_the_form_is_completed(self):
        token = self.mint()
        IntakeJourneyEvent.objects.create(
            denial=self.denial, event_type=IntakeJourneyEvent.FORM_COMPLETED
        )
        self.assertLinkIsDead(self.open_case(token))

    def test_a_link_stops_working_once_the_person_deletes_their_data(self):
        token = self.mint()
        RemoveDataHelper.remove_data_for_email(EMAIL)
        self.assertLinkIsDead(self.open_case(token))

    def test_both_resume_pages_are_not_found_while_the_intake_journey_is_off(self):
        token = self.mint()
        with override_settings(TEMPORAL_INTAKE_JOURNEY_ENABLED=False):
            for response in (
                self.follow_link(token),
                self.client.get(reverse("intake_resume")),
                self.client.post(reverse("intake_resume"), {"email": EMAIL}),
            ):
                with self.subTest(url=response.request["PATH_INFO"]):
                    self.assertEqual(response.status_code, 404)
        self.assertNotIn("denial_id", self.client.session)


class StepsTest(IntakeResumeTestBase):
    def test_steps_only_move_forward(self):
        intake_resume.note_step(self.denial.denial_id, "eev")
        intake_resume.note_step(self.denial.denial_id, "hh")
        self.assertEqual(self.point().step, "eev")

    def test_reaching_a_later_step_revokes_the_link(self):
        self.mint()
        intake_resume.note_step(self.denial.denial_id, "categorize_review")
        self.assertIsNone(self.point().token_digest)

    def test_no_step_is_recorded_while_the_intake_journey_is_off(self):
        with override_settings(TEMPORAL_INTAKE_JOURNEY_ENABLED=False):
            intake_resume.note_step(self.denial.denial_id, "dvc")
        self.assertIsNone(self.point())

    def test_walking_the_form_records_the_furthest_step_reached(self):
        self.client.post(
            reverse("process"),
            {
                "email": "walker@example.com",
                "denial_text": DENIAL_TEXT,
                "pii": "on",
                "tos": "on",
                "privacy": "on",
            },
        )
        denial = Denial.objects.get(denial_id=self.client.session["denial_id"])
        self.assertEqual(
            IntakeResumePoint.objects.get(denial=denial).step, "hh"
        )
        self.client.post(
            reverse("hh"),
            {
                "denial_id": denial.denial_id,
                "email": "walker@example.com",
                "semi_sekret": denial.semi_sekret,
                "health_history": "",
                "health_history_seen": health_history_digest("", denial.denial_id),
                "health_history_consent": "on",
            },
        )
        self.assertEqual(
            IntakeResumePoint.objects.get(denial=denial).step, "dvc"
        )

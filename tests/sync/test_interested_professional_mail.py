"""Tests for the bulk "email all interested professionals" staff flow."""

import importlib
from unittest.mock import MagicMock, patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from fighthealthinsurance.mailing_list_actor import dedupe_recipients_by_email
from fighthealthinsurance.models import InterestedProfessional, MailingListSubscriber
from fighthealthinsurance.proconnector import (
    mailable_interested_professionals,
    processable_queryset,
)

User = get_user_model()

SEND_URL = "/timbit/help/send_interested_professional_mail"

# Migration module names start with a digit, so they can't be imported with
# normal import syntax.
backfill_unsubscribe_tokens = importlib.import_module(
    "fighthealthinsurance.migrations."
    "0198_interestedprofessional_unsubscribe_token_and_more"
).backfill_unsubscribe_tokens


class MailableInterestedProfessionalsTest(TestCase):
    """Which professionals the broadcast is allowed to reach."""

    def test_includes_ordinary_signup(self):
        InterestedProfessional.objects.create(email="doc@clinic.com", name="Doc")
        self.assertEqual(
            list(mailable_interested_professionals().values_list("email", flat=True)),
            ["doc@clinic.com"],
        )

    def test_excludes_unsubscribed(self):
        InterestedProfessional.objects.create(
            email="gone@clinic.com", unsubscribed=True
        )
        self.assertEqual(mailable_interested_professionals().count(), 0)

    def test_excludes_spam_tld_signup(self):
        InterestedProfessional.objects.create(email="spam@example.ru")
        self.assertEqual(mailable_interested_professionals().count(), 0)

    def test_includes_already_introduced_professional(self):
        """A broadcast is independent of pro-connector state, unlike the queue."""
        InterestedProfessional.objects.create(
            email="introduced@clinic.com", proconnector_attempted=True
        )
        self.assertEqual(mailable_interested_professionals().count(), 1)
        self.assertEqual(processable_queryset().count(), 0)


class ProcessableQuerysetUnsubscribeTest(TestCase):
    """Opting out also removes someone from the pro-connector intro queue."""

    def test_unsubscribed_professional_is_not_processable(self):
        InterestedProfessional.objects.create(
            email="optout@clinic.com", unsubscribed=True
        )
        self.assertEqual(processable_queryset().count(), 0)

    def test_subscribed_professional_is_processable(self):
        InterestedProfessional.objects.create(email="ok@clinic.com")
        self.assertEqual(processable_queryset().count(), 1)


class DedupeRecipientsTest(TestCase):
    """Duplicate signups must be mailed exactly once."""

    def test_dedupes_case_insensitively_keeping_first(self):
        result = dedupe_recipients_by_email(
            [("Doc@Clinic.com", "url-a"), ("doc@clinic.com", "url-b")]
        )
        self.assertEqual(result, [("Doc@Clinic.com", "url-a")])

    def test_keeps_distinct_addresses(self):
        result = dedupe_recipients_by_email(
            [("a@clinic.com", None), ("b@clinic.com", None)]
        )
        self.assertEqual(len(result), 2)

    def test_drops_empty_addresses(self):
        self.assertEqual(dedupe_recipients_by_email([("", "url")]), [])


class InterestedProfessionalUnsubscribeTokenTest(TestCase):
    """Every professional gets their own working unsubscribe link."""

    def test_tokens_are_unique_per_record(self):
        first = InterestedProfessional.objects.create(email="one@clinic.com")
        second = InterestedProfessional.objects.create(email="two@clinic.com")
        self.assertNotEqual(first.unsubscribe_token, second.unsubscribe_token)

    def test_unsubscribe_url_points_at_the_unsubscribe_route(self):
        pro = InterestedProfessional.objects.create(email="url@clinic.com")
        url = pro.get_unsubscribe_url()
        self.assertIn("https://www.fighthealthinsurance.com", url)
        self.assertIn("v0/unsubscribe", url)
        self.assertIn(pro.unsubscribe_token, url)

    def test_unsubscribe_flags_record_without_deleting_it(self):
        """The row carries pro-connector state, so it is flagged, not deleted."""
        pro = InterestedProfessional.objects.create(
            email="flagme@clinic.com", proconnector_skip_reason="not a fit"
        )
        response = self.client.get(
            reverse("unsubscribe", kwargs={"token": pro.unsubscribe_token})
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "flagme@clinic.com")
        pro.refresh_from_db()
        self.assertTrue(pro.unsubscribed)
        self.assertIsNotNone(pro.unsubscribed_at)
        self.assertEqual(pro.proconnector_skip_reason, "not a fit")

    def test_unsubscribe_also_removes_matching_mailing_list_subscriber(self):
        """One opt-out click stops marketing mail from every list."""
        pro = InterestedProfessional.objects.create(email="both@clinic.com")
        MailingListSubscriber.objects.create(email="Both@Clinic.com")

        self.client.get(reverse("unsubscribe", kwargs={"token": pro.unsubscribe_token}))

        self.assertEqual(
            MailingListSubscriber.objects.filter(
                email__iexact="both@clinic.com"
            ).count(),
            0,
        )

    def test_subscriber_token_also_unsubscribes_professional_record(self):
        """The reverse direction: a newsletter link opts them out as a pro too."""
        subscriber = MailingListSubscriber.objects.create(email="both2@clinic.com")
        pro = InterestedProfessional.objects.create(email="Both2@Clinic.com")

        self.client.get(
            reverse("unsubscribe", kwargs={"token": subscriber.unsubscribe_token})
        )

        pro.refresh_from_db()
        self.assertTrue(pro.unsubscribed)

    def test_duplicate_token_does_not_error(self):
        """A shared token resolves rather than raising MultipleObjectsReturned."""
        first = InterestedProfessional.objects.create(email="dup1@clinic.com")
        InterestedProfessional.objects.create(
            email="dup2@clinic.com", unsubscribe_token=first.unsubscribe_token
        )

        response = self.client.get(
            reverse("unsubscribe", kwargs={"token": first.unsubscribe_token})
        )

        self.assertEqual(response.status_code, 200)


class UnsubscribeTokenBackfillTest(TestCase):
    """The 0198 data migration that repairs the AddField default.

    ``AddField`` with a callable default evaluates it once and writes the same
    value to every existing row, so the migration follows up with a backfill.
    These tests reproduce that starting state -- rows sharing one token -- and
    check the backfill separates them.
    """

    def test_backfill_gives_each_row_a_distinct_token(self):
        shared = "shared-token-from-addfield-default"
        for i in range(5):
            InterestedProfessional.objects.create(
                email=f"pro{i}@clinic.com", unsubscribe_token=shared
            )

        backfill_unsubscribe_tokens(django_apps, None)

        tokens = list(
            InterestedProfessional.objects.values_list("unsubscribe_token", flat=True)
        )
        self.assertEqual(len(tokens), 5)
        self.assertEqual(len(set(tokens)), 5)
        self.assertNotIn(shared, tokens)

    def test_backfilled_tokens_resolve_to_their_own_record(self):
        """The point of the backfill: a link unsubscribes only its own owner."""
        shared = "shared-token-from-addfield-default"
        first = InterestedProfessional.objects.create(
            email="first@clinic.com", unsubscribe_token=shared
        )
        second = InterestedProfessional.objects.create(
            email="second@clinic.com", unsubscribe_token=shared
        )

        backfill_unsubscribe_tokens(django_apps, None)
        first.refresh_from_db()
        second.refresh_from_db()

        self.client.get(
            reverse("unsubscribe", kwargs={"token": first.unsubscribe_token})
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(first.unsubscribed)
        self.assertFalse(second.unsubscribed)

    def test_backfill_on_empty_table_is_a_noop(self):
        backfill_unsubscribe_tokens(django_apps, None)
        self.assertEqual(InterestedProfessional.objects.count(), 0)


class SendInterestedProfessionalMailViewTest(TestCase):
    """Access control and send behavior for the staff broadcast page."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.client = Client()
        self.staff_user = User.objects.create_user(
            username="staffuser",
            password="testpass123",
            email="staff@example.com",
            is_staff=True,
        )
        self.regular_user = User.objects.create_user(
            username="regularuser",
            password="testpass123",
            email="regular@example.com",
            is_staff=False,
        )
        self.form_data = {
            "subject": "Test Subject",
            "html_content": "<p>Test HTML</p>",
            "text_content": "Test text",
        }

    def test_access_denied_for_anonymous_user(self):
        response = self.client.get(SEND_URL)
        self.assertIn(response.status_code, [302, 403])

    def test_access_denied_for_regular_user(self):
        self.client.login(username="regularuser", password="testpass123")
        response = self.client.get(SEND_URL)
        self.assertIn(response.status_code, [302, 403])

    def test_access_granted_for_staff_user(self):
        self.client.login(username="staffuser", password="testpass123")
        response = self.client.get(SEND_URL)
        self.assertEqual(response.status_code, 200)

    def test_page_shows_distinct_recipient_count(self):
        """Duplicate signups count once, matching what actually gets sent."""
        InterestedProfessional.objects.create(email="dupe@clinic.com")
        InterestedProfessional.objects.create(email="DUPE@clinic.com")
        InterestedProfessional.objects.create(email="other@clinic.com")
        InterestedProfessional.objects.create(
            email="optout@clinic.com", unsubscribed=True
        )

        self.client.login(username="staffuser", password="testpass123")
        response = self.client.get(SEND_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["recipient_count"], 2)

    @patch("fighthealthinsurance.staff_views.ray_cluster_available", return_value=True)
    @patch("fighthealthinsurance.staff_views.mailing_list_actor_ref")
    @patch("fighthealthinsurance.staff_views.ray")
    def test_send_dispatches_to_the_professional_actor_method(
        self, mock_ray, mock_actor_ref, mock_cluster_available
    ):
        """The professionals page must not send to the mailing list."""
        mock_actor = MagicMock()
        mock_actor_ref.get = mock_actor
        mock_ray.get.return_value = (3, 1, 2)

        self.client.login(username="staffuser", password="testpass123")
        response = self.client.post(SEND_URL, data=self.form_data)

        self.assertEqual(response.status_code, 200)
        mock_actor.send_interested_professional_email.remote.assert_called_once_with(
            "Test Subject", "<p>Test HTML</p>", "Test text", ""
        )
        mock_actor.send_mailing_list_email.remote.assert_not_called()
        self.assertContains(response, "Success: 3")
        self.assertContains(response, "Failed: 1")
        self.assertContains(response, "Blocked: 2")

    @patch("fighthealthinsurance.staff_views.ray_cluster_available", return_value=True)
    @patch("fighthealthinsurance.staff_views.mailing_list_actor_ref")
    @patch("fighthealthinsurance.staff_views.ray")
    def test_test_email_reports_test_send(
        self, mock_ray, mock_actor_ref, mock_cluster_available
    ):
        mock_actor = MagicMock()
        mock_actor_ref.get = mock_actor
        mock_ray.get.return_value = (1, 0, 0)

        self.client.login(username="staffuser", password="testpass123")
        response = self.client.post(
            SEND_URL, data={**self.form_data, "test_email": "staff@example.com"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test email sent successfully")

    @patch("fighthealthinsurance.staff_views.ray_cluster_available", return_value=False)
    def test_send_without_ray_cluster_returns_503(self, mock_cluster_available):
        self.client.login(username="staffuser", password="testpass123")
        response = self.client.post(SEND_URL, data=self.form_data)
        self.assertEqual(response.status_code, 503)

    @patch("fighthealthinsurance.staff_views.ray_cluster_available", return_value=True)
    @patch("fighthealthinsurance.staff_views.mailing_list_actor_ref")
    @patch("fighthealthinsurance.staff_views.ray")
    def test_actor_failure_returns_500(
        self, mock_ray, mock_actor_ref, mock_cluster_available
    ):
        mock_actor = MagicMock()
        mock_actor_ref.get = mock_actor
        mock_ray.get.side_effect = RuntimeError("actor died at smtp.internal.host")

        self.client.login(username="staffuser", password="testpass123")
        response = self.client.post(SEND_URL, data=self.form_data)

        self.assertEqual(response.status_code, 500)
        # The exception text (which can carry hostnames/addresses) must stay in
        # the logs, not the HTTP response.
        self.assertNotContains(response, "smtp.internal.host", status_code=500)

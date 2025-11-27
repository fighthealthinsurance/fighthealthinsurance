"""Test mailing list subscription during appeal flow."""

from django.test import TestCase, Client
from django.urls import reverse
from fighthealthinsurance.models import MailingListSubscriber


class MailingListSubscriptionTest(TestCase):
    """Test that mailing list subscription works during appeal flow."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.client = Client()

    def test_subscribe_checkbox_in_form(self):
        """Test that the subscribe checkbox exists in the form."""
        from fighthealthinsurance.forms import DenialForm

        form = DenialForm()
        self.assertIn("subscribe", form.fields)
        self.assertTrue(form.fields["subscribe"].initial)
        self.assertFalse(form.fields["subscribe"].required)

    def test_submission_creates_mailing_list_subscriber(self):
        """Test that submitting with subscribe=True creates a MailingListSubscriber."""
        # Initial count
        initial_count = MailingListSubscriber.objects.count()

        # Submit form data
        response = self.client.post(
            reverse("process"),
            {
                "email": "test@example.com",
                "denial_text": "Your claim has been denied.",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "subscribe": "on",
                "fname": "John",
                "lname": "Doe",
            },
            follow=True,
        )

        # Check that we're redirected to the next step (health_history)
        self.assertEqual(response.status_code, 200)

        # Check that a new subscriber was created
        new_count = MailingListSubscriber.objects.count()
        self.assertEqual(new_count, initial_count + 1)

        # Verify the subscriber data
        subscriber = MailingListSubscriber.objects.latest("id")
        self.assertEqual(subscriber.email, "test@example.com")
        self.assertEqual(subscriber.name, "John Doe")
        self.assertEqual(subscriber.comments, "From appeal flow")

    def test_submission_without_subscribe_does_not_create_subscriber(self):
        """Test that submitting without subscribe does not create a MailingListSubscriber."""
        # Initial count
        initial_count = MailingListSubscriber.objects.count()

        # Submit form data without subscribe
        response = self.client.post(
            reverse("process"),
            {
                "email": "nosubscribe@example.com",
                "denial_text": "Your claim has been denied.",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                # subscribe not included (unchecked)
                "fname": "Jane",
                "lname": "Smith",
            },
            follow=True,
        )

        # Check that we're redirected to the next step
        self.assertEqual(response.status_code, 200)

        # Check that no new subscriber was created
        new_count = MailingListSubscriber.objects.count()
        self.assertEqual(new_count, initial_count)

        # Verify no subscriber with this email was created
        self.assertFalse(
            MailingListSubscriber.objects.filter(
                email="nosubscribe@example.com"
            ).exists()
        )

    def test_duplicate_subscription_prevention(self):
        """Test that submitting multiple times with same email doesn't create duplicates."""
        # Create an initial subscriber
        MailingListSubscriber.objects.create(
            email="existing@example.com",
            name="Existing User",
            comments="Previously subscribed",
        )
        initial_count = MailingListSubscriber.objects.count()

        # Submit form data with the same email
        response = self.client.post(
            reverse("process"),
            {
                "email": "existing@example.com",
                "denial_text": "Your claim has been denied.",
                "pii": "on",
                "tos": "on",
                "privacy": "on",
                "subscribe": "on",
                "fname": "New",
                "lname": "Name",
            },
            follow=True,
        )

        # Check that we're redirected to the next step
        self.assertEqual(response.status_code, 200)

        # Check that no new subscriber was created (duplicate prevention)
        new_count = MailingListSubscriber.objects.count()
        self.assertEqual(new_count, initial_count)

        # Verify the original subscriber data is unchanged
        subscriber = MailingListSubscriber.objects.get(email="existing@example.com")
        self.assertEqual(subscriber.name, "Existing User")
        self.assertEqual(subscriber.comments, "Previously subscribed")

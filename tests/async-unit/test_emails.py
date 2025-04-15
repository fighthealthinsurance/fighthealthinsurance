import pytest
from django.core import mail
from fhi_users.emails import (
    send_provider_started_appeal_email,
    send_password_reset_email,
    send_email_confirmation,
    send_appeal_submitted_successfully_email,
    send_error_submitting_appeal_email,
    send_verification_email,
)
from django.contrib.auth.models import User
from django.test import RequestFactory


@pytest.fixture
def context():
    return {"example_key": "example_value"}


@pytest.fixture
def test_user():
    return User.objects.create_user(
        username="testuser", email="testuser@example.com", password="testpassword"
    )


@pytest.fixture
def test_request():
    return RequestFactory().get("/")


@pytest.mark.django_db
class TestEmails:

    def test_send_provider_started_appeal_email(self, context):
        send_provider_started_appeal_email("patient@example.com", context)
        assert len(mail.outbox) == 2
        assert mail.outbox[0].subject == "Provider Started Appeal"

    def test_send_password_reset_email(self, context):
        send_password_reset_email("user@example.com", context)
        assert len(mail.outbox) == 2
        assert mail.outbox[0].subject == "Reset your password"

    def test_send_email_confirmation(self, context):
        send_email_confirmation("user@example.com", context)
        assert len(mail.outbox) == 2
        assert mail.outbox[0].subject == "Email Confirmation"

    def test_send_appeal_submitted_successfully_email(self, context):
        send_appeal_submitted_successfully_email("user@example.com", context)
        assert len(mail.outbox) == 2
        assert mail.outbox[0].subject == "Appeal Submitted Successfully"

    def test_send_error_submitting_appeal_email(self, context):
        send_error_submitting_appeal_email("user@example.com", context)
        assert len(mail.outbox) == 2
        assert mail.outbox[0].subject == "Error Submitting Appeal"

    def test_send_verification_email(self, test_request, test_user):
        send_verification_email(test_request, test_user)
        assert len(mail.outbox) == 2
        assert mail.outbox[0].subject == "Activate your account."

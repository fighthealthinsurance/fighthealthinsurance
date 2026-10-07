"""With FIGHT_PAPERWORK_ENABLED off, Fight Paperwork's account endpoints refuse.

Each refused route answers 404 with a short JSON error, sends no email and
creates no user, domain, token or Stripe checkout.
"""

from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import URLPattern, reverse
from rest_framework.test import APIClient

from fhi_users import urls as fhi_users_urls
from fhi_users.fight_paperwork import UNAVAILABLE_MESSAGE
from fhi_users.models import (
    ExtraUserProperties,
    PatientUser,
    ProfessionalDomainRelation,
    ProfessionalUser,
    ResetToken,
    UserDomain,
    VerificationToken,
)
from fighthealthinsurance.models import LostStripeSession

User = get_user_model()

SIGNUP = {
    "user_signup_info": {
        "username": "offpro",
        "password": "newLongerPasswordMagicCheetoCheeto123",
        "email": "offpro@test-fhi.com",
        "first_name": "Off",
        "last_name": "Pro",
        "domain_name": "offdomain",
        "visible_phone_number": "1234567899",
        "continue_url": "http://example.com/continue",
    },
    "make_new_domain": True,
    "skip_stripe": True,
    "user_domain": {
        "name": "offdomain",
        "visible_phone_number": "1234567899",
        "internal_phone_number": "0987654399",
        "display_name": "Off Domain",
        "country": "USA",
        "state": "CA",
        "city": "Test City",
        "address1": "123 Test St",
        "zipcode": "12345",
    },
}


def test_fight_paperwork_is_off_by_default():
    assert settings.FIGHT_PAPERWORK_ENABLED is False


def test_every_auth_router_route_is_gated():
    router_include = fhi_users_urls.urlpatterns[0]
    patterns = [p for p in router_include.url_patterns if isinstance(p, URLPattern)]
    assert patterns
    for pattern in patterns:
        assert hasattr(pattern.callback, "__wrapped__"), pattern.name


@override_settings(FIGHT_PAPERWORK_ENABLED=False)
class FightPaperworkOffTests(TestCase):
    def setUp(self) -> None:
        self.client = APIClient()
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )
        self.password = "adminpass123"
        self.user = User.objects.create_user(
            username=f"adminuser🐼{self.domain.id}",
            password=self.password,
            email="admin@test-fhi.com",
            first_name="Admin",
            last_name="User",
        )
        self.professional = ProfessionalUser.objects.create(user=self.user, active=True)
        ProfessionalDomainRelation.objects.create(
            professional=self.professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=True,
            pending_domain_relation=False,
        )
        ExtraUserProperties.objects.create(user=self.user, email_verified=True)
        self.users_before = User.objects.count()
        self.domains_before = UserDomain.objects.count()

    def log_in_as_admin(self) -> None:
        self.client.force_authenticate(user=self.user)
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

    def assert_refused(self, response) -> None:
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": UNAVAILABLE_MESSAGE})
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(User.objects.count(), self.users_before)
        self.assertEqual(UserDomain.objects.count(), self.domains_before)

    def test_professional_signup_is_refused(self) -> None:
        response = self.client.post(
            reverse("professional_user-list"), SIGNUP, format="json"
        )
        self.assert_refused(response)

    def test_create_professional_in_domain_is_refused(self) -> None:
        self.log_in_as_admin()
        response = self.client.post(
            reverse("professional_user-create-professional-in-current-domain"),
            {
                "email": "created@test-fhi.com",
                "first_name": "New",
                "last_name": "Pro",
            },
            format="json",
        )
        self.assert_refused(response)

    def test_coworker_invite_is_refused(self) -> None:
        self.log_in_as_admin()
        response = self.client.post(
            reverse("professional_user-invite"),
            {"user_email": "invitee@test-fhi.com", "name": "Invitee"},
            format="json",
        )
        self.assert_refused(response)

    @patch("stripe.checkout.Session.create")
    def test_professional_checkout_is_refused(self, mock_create) -> None:
        response = self.client.post(
            reverse("professional_user-finish-payment"),
            {
                "domain_id": str(self.domain.id),
                "professional_user_id": str(self.professional.id),
            },
            format="json",
        )
        self.assert_refused(response)
        mock_create.assert_not_called()

    def test_login_of_inactive_user_is_refused(self) -> None:
        self.user.is_active = False
        self.user.save()
        response = self.client.post(
            reverse("rest_login-login"),
            {
                "username": "adminuser",
                "password": self.password,
                "domain": "testdomain",
                "phone": "",
            },
            format="json",
        )
        self.assert_refused(response)
        self.assertFalse(VerificationToken.objects.exists())

    def test_patient_signup_is_refused(self) -> None:
        response = self.client.post(
            reverse("patient_user-list"),
            {
                "username": "offpatient",
                "password": "newLongerPasswordMagicCheetoCheeto123",
                "email": "offpatient@test-fhi.com",
                "provider_phone_number": "1234567890",
                "country": "USA",
                "state": "CA",
                "city": "Test City",
                "address1": "123 Test St",
                "zipcode": "12345",
                "domain_name": "testdomain",
            },
            format="json",
        )
        self.assert_refused(response)
        self.assertFalse(PatientUser.objects.exists())

    def test_pending_patient_creation_is_refused(self) -> None:
        self.log_in_as_admin()
        response = self.client.post(
            reverse("patient_user-get-or-create-pending"),
            {"username": "pending@test-fhi.com", "first_name": "P", "last_name": "Q"},
            format="json",
        )
        self.assert_refused(response)

    def test_verification_resend_is_refused(self) -> None:
        response = self.client.post(
            reverse("rest_verify_email-resend"),
            {"user_id": self.user.id, "token": "unused"},
            format="json",
        )
        self.assert_refused(response)

    def test_password_reset_request_is_refused(self) -> None:
        response = self.client.post(
            reverse("password_reset-request-reset"),
            {"username": "adminuser", "domain": "testdomain", "phone": ""},
            format="json",
        )
        self.assert_refused(response)
        self.assertFalse(ResetToken.objects.exists())

    def test_professional_routes_answer_again_when_on(self) -> None:
        with self.settings(FIGHT_PAPERWORK_ENABLED=True):
            response = self.client.post(
                reverse("professional_user-list"), SIGNUP, format="json"
            )
        self.assertNotEqual(response.status_code, 404)
        self.assertTrue(User.objects.filter(email="offpro@test-fhi.com").exists())


def _stripe_event(payment_type: str, event_type: str = "checkout.session.completed"):
    session = SimpleNamespace(
        metadata={
            "payment_type": payment_type,
            "domain_id": "1",
            "professional_id": "1",
        },
        subscription="sub_123",
        customer="cus_123",
        customer_email="buyer@test-fhi.com",
        id="cs_123",
    )
    return SimpleNamespace(
        id="evt_123", type=event_type, data=SimpleNamespace(object=session)
    )


@override_settings(FIGHT_PAPERWORK_ENABLED=False)
class FightPaperworkStripeOffTests(TestCase):
    @patch(
        "fighthealthinsurance.views.StripeWebhookHelper.handle_stripe_webhook",
    )
    @patch("stripe.Webhook.construct_event")
    def test_professional_webhooks_are_logged_and_ignored(
        self, mock_construct, mock_handle
    ) -> None:
        for event_type in ("checkout.session.completed", "checkout.session.expired"):
            mock_construct.return_value = _stripe_event(
                "professional_domain_subscription", event_type
            )
            response = self.client.post(
                reverse("stripe-webhook"),
                data="{}",
                content_type="application/json",
                HTTP_STRIPE_SIGNATURE="sig",
            )
            self.assertEqual(response.status_code, 200)
        mock_handle.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(LostStripeSession.objects.exists())

    @patch(
        "fighthealthinsurance.views.StripeWebhookHelper.handle_stripe_webhook",
    )
    @patch("stripe.Webhook.construct_event")
    def test_fax_webhooks_are_still_handled(self, mock_construct, mock_handle) -> None:
        mock_construct.return_value = _stripe_event("fax")
        response = self.client.post(
            reverse("stripe-webhook"),
            data="{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig",
        )
        self.assertEqual(response.status_code, 200)
        mock_handle.assert_called_once()

    @patch("stripe.checkout.Session.create")
    def test_professional_checkout_cannot_be_resumed(self, mock_create) -> None:
        lost = LostStripeSession.objects.create(
            session_id="cs_lost",
            payment_type="professional_domain_subscription",
            email="buyer@test-fhi.com",
            metadata={"recovery_info_id": "1"},
        )
        response = self.client.get(
            f"{reverse('complete_payment')}?format=json&token={lost.secure_token}"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": UNAVAILABLE_MESSAGE})
        mock_create.assert_not_called()

    @patch("stripe.checkout.Session.create")
    def test_professional_recovery_link_answers_json_404(self, mock_create) -> None:
        lost = LostStripeSession.objects.create(
            session_id="cs_lost_browser",
            payment_type="professional_domain_subscription",
            email="buyer@test-fhi.com",
            metadata={"recovery_info_id": "1"},
        )
        response = self.client.get(
            f"{reverse('complete_payment')}?token={lost.secure_token}"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"error": UNAVAILABLE_MESSAGE})
        mock_create.assert_not_called()

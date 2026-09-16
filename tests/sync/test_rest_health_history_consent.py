"""An API caller can still revoke consent to include the health history.

The HTML page renders no checkbox for either consent flag, and an absent
BooleanField cleans to False, so declaring them on the form made every Next
silently revoke whatever the person had chosen. Taking them off the form
fixed that and broke the API at the same time: the serializer is built from
the same form, so an explicit `false` from a caller was dropped and the
endpoint answered 201 without revoking anything.

The flags live on the serializer now. A caller who sends one is obeyed; a
caller who sends nothing leaves it alone.
"""

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from fighthealthinsurance.models import Denial


class RestHealthHistoryConsentTest(TestCase):
    EMAIL = "someone@example.com"

    def setUp(self):
        self.client = APIClient()
        self.denial = Denial.objects.create(
            denial_text="a denial",
            hashed_email=Denial.get_hashed_email(self.EMAIL),
            include_provided_health_history_in_appeal=True,
        )

    def _post(self, **extra):
        payload = {
            "email": self.EMAIL,
            "denial_id": self.denial.denial_id,
            "semi_sekret": self.denial.semi_sekret,
            "health_history": "some history",
        }
        payload.update(extra)
        return self.client.post(reverse("healthhistory-list"), payload, format="json")

    def test_an_explicit_false_revokes_the_consent(self) -> None:
        response = self._post(include_provided_health_history_in_appeal=False)
        self.assertIn(response.status_code, (200, 201), response.content[:200])
        self.denial.refresh_from_db()
        self.assertFalse(
            self.denial.include_provided_health_history_in_appeal,
            "the endpoint answered success without revoking the consent it was "
            "explicitly told to revoke",
        )

    def test_sending_nothing_leaves_the_consent_alone(self) -> None:
        response = self._post()
        self.assertIn(response.status_code, (200, 201), response.content[:200])
        self.denial.refresh_from_db()
        self.assertTrue(self.denial.include_provided_health_history_in_appeal)

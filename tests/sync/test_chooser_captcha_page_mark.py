"""The Appeal Arena page tells the app when this session has already passed
the "I'm not a robot" check, so a reload asks for no second tick."""

from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance.rest_views import ChooserViewSet

CAPTCHA_ON = dict(
    RECAPTCHA_TESTING=False,
    RECAPTCHA_PUBLIC_KEY="test-public-key",
    RECAPTCHA_PRIVATE_KEY="test-private-key",
)


@override_settings(**CAPTCHA_ON)
class ArenaPageCarriesTheSessionMarkTest(TestCase):
    def _page(self) -> str:
        with mock.patch("fighthealthinsurance.chooser_tasks.trigger_prefill_async"):
            response = self.client.get(reverse("chooser"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_a_fresh_session_is_not_marked(self):
        self.assertNotIn("data-captcha-passed", self._page())

    def test_a_session_that_passed_is_marked(self):
        session = self.client.session
        session[ChooserViewSet.CAPTCHA_SESSION_KEY] = True
        session.save()

        self.assertIn('data-captcha-passed="1"', self._page())

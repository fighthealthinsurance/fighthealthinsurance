"""The Appeal Arena page hands the app the reCAPTCHA site key only while
the captcha is enforced, on the same terms as the site's other forms."""

from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

CAPTCHA_ON = dict(
    RECAPTCHA_TESTING=False,
    RECAPTCHA_PUBLIC_KEY="test-public-key",
    RECAPTCHA_PRIVATE_KEY="test-private-key",
)


class ArenaCaptchaPageTest(TestCase):
    def _page(self) -> str:
        # The page load kicks off a background prefill thread that writes to
        # the database; a test must not leave one running.
        with mock.patch("fighthealthinsurance.chooser_tasks.trigger_prefill_async"):
            response = self.client.get(reverse("chooser"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_in_testing_mode_the_page_carries_no_site_key_or_script(self):
        body = self._page()
        self.assertIn('<div id="chooser-root"></div>', body)
        self.assertNotIn("recaptcha/api.js", body)

    @override_settings(**CAPTCHA_ON)
    def test_when_enforced_the_page_carries_the_site_key_and_the_script(self):
        body = self._page()
        self.assertIn(
            '<div id="chooser-root" data-recaptcha-site-key="test-public-key"></div>',
            body,
        )
        self.assertIn(
            '<script src="https://www.google.com/recaptcha/api.js?render=explicit" async defer></script>',
            body,
        )
        self.assertNotIn("test-private-key", body)

    @override_settings(RECAPTCHA_TESTING=False, RECAPTCHA_PUBLIC_KEY="pub", RECAPTCHA_PRIVATE_KEY="")
    def test_one_key_alone_does_not_enforce_it(self):
        body = self._page()
        self.assertIn('<div id="chooser-root"></div>', body)

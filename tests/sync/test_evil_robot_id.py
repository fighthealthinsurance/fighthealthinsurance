"""Tests for /evil/robot-id, where the X-Warning header sends pen testers."""

from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance import pen_test_permission
from fighthealthinsurance.constants import FHI_SUPPORT_EMAIL

SECRET = "not-a-real-secret"

LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "evil-robot-id-tests",
    }
}


class EvilRobotIdTest(TestCase):
    def setUp(self):
        self.url = reverse("evil_robot_id")

    def _logged(self, **extra):
        with patch.object(pen_test_permission.logger, "warning") as warning:
            self.client.get(self.url, **extra)
        (template, detail), _ = warning.call_args
        return template.format(detail)

    def test_lives_at_the_path_the_warning_names(self):
        self.assertEqual(self.url, "/evil/robot-id")

    def test_get_is_answered(self):
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_post_without_a_csrf_token_is_answered(self):
        client = self.client_class(enforce_csrf_checks=True)
        self.assertEqual(client.post(self.url, {"a": "b"}).status_code, 200)

    def test_response_says_no_permission_was_granted(self):
        self.assertContains(self.client.get(self.url), "do NOT have permission")

    def test_response_is_not_cached(self):
        self.assertIn("no-store", self.client.get(self.url)["Cache-Control"])

    def test_request_is_logged_with_its_user_agent(self):
        self.assertIn("EvilBot/1.0", self._logged(HTTP_USER_AGENT="EvilBot/1.0"))

    def test_request_is_logged_with_its_cloudflare_address(self):
        logged = self._logged(HTTP_CF_CONNECTING_IP="203.0.113.5")
        self.assertIn("203.0.113.5", logged)

    def test_newline_in_a_header_cannot_start_a_new_log_line(self):
        logged = self._logged(HTTP_REFERER="x\nevil robot-id request: forged")
        self.assertNotIn("\n", logged)

    def test_long_header_is_cut_short_in_the_log(self):
        logged = self._logged(HTTP_USER_AGENT="A" * 5000)
        self.assertNotIn("A" * (pen_test_permission._MAX_VALUE_CHARS + 1), logged)

    def test_request_emails_the_support_alias(self):
        self.client.get(self.url)
        self.assertEqual([m.to for m in mail.outbox], [[FHI_SUPPORT_EMAIL]])

    def test_email_carries_the_user_agent(self):
        self.client.get(self.url, HTTP_USER_AGENT="EvilBot/1.0")
        self.assertIn("EvilBot/1.0", mail.outbox[0].body)

    @override_settings(EVIL_SECRET=SECRET)
    def test_valid_evil_header_sends_no_email(self):
        self.client.get(self.url, headers={"X-Evil": SECRET})
        self.assertEqual(mail.outbox, [])

    @override_settings(EVIL_SECRET=SECRET)
    def test_valid_evil_header_is_still_logged(self):
        logged = self._logged(headers={"X-Evil": SECRET}, HTTP_USER_AGENT="Tester/1")
        self.assertIn("Tester/1", logged)

    @override_settings(EVIL_SECRET=SECRET)
    def test_log_never_carries_the_secret(self):
        self.assertNotIn(SECRET, self._logged(headers={"X-Evil": SECRET}))

    @override_settings(EVIL_SECRET=SECRET)
    def test_wrong_evil_header_still_sends_the_email(self):
        self.client.get(self.url, headers={"X-Evil": SECRET + "x"})
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(EVIL_SECRET="")
    def test_no_configured_secret_still_sends_the_email(self):
        self.client.get(self.url, headers={"X-Evil": "anything"})
        self.assertEqual(len(mail.outbox), 1)

    def test_failed_email_still_answers_the_request(self):
        with patch.object(
            pen_test_permission, "send_mail", side_effect=OSError("smtp down")
        ):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    @override_settings(CACHES=LOCMEM_CACHE)
    def test_repeat_requests_send_one_email(self):
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)
        for _ in range(3):
            self.client.get(self.url)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(CACHES=LOCMEM_CACHE)
    def test_repeat_requests_are_all_logged(self):
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)
        with patch.object(pen_test_permission.logger, "warning") as warning:
            for _ in range(3):
                self.client.get(self.url)
        self.assertEqual(warning.call_count, 3)

"""The staff Experiments page (ExperimentsView): staff only, lists every
experimental flag with its state, and never shows a key's value."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance.experiments import EXPERIMENTS

User = get_user_model()


class ExperimentsAccessTest(TestCase):
    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("staff_experiments"))
        self.assertEqual(response.status_code, 302)

    def test_authenticated_non_staff_user_is_redirected(self):
        User.objects.create_user(username="plain", password="pw123", is_staff=False)
        self.client.login(username="plain", password="pw123")
        response = self.client.get(reverse("staff_experiments"))
        self.assertEqual(response.status_code, 302)


class ExperimentsPageTest(TestCase):
    def setUp(self):
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")

    def _get(self):
        return self.client.get(reverse("staff_experiments"))

    def test_staff_user_gets_the_page(self):
        self.assertContains(self._get(), "<h1>Experiments</h1>", html=True)

    def test_every_experiment_setting_is_listed(self):
        body = self._get().content.decode()
        missing = [e.setting for e in EXPERIMENTS if e.setting not in body]
        self.assertEqual(missing, [])

    @override_settings(TYPESAFE_LETTER_RANKING_ENABLED=True, TYPESAFE_API_KEY=None)
    def test_a_set_flag_missing_its_key_says_which_setting_is_unset(self):
        self.assertContains(
            self._get(), "Not in effect: unset <code>TYPESAFE_API_KEY</code>."
        )

    @override_settings(
        TYPESAFE_LETTER_RANKING_ENABLED=True,
        TYPESAFE_API_KEY="sk-experiments-page-must-not-show-this",
        TEMPORAL_PAYLOAD_KEY="temporal-key-must-not-show-either",
    )
    def test_key_values_are_never_rendered(self):
        body = self._get().content.decode()
        for secret in (
            "sk-experiments-page-must-not-show-this",
            "temporal-key-must-not-show-either",
        ):
            self.assertNotIn(secret, body)

    @override_settings(MCP_DRAFT_IN_CHAT_PAUSED=True)
    def test_an_engaged_kill_switch_reads_engaged(self):
        self.assertContains(self._get(), "ENGAGED")

    def test_staff_dashboard_links_to_the_page(self):
        response = self.client.get(reverse("staff_dashboard"))
        self.assertContains(response, f'href="{reverse("staff_experiments")}"')

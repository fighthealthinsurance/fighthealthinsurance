"""Only the public site is indexed: other hosts disallow crawling and send noindex."""

from django.test import TestCase, override_settings

INDEXED = ["www.fighthealthinsurance.com", "fighthealthinsurance.com"]


@override_settings(SEARCH_INDEXED_HOSTS=INDEXED, ALLOWED_HOSTS=["*"])
class NoIndexOffMainHostTest(TestCase):
    def test_main_site_robots_allows_crawling(self):
        body = self.client.get(
            "/robots.txt", HTTP_HOST="www.fighthealthinsurance.com"
        ).content.decode()
        self.assertIn("Allow: /", body)
        self.assertNotIn("Disallow", body)

    def test_staging_robots_disallows_everything(self):
        for host in (
            "staging.fighthealthinsurance.com",
            "dev.fighthealthinsurance.com",
            "api.fighthealthinsurance.com",
        ):
            body = self.client.get("/robots.txt", HTTP_HOST=host).content.decode()
            self.assertEqual(body, "User-agent: *\nDisallow: /\n", host)

    def test_main_site_pages_have_no_noindex_header(self):
        response = self.client.get("/", HTTP_HOST="www.fighthealthinsurance.com")
        self.assertNotIn("X-Robots-Tag", response)

    def test_other_hosts_send_noindex(self):
        response = self.client.get("/", HTTP_HOST="dev.fighthealthinsurance.com")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")

    def test_host_match_ignores_case_and_port(self):
        response = self.client.get("/", HTTP_HOST="WWW.FightHealthInsurance.com:443")
        self.assertNotIn("X-Robots-Tag", response)

    def test_noindex_reaches_not_found_pages_too(self):
        response = self.client.get(
            "/no-such-page", HTTP_HOST="staging.fighthealthinsurance.com"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")

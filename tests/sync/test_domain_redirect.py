from django.test import TestCase, override_settings


class DomainRedirectMiddlewareTest(TestCase):
    @override_settings(
        ALLOWED_HOSTS=[
            "fuckhealthinsurance.com",
            "www.fuckhealthinsurance.com",
            "fightinsurance.ai",
            "www.fightinsurance.ai",
            "www.fighthealthinsurance.com",
            "testserver",
        ]
    )
    def test_alternate_domains_redirect_to_canonical(self):
        cases = [
            "fuckhealthinsurance.com",
            "www.fuckhealthinsurance.com",
            "fightinsurance.ai",
            "www.fightinsurance.ai",
        ]
        for host in cases:
            with self.subTest(host=host):
                response = self.client.get("/some/path?x=1", HTTP_HOST=host)
                self.assertEqual(response.status_code, 301)
                self.assertEqual(
                    response["Location"],
                    f"http://www.fighthealthinsurance.com/some/path?x=1",
                )

    def test_canonical_host_is_not_redirected(self):
        response = self.client.get("/", HTTP_HOST="testserver")
        self.assertNotEqual(response.status_code, 301)

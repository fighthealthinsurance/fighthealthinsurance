from django.test import TestCase
from django.urls import reverse


class TestCoreURLsLoad(TestCase):
    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def test_homepage(self):
        result = self.client.get("/")
        self.assertEqual(result.status_code, 200)

    def test_common_urls(self):
        """Test the core/simple URLs that don't need any parameters all load."""
        simple_endpoints = [
            "scan",
            "about",
            "how-to-help",
            "other-resources",
            "share_denial",
            #            "share_appeal",
            "remove_data",
            "tos",
        ]
        for e in simple_endpoints:
            url = reverse(e)
            result = self.client.get(url)
            self.assertEqual(result.status_code, 200)

from django.test import TestCase, Client
from django.urls import reverse


class TestCoreURLsLoad(TestCase):
    def test_homepage(self):
        result = self.client.get("/")
        self.assertEqual(result.status_code, 200)

    def test_common_urls(self):
        """Test the core/simple URLs that don't need any parameters all load."""
        simple_endpoints = [
            "scan",
            "about",
            "other-resources",
            "share_denial",
#            "share_appeal",
            "remove_data",
            "tos"]
        for e in simple_endpoints:
            url = reverse(e)
            result = self.client.get(url)
            self.assertEqual(result.status_code, 200)

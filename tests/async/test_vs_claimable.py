"""Tests for the 'Fight Health Insurance vs Claimable' comparison page."""

from django.test import Client, TestCase
from django.urls import reverse


class TestVsClaimableView(TestCase):
    """The comparison page should render as a simple static page."""

    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.client = Client()
        self.url = reverse("vs-claimable")

    def test_page_loads(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_page_compares_both_products(self):
        response = self.client.get(self.url)
        self.assertContains(response, "Claimable")
        self.assertContains(response, "Fight Health Insurance")

    def test_page_links_to_appeal_flow(self):
        response = self.client.get(self.url)
        self.assertContains(response, reverse("scan"))

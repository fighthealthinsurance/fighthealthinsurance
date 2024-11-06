from django.test import TestCase
from fighthealthinsurance.utils import url_fixer


class TestBadURLs(TestCase):

    def test_none(self):
        fixed = url_fixer(None)
        self.assertEqual(None, fixed)

    def test_badtla(self):
        fixed = url_fixer("http://www.google.com http://www.google.com/farts")
        self.assertEqual("http://www.google.com ", fixed)

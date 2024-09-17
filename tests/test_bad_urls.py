from django.test import TestCase
from fighthealthinsurance.ml_models import RemoteOpenLike


class TestBadURLs(TestCase):

    def test_none(self):
        r = RemoteOpenLike("", "", "", "")
        fixed = r.url_fixer(None)
        self.assertEqual(None, fixed)

    def test_badtla(self):
        r = RemoteOpenLike("", "", "", "")
        fixed = r.url_fixer("http://www.google.com http://www.google.com/farts")
        self.assertEqual("http://www.google.com ", fixed)

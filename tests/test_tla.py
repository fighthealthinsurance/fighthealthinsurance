from django.test import TestCase
from fighthealthinsurance.ml_models import RemoteOpenLike

class TestTLA(TestCase):
    
    def test_none(self):
        r = RemoteOpenLike("", "", "", "")
        fixed = r.tla_fixer(None)
        self.assertEqual(None, fixed)

    def test_badtla(self):
        r = RemoteOpenLike("", "", "", "")
        fixed = r.tla_fixer("Farts Farts Magic (FFG). FFG is.")
        self.assertEqual("Farts Farts Magic (FFM). FFM is.", fixed)

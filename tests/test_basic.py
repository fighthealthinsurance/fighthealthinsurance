from django.test import TestCase


class TestBasic(TestCase):
    def test_trivial(self):
        self.assertEqual(True, True)

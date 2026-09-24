"""Staff sender test pages: a numeric entry is a count, anything else a target.

The follow-up, thank-you and fax sender pages share one field. A number
means "send this many"; anything else is an email to send to. The email
views checked the field with int() and then passed the raw string on, so
send_all sliced its candidates with "2" and raised a TypeError (a 500). The
fax view used isdigit(), which also passes a string on, and accepts "²",
which int() rejects.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from fighthealthinsurance import staff_views
from fighthealthinsurance.followup_emails import ThankyouEmailSender
from fighthealthinsurance.models import InterestedProfessional

FOLLOWUP_URL = "/timbit/help/followup_sender_test"
THANKYOU_URL = "/timbit/help/thankyou_sender_test"
FAX_URL = "/timbit/help/followup_fax_test"


class ParseCountTest(TestCase):
    def test_digits_are_a_count(self):
        self.assertEqual(staff_views.parse_count("2"), 2)
        self.assertEqual(staff_views.parse_count("0"), 0)

    def test_anything_else_is_not_a_count(self):
        for value in [None, "", "-1", " 2", "2 ", "1.5", "²", "someone@example.com"]:
            with self.subTest(value=value):
                self.assertIsNone(staff_views.parse_count(value))


class SenderCountTest(TestCase):
    def setUp(self):
        self.client = Client()
        staff = get_user_model().objects.create_user(
            username="staffer", password="pw", is_staff=True
        )
        self.client.force_login(staff)

    def test_thankyou_count_limits_the_send(self):
        """End to end: "2" sends to two of three waiting professionals."""
        for i in range(3):
            InterestedProfessional.objects.create(email=f"pro{i}@clinic.com")
        with patch.object(ThankyouEmailSender, "dosend", return_value=True) as dosend:
            response = self.client.post(THANKYOU_URL, {"email": "2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"2")
        self.assertEqual(dosend.call_count, 2)

    def test_followup_count_reaches_send_all_as_int(self):
        with patch(
            "fighthealthinsurance.followup_emails.FollowUpEmailSender.find_all_due",
            return_value=[],
        ):
            response = self.client.post(FOLLOWUP_URL, {"email": "2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"0")

    def test_fax_count_reaches_dosend_all_as_int(self):
        with patch.object(
            staff_views.SendFaxHelper, "blocking_dosend_all", return_value=0
        ) as dosend_all:
            response = self.client.post(FAX_URL, {"email": "2"})
        self.assertEqual(response.status_code, 200)
        dosend_all.assert_called_once_with(count=2)

    def test_fax_superscript_digit_is_a_target_not_a_count(self):
        with (
            patch.object(
                staff_views.SendFaxHelper, "blocking_dosend_all", return_value=0
            ) as dosend_all,
            patch.object(
                staff_views.SendFaxHelper, "blocking_dosend_target", return_value=0
            ) as dosend_target,
        ):
            response = self.client.post(FAX_URL, {"email": "²"})
        self.assertEqual(response.status_code, 200)
        dosend_all.assert_not_called()
        dosend_target.assert_called_once_with(email="²")

    def test_email_is_a_target(self):
        with patch.object(ThankyouEmailSender, "dosend", return_value=True) as dosend:
            response = self.client.post(THANKYOU_URL, {"email": "someone@example.com"})
        self.assertEqual(response.status_code, 200)
        dosend.assert_called_once_with(email="someone@example.com")

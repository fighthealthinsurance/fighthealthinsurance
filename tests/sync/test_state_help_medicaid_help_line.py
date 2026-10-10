"""The state help pages call the listed Medicaid contact a help line, since
many states list member services rather than an ombudsman."""

from django.test import Client, TestCase
from django.urls import reverse


class MedicaidHelpLineLabelTest(TestCase):
    def test_a_member_services_line_is_not_called_an_ombudsman(self):
        body = Client().get(reverse("state_help", args=["kentucky"])).content.decode()
        self.assertIn("Medicaid help line", body)
        self.assertIn("Kentucky Medicaid Member Services", body)
        self.assertNotIn("Medicaid Ombudsman</h4>", body)

    def test_a_real_ombudsman_still_reads_as_one_by_its_name(self):
        body = Client().get(reverse("state_help", args=["texas"])).content.decode()
        self.assertIn("Medicaid help line", body)
        self.assertIn("Office of the Ombudsman", body)

    def test_the_index_doesnt_promise_an_ombudsman(self):
        body = Client().get(reverse("state_help_index")).content.decode()
        self.assertNotIn("Ombudsman Available", body)
        self.assertIn("Medicaid help line listed", body)

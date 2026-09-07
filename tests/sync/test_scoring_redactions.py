"""common_view_logic.scoring_redactions: the identifiers held in the
denial's profile fields, gathered per denial for letter_quality. Not a
promise about the letter text itself."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from fhi_users.models import PatientUser, ProfessionalUser
from fighthealthinsurance.common_view_logic import scoring_redactions
from fighthealthinsurance.models import Denial


class ScoringRedactionsTest(TestCase):
    def test_collects_names_emails_numbers_and_ids(self):
        User = get_user_model()
        patient = PatientUser.objects.create(
            user=User.objects.create_user(
                username="jane", email="jane@example.org", first_name="Jane", last_name="Doe"
            ),
            display_name="JD",
        )
        professional = ProfessionalUser.objects.create(
            user=User.objects.create_user(
                username="sam", email="sam@clinic.example", first_name="Sam", last_name="Smith"
            ),
            active=True,
            npi_number="1234567890",
            fax_number="415-555-0100",
            display_name="Sam Smith MD",
        )
        from fhi_users.models import UserContactInfo

        UserContactInfo.objects.create(
            user=patient.user, phone_number="212-555-0199", address1="14 Oak St", address2="Apt 2"
        )
        denial = Denial.objects.create(
            hashed_email="h",
            denial_text="denied",
            raw_email="jane@example.org",
            claim_id="TLH-1",
            plan_id="UNKNOWN",
            employer_name="Totally Legit Co",
            patient_user=patient,
            primary_professional=professional,
        )
        found = dict(scoring_redactions(denial))
        for value, category in {
            "Jane Doe": "PATIENT#patient",
            "Doe": "PATIENT#patient",
            "jane@example.org": "EMAIL",
            "Sam Smith": f"PROFESSIONAL#{professional.pk}",
            "Sam Smith MD": f"PROFESSIONAL#{professional.pk}",
            "sam@clinic.example": "EMAIL",
            "1234567890": "NPI",
            "415-555-0100": "PHONE",  # fax numbers share the phone namespace
            "TLH-1": "CLAIM_ID",
            "Totally Legit Co": "EMPLOYER",
            "14 Oak St": "ADDRESS",
            "Apt 2": "ADDRESS",
            "212-555-0199": "PHONE",
        }.items():
            self.assertEqual(found.get(value), category, value)
        self.assertNotIn("UNKNOWN", found)

    def test_a_relation_that_cannot_be_read_raises_instead_of_dropping_it(self):
        from unittest.mock import patch

        User = get_user_model()
        patient = PatientUser.objects.create(
            user=User.objects.create_user(username="p", email="p@example.org"), display_name="P"
        )
        denial = Denial.objects.create(hashed_email="h", denial_text="denied", patient_user=patient)
        with patch.object(PatientUser, "get_legal_name", side_effect=RuntimeError("db away")):
            with self.assertRaises(RuntimeError):
                scoring_redactions(denial)

    def test_a_bare_denial_yields_nothing_and_never_raises(self):
        denial = Denial.objects.create(hashed_email="h", denial_text="denied")
        self.assertEqual(scoring_redactions(denial), [])

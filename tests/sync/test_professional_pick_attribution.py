"""The professional flow's picks reach the model-usage reporting.

AppealViewSet.assemble_appeal is where a professional's final text enters the
system. It used to write the Appeal only, so no professional pick ever became
a chosen ProposedAppeal row and the per-model win rates on the staff dashboard
reflected consumer picks alone.
"""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance.common_view_logic import AppealAssemblyHelper
from fighthealthinsurance.models import (
    Denial,
    ExtraUserProperties,
    PatientUser,
    ProfessionalUser,
    ProposedAppeal,
    UserDomain,
)

User = get_user_model()


class ProfessionalPickAttributionTest(APITestCase):
    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )
        self.pro_user = User.objects.create_user(
            username=f"prouser🐼{self.domain.id}",
            password="testpass",
            email="pro@example.com",
        )
        self.professional = ProfessionalUser.objects.create(
            user=self.pro_user, active=True, npi_number="1234567890"
        )
        ExtraUserProperties.objects.create(user=self.pro_user, email_verified=True)
        patient_user = User.objects.create_user(
            username="patientuser",
            password="patientpass",
            email="patient@example.com",
            first_name="Test",
            last_name="Patient",
        )
        self.patient = PatientUser.objects.create(user=patient_user)
        self.denial = Denial.objects.create(
            denial_text="Test denial text about arthritis and physical therapy",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient,
            hashed_email=Denial.get_hashed_email(patient_user.email),
            insurance_company="Test Insurance Co",
            procedure="physical therapy",
            diagnosis="rheumatoid arthritis",
            domain=self.domain,
        )
        self.client.login(username=self.pro_user.username, password="testpass")
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()
        # Drafts the streaming path stored for this denial, from two models.
        self.draft = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="Draft letter from model x",
            chosen=False,
            model_name="model-x",
        )
        ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="Draft letter from model y",
            chosen=False,
            model_name="model-y",
        )

    def _assemble(self, text, **extra):
        payload = {
            "denial_id": str(self.denial.denial_id),
            "completed_appeal_text": text,
            "insurance_company": "Test Insurance Co",
            **extra,
        }
        with mock.patch.object(
            AppealAssemblyHelper, "_assemble_appeal_pdf", return_value=None
        ):
            response = self.client.post(
                reverse("appeals-assemble-appeal"),
                json.dumps(payload),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.content)
        return response

    def _chosen(self):
        return ProposedAppeal.objects.filter(for_denial=self.denial, chosen=True)

    def test_assembling_records_the_pick_with_the_drafts_model(self):
        self._assemble(
            "Draft letter from model x, edited by the professional",
            proposed_appeal_id=self.draft.id,
        )
        self.assertEqual(self._chosen().get().model_name, "model-x")

    def test_assembling_without_an_id_still_matches_the_draft_text(self):
        self._assemble("Draft letter from model y")
        self.assertEqual(self._chosen().get().model_name, "model-y")

    def test_regenerating_the_document_with_the_same_text_is_one_pick(self):
        self._assemble("Draft letter from model x", proposed_appeal_id=self.draft.id)
        self._assemble("Draft letter from model x", proposed_appeal_id=self.draft.id)
        self.assertEqual(self._chosen().count(), 1)

    def test_text_from_scratch_with_several_models_in_play_is_unattributed(self):
        self._assemble("Something written from scratch")
        self.assertIsNone(self._chosen().get().model_name)

    def test_an_edited_assembly_is_recorded_as_edited_and_a_verbatim_one_is_not(
        self,
    ):
        # completed_appeal_text is post-editing text and the flow has no
        # textarea flag, so whether the pick was edited comes from the text.
        self._assemble(
            "Draft letter from model x, edited by the professional",
            proposed_appeal_id=self.draft.id,
        )
        self._assemble("Draft letter from model y")
        by_text = {p.appeal_text: p for p in self._chosen()}
        self.assertTrue(
            by_text["Draft letter from model x, edited by the professional"].editted
        )
        self.assertFalse(by_text["Draft letter from model y"].editted)

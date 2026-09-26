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

    def test_regenerating_with_crlf_line_endings_is_still_one_pick(self):
        # The same letter resubmitted with Windows line endings.
        self._assemble("Dear insurer,\nplease cover this.")
        self._assemble("Dear insurer,\r\nplease cover this.")
        self.assertEqual(self._chosen().count(), 1)

    def test_iterating_on_the_letter_leaves_one_pick_with_the_latest_text(self):
        # Fix a typo and regenerate: one decision, not a pick per version.
        self._assemble("Draft letter from model x, with a typo")
        self._assemble("Draft letter from model x, typo fixed")
        self.assertEqual(
            self._chosen().get().appeal_text, "Draft letter from model x, typo fixed"
        )

    def test_a_pick_from_another_flow_survives_a_professional_reassembly(self):
        # Replacement is limited to this flow's own picks: a consumer or
        # share-flow pick on the same denial, or one recorded before this
        # flow recorded any, is somebody else's decision.
        other = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="Draft letter from model y, picked elsewhere",
            chosen=True,
            model_name="model-y",
        )
        self._assemble("Draft letter from model x, first version")
        self._assemble("Draft letter from model x, second version")
        self.assertTrue(ProposedAppeal.objects.filter(id=other.id).exists())

    def test_a_letter_written_with_no_drafts_stored_is_not_a_pick(self):
        # No model was on offer, so there is nothing to attribute.
        ProposedAppeal.objects.filter(for_denial=self.denial).delete()
        self._assemble("Written entirely by the professional")
        self.assertFalse(self._chosen().exists())

    # completed_appeal_text is post-editing text and the flow has no textarea
    # flag, so whether the pick was edited comes from the text.

    def test_an_edited_assembly_is_recorded_as_edited(self):
        self._assemble(
            "Draft letter from model x, edited by the professional",
            proposed_appeal_id=self.draft.id,
        )
        self.assertTrue(self._chosen().get().editted)

    def test_a_verbatim_assembly_is_not_recorded_as_edited(self):
        self._assemble("Draft letter from model y")
        self.assertFalse(self._chosen().get().editted)

    def test_a_verbatim_assembly_of_a_substituted_draft_is_not_an_edit(self):
        # The browser shows the draft with the denial's values substituted
        # for its placeholders; the stored draft keeps the placeholders.
        draft = ProposedAppeal.objects.create(
            for_denial=self.denial,
            appeal_text="Please cover {procedure} for my patient.",
            chosen=False,
            model_name="model-x",
        )
        self._assemble(
            "Please cover physical therapy for my patient.",
            proposed_appeal_id=draft.id,
        )
        self.assertFalse(self._chosen().get().editted)

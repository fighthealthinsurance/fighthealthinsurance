"""Unit tests for the regulator escalation packet feature."""

from unittest.mock import patch

from django.test import TestCase, Client
from django.urls import reverse

from fighthealthinsurance.common_view_logic import get_denial_for_action
from fighthealthinsurance.escalation_addresses import (
    DOL_EBSA_NAME,
    RECIPIENT_DOI,
    RECIPIENT_DOL_EBSA,
    RECIPIENT_MEDICAL_DIRECTOR,
    EscalationRecipient,
    get_recipients_for_denial,
)
from fighthealthinsurance.generate_regulator_letter import (
    make_regulator_letter_prompt,
)
from fighthealthinsurance.models import Denial, RegulatorEscalation
from fighthealthinsurance.state_help import StateHelp

CALIFORNIA_DATA = {
    "slug": "california",
    "name": "California",
    "abbreviation": "CA",
    "insurance_department": {
        "name": "California Department of Insurance",
        "url": "https://www.insurance.ca.gov/",
        "phone": "916-492-3500",
        "consumer_line": "800-927-4357",
        "complaint_url": "https://cdi.ca.gov/complaint",
    },
    "consumer_assistance": {
        "cap_name": "CHA",
    },
    "medicaid": {
        "agency_name": "DHCS",
    },
    "external_review": {"available": True, "info_url": "https://example.com"},
}


class _FakeDenial:
    """Lightweight stand-in for Denial when tests don't need DB."""

    def __init__(
        self,
        state="CA",
        insurance_company="Aetna",
        regulator=None,
        is_tpa=False,
        denial_text="Service denied because not medically necessary.",
        procedure="MRI",
        diagnosis="Lower back pain",
        claim_id="CLAIM123",
        plan_id="PLAN9",
        qa_context="",
    ):
        self.your_state = state
        self.state = state
        self.insurance_company = insurance_company
        self.insurance_company_obj = (
            type("ICO", (), {"is_tpa": is_tpa, "name": insurance_company})()
            if insurance_company
            else None
        )
        self.regulator = regulator
        self.denial_text = denial_text
        self.procedure = procedure
        self.diagnosis = diagnosis
        self.claim_id = claim_id
        self.plan_id = plan_id
        self.qa_context = qa_context
        self.use_external = False

        class _PlanSourceManager:
            def all(self):
                return []

        self.plan_source = _PlanSourceManager()


class EscalationAddressesTest(TestCase):
    """Recipient assembly logic."""

    @patch("fighthealthinsurance.escalation_addresses.get_state_help_by_abbreviation")
    def test_recipients_with_state_includes_doi_and_medical_director(self, mock_state):
        mock_state.return_value = StateHelp(CALIFORNIA_DATA)
        denial = _FakeDenial(state="CA", insurance_company="Aetna")
        recipients = get_recipients_for_denial(denial)
        types = [r.recipient_type for r in recipients]
        self.assertIn("doi", types)
        self.assertIn("medical_director", types)
        self.assertNotIn("dol_ebsa", types)

        doi = next(r for r in recipients if r.recipient_type == "doi")
        self.assertEqual(doi.name, "California Department of Insurance")
        self.assertTrue(doi.extra["external_review_available"])

    def test_recipients_without_state_still_includes_medical_director(self):
        denial = _FakeDenial(state="", insurance_company="Cigna")
        recipients = get_recipients_for_denial(denial)
        types = [r.recipient_type for r in recipients]
        self.assertEqual(types, ["medical_director"])
        self.assertIn("Cigna", recipients[0].name)

    def test_erisa_branch_via_tpa_flag_adds_dol_ebsa(self):
        denial = _FakeDenial(
            state="",
            insurance_company="UnitedHealthcare",
            is_tpa=True,
        )
        recipients = get_recipients_for_denial(denial)
        types = [r.recipient_type for r in recipients]
        self.assertIn("dol_ebsa", types)
        ebsa = next(r for r in recipients if r.recipient_type == "dol_ebsa")
        self.assertEqual(ebsa.name, DOL_EBSA_NAME)

    def test_erisa_branch_via_regulator_alt_name_adds_dol_ebsa(self):
        regulator = type("Reg", (), {"alt_name": "ERISA"})()
        denial = _FakeDenial(state="", regulator=regulator, insurance_company="X")
        recipients = get_recipients_for_denial(denial)
        types = [r.recipient_type for r in recipients]
        self.assertIn("dol_ebsa", types)


class RegulatorLetterPromptTest(TestCase):
    """Prompt construction for the LLM."""

    def test_doi_prompt_mentions_state_and_insurance_company(self):
        recipient = EscalationRecipient(
            recipient_type="doi",
            name="California Department of Insurance",
            extra={
                "state_name": "California",
                "external_review_available": True,
            },
        )
        denial = _FakeDenial()
        prompt = make_regulator_letter_prompt(denial, recipient)
        self.assertIn("California", prompt)
        self.assertIn("external (independent) medical review", prompt)
        self.assertIn("Aetna", prompt)
        self.assertIn("MRI", prompt)

    def test_dol_ebsa_prompt_mentions_erisa(self):
        recipient = EscalationRecipient(
            recipient_type="dol_ebsa",
            name=DOL_EBSA_NAME,
        )
        denial = _FakeDenial()
        prompt = make_regulator_letter_prompt(denial, recipient)
        self.assertIn("ERISA", prompt)
        self.assertIn("29 C.F.R. § 2560.503-1", prompt)

    def test_medical_director_prompt_uses_peer_to_peer_framing(self):
        recipient = EscalationRecipient(
            recipient_type="medical_director",
            name="Medical Director, Aetna",
        )
        denial = _FakeDenial()
        prompt = make_regulator_letter_prompt(denial, recipient)
        self.assertIn("peer-to-peer", prompt.lower())


class RecipientConstantsTest(TestCase):
    """The escalation_addresses module re-exports model constants."""

    def test_constants_match_model(self):
        self.assertEqual(RECIPIENT_DOI, RegulatorEscalation.RECIPIENT_DOI)
        self.assertEqual(
            RECIPIENT_MEDICAL_DIRECTOR,
            RegulatorEscalation.RECIPIENT_MEDICAL_DIRECTOR,
        )
        self.assertEqual(RECIPIENT_DOL_EBSA, RegulatorEscalation.RECIPIENT_DOL_EBSA)


class GetDenialForActionTest(TestCase):
    """The shared denial lookup used by views and the escalation helper."""

    def setUp(self):
        self.email = "lookup@example.com"
        self.denial = Denial.objects.create(
            denial_text="x",
            hashed_email=Denial.get_hashed_email(self.email),
            insurance_company="Aetna",
        )

    def test_returns_denial_when_all_match(self):
        result = get_denial_for_action(
            self.denial.denial_id, self.email, self.denial.semi_sekret
        )
        self.assertEqual(result.pk, self.denial.pk)

    def test_returns_none_for_wrong_sekret(self):
        result = get_denial_for_action(self.denial.denial_id, self.email, "wrong")
        self.assertIsNone(result)

    def test_returns_none_for_missing_args(self):
        self.assertIsNone(get_denial_for_action(None, self.email, "x"))
        self.assertIsNone(get_denial_for_action(1, "", "x"))
        self.assertIsNone(get_denial_for_action(1, self.email, ""))

    def test_returns_none_for_non_integer_denial_id(self):
        self.assertIsNone(
            get_denial_for_action("not-a-number", self.email, self.denial.semi_sekret)
        )


class RegulatorEscalationModelTest(TestCase):
    """Smoke tests for the new model."""

    def _make_denial(self):
        return Denial.objects.create(
            denial_text="some denial",
            hashed_email="abc123",
            insurance_company="Aetna",
            your_state="CA",
        )

    def test_create_escalation_row(self):
        denial = self._make_denial()
        escalation = RegulatorEscalation.objects.create(
            for_denial=denial,
            hashed_email="abc123",
            recipient_type=RegulatorEscalation.RECIPIENT_DOI,
            recipient_name="California Department of Insurance",
            letter_text="Dear Commissioner,",
        )
        self.assertEqual(escalation.recipient_type, "doi")
        self.assertFalse(escalation.chosen)
        self.assertFalse(escalation.editted)
        self.assertEqual(
            list(denial.regulator_escalations.values_list("id", flat=True)),
            [escalation.id],
        )


class EscalationPacketViewTest(TestCase):
    """Smoke tests for the new views and URL routes."""

    def setUp(self):
        self.client = Client()
        # Create a denial that resolves through the GET path.
        self.email = "tester@example.com"
        self.denial = Denial.objects.create(
            denial_text="denial body",
            hashed_email=Denial.get_hashed_email(self.email),
            insurance_company="Aetna",
            your_state="CA",
        )

    def test_escalation_packet_get_redirects_without_params(self):
        url = reverse("escalation_packet")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_escalation_packet_get_renders_when_valid(self):
        url = reverse("escalation_packet")
        response = self.client.get(
            url,
            {
                "denial_id": self.denial.denial_id,
                "email": self.email,
                "semi_sekret": self.denial.semi_sekret,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "escalation_packet.html")
        # Medical director recipient is always present.
        self.assertContains(response, "Medical Director")

    def test_choose_escalation_letter_rejects_invalid_form(self):
        url = reverse("choose_escalation_letter")
        response = self.client.post(url, {"letter_text": "x"})
        self.assertEqual(response.status_code, 302)

    def test_choose_escalation_letter_persists_chosen_text(self):
        escalation = RegulatorEscalation.objects.create(
            for_denial=self.denial,
            hashed_email=self.denial.hashed_email,
            recipient_type=RegulatorEscalation.RECIPIENT_DOI,
            recipient_name="California Department of Insurance",
            letter_text="Original draft.",
        )
        url = reverse("choose_escalation_letter")
        response = self.client.post(
            url,
            {
                "denial_id": str(self.denial.denial_id),
                "email": self.email,
                "semi_sekret": self.denial.semi_sekret,
                "escalation_uuid": escalation.uuid,
                "letter_text": "User-edited text.",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "escalation_packet_review.html")
        escalation.refresh_from_db()
        self.assertTrue(escalation.chosen)
        self.assertTrue(escalation.editted)
        self.assertEqual(escalation.letter_text, "User-edited text.")

    def test_choose_escalation_letter_rejects_malformed_uuid(self):
        url = reverse("choose_escalation_letter")
        response = self.client.post(
            url,
            {
                "denial_id": str(self.denial.denial_id),
                "email": self.email,
                "semi_sekret": self.denial.semi_sekret,
                "escalation_uuid": "not-a-uuid",
                "letter_text": "x",
            },
        )
        self.assertEqual(response.status_code, 302)

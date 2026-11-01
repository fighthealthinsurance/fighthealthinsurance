"""Unit tests for the regulator escalation packet feature."""

import json
import re
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.test import TestCase, Client
from django.urls import reverse

from fighthealthinsurance.common_view_logic import (
    EscalationPacketHelper,
    get_denial_for_action,
)
from fighthealthinsurance.escalation_addresses import (
    DOL_EBSA_NAME,
    RECIPIENT_DOI,
    RECIPIENT_DOL_EBSA,
    RECIPIENT_MEDICAL_DIRECTOR,
    EscalationRecipient,
    get_recipients_for_denial,
    regulator_escalation_reasons,
)
from fighthealthinsurance.generate_regulator_letter import (
    make_regulator_letter_prompt,
)
from fighthealthinsurance.models import Denial, RegulatorEscalation
from fighthealthinsurance.state_help import StateHelp
from tests.back_links import back_link

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
        urgent=False,
        denial_types=(),
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
        self.urgent = urgent

        class _PlanSourceManager:
            def all(self):
                return []

        self.plan_source = _PlanSourceManager()

        class _DenialTypeManager:
            def all(self):
                return [type("DT", (), {"name": name})() for name in denial_types]

        self.denial_type = _DenialTypeManager()


def _fake_regulator(name, alt_name="", regex=None):
    """A stand-in Regulator; its regex is its name unless given (empty for
    one the model inferred), compiled as the RegexField would."""
    pattern = name if regex is None else regex
    return type(
        "Reg",
        (),
        {
            "alt_name": alt_name,
            "name": name,
            "regex": re.compile(pattern, re.IGNORECASE),
            "negative_regex": re.compile(""),
        },
    )()


def _make_ico(**overrides):
    """Build a stand-in matched ``InsuranceCompany`` object for recipient tests.

    Defaults model a plain (non-TPA) carrier with no appeal-routing info;
    pass keyword overrides for the fields a given test exercises.
    """
    attrs = {
        "is_tpa": False,
        "name": "Aetna",
        "appeal_address": "",
        "appeal_phone_number": "",
        "member_services_url": "",
        "website": "",
    }
    attrs.update(overrides)
    return type("ICO", (), attrs)()


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

    @patch("fighthealthinsurance.escalation_addresses.get_state_help_by_abbreviation")
    def test_doi_recipient_includes_consumer_phone_number(self, mock_state):
        mock_state.return_value = StateHelp(CALIFORNIA_DATA)
        denial = _FakeDenial(state="CA")
        recipients = get_recipients_for_denial(denial)
        doi = next(r for r in recipients if r.recipient_type == "doi")
        self.assertEqual(doi.phone, "800-927-4357")

    def test_medical_director_uses_insurer_appeal_contact_info(self):
        denial = _FakeDenial(state="", insurance_company="Aetna")
        denial.insurance_company_obj = _make_ico(
            appeal_address="Aetna Appeals\nPO Box 14463\nLexington, KY 40512",
            appeal_phone_number="1-800-872-3862",
            member_services_url="https://www.aetna.com/contact-us.html",
            website="https://www.aetna.com/",
        )
        recipients = get_recipients_for_denial(denial)
        md = next(r for r in recipients if r.recipient_type == "medical_director")
        self.assertEqual(md.phone, "1-800-872-3862")
        self.assertTrue(md.address.startswith("Medical Director\n"))
        self.assertIn("PO Box 14463", md.address)
        self.assertEqual(md.url, "https://www.aetna.com/contact-us.html")

    def test_medical_director_falls_back_to_insurer_website_url(self):
        denial = _FakeDenial(state="", insurance_company="Aetna")
        denial.insurance_company_obj = _make_ico(website="https://www.aetna.com/")
        recipients = get_recipients_for_denial(denial)
        md = next(r for r in recipients if r.recipient_type == "medical_director")
        self.assertEqual(md.url, "https://www.aetna.com/")

    def test_medical_director_keeps_placeholder_without_matched_insurer(self):
        denial = _FakeDenial(state="", insurance_company="Cigna")
        denial.insurance_company_obj = None
        recipients = get_recipients_for_denial(denial)
        md = next(r for r in recipients if r.recipient_type == "medical_director")
        self.assertEqual(md.phone, "")
        self.assertIn("[Insurance company mailing address", md.address)

    def test_medical_director_drops_non_http_insurer_url(self):
        """Recipient URLs become clickable links, so schemes other than
        http(s) must be dropped at construction."""
        denial = _FakeDenial(state="", insurance_company="Aetna")
        denial.insurance_company_obj = _make_ico(
            member_services_url="javascript:alert(1)"
        )
        recipients = get_recipients_for_denial(denial)
        md = next(r for r in recipients if r.recipient_type == "medical_director")
        self.assertEqual(md.url, "")

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


class RegulatorEscalationReasonsTest(TestCase):
    """What makes the appeal pages say a regulator should hear about a denial:
    internal appeals done, or urgent care that needs a parallel complaint."""

    def test_a_plain_first_denial_gives_no_reason(self):
        denial = _FakeDenial(
            state="", denial_text="The service is not medically necessary."
        )
        self.assertEqual(regulator_escalation_reasons(denial), [])

    def test_a_first_medical_necessity_denial_of_prep_gives_no_reason(self):
        """The reported case: PrEP is tagged Preventive Care from the kind of
        care alone; a first-level medical-necessity denial is an appeal, not
        yet a regulator case."""
        denial = _FakeDenial(
            state="",
            procedure="PrEP",
            denial_text=(
                "Your request for prep has been denied as not medically "
                "necessary."
            ),
            denial_types=("Preventive Care",),
        )
        self.assertEqual(regulator_escalation_reasons(denial), [])

    def test_an_appeal_upheld_once_is_not_yet_a_reason(self):
        """Most plans have a second internal level, so one upheld appeal is
        still a job for the next appeal."""
        denial = _FakeDenial(
            state="",
            denial_text="After review we have upheld our original decision.",
        )
        self.assertEqual(regulator_escalation_reasons(denial), [])

    def test_a_final_adverse_benefit_determination_is_a_reason(self):
        denial = _FakeDenial(
            state="", denial_text="This is our final adverse benefit determination."
        )
        reasons = regulator_escalation_reasons(denial)
        self.assertEqual(len(reasons), 1)
        self.assertIn("final decision on your internal appeals", reasons[0])

    def test_later_level_appeals_decided_are_a_reason(self):
        for text in (
            "Your second level appeal has been reviewed and the denial stands.",
            "We reviewed your level 2 appeal.",
            "This responds to your third-level appeal.",
            "You have exhausted all internal appeals.",
        ):
            with self.subTest(text=text):
                denial = _FakeDenial(state="", denial_text=text)
                self.assertTrue(regulator_escalation_reasons(denial))

    def test_a_rights_notice_about_later_levels_is_not_a_reason(self):
        """A first denial's rights notice uses the same words about what
        could happen next (the triage fixture's letter is one)."""
        denial = _FakeDenial(
            state="",
            denial_text=(
                "The MRI was denied as not medically necessary. You have the "
                "right to appeal. If our decision is upheld you may request "
                "a second level appeal or an external review by 10/15/2026. "
                "Once you have exhausted your internal appeals, you will "
                "receive a final adverse determination letter."
            ),
        )
        self.assertEqual(regulator_escalation_reasons(denial), [])

    def test_a_conditional_sentence_does_not_shield_the_next_one(self):
        denial = _FakeDenial(
            state="",
            denial_text=(
                "If you disagree you may contact us. This is our final "
                "adverse determination."
            ),
        )
        self.assertTrue(regulator_escalation_reasons(denial))

    def test_a_negated_final_decision_is_not_a_reason(self):
        denial = _FakeDenial(state="", denial_text="This is not a final decision.")
        self.assertEqual(regulator_escalation_reasons(denial), [])

    def test_the_external_review_rights_notice_alone_is_not_a_reason(self):
        """Every first-level denial carries this notice, so it says nothing
        about where the person is in the process."""
        denial = _FakeDenial(
            state="",
            denial_text=(
                "You may have the right to an external review by an "
                "independent review organization within four months."
            ),
        )
        self.assertEqual(regulator_escalation_reasons(denial), [])

    def test_urgent_care_is_a_reason(self):
        denial = _FakeDenial(state="", urgent=True)
        reasons = regulator_escalation_reasons(denial)
        self.assertEqual(len(reasons), 1)
        self.assertIn("in parallel with your appeal", reasons[0])

    def test_supporting_signals_alone_are_not_a_reason(self):
        """A named regulator, an ERISA plan and a policed denial type say who
        to write to, not that it is time to."""
        denial = _FakeDenial(
            state="",
            regulator=_fake_regulator("Texas Department of Insurance"),
            denial_text="Contact the Texas Department of Insurance with questions.",
            is_tpa=True,
            denial_types=("Pre-Existing Condition",),
        )
        self.assertEqual(regulator_escalation_reasons(denial), [])

    def test_a_regulator_named_in_the_letter_supports_an_urgent_denial(self):
        denial = _FakeDenial(
            state="",
            urgent=True,
            regulator=_fake_regulator("Texas Department of Insurance"),
            denial_text="Contact the Texas Department of Insurance with questions.",
        )
        reasons = regulator_escalation_reasons(denial)
        self.assertTrue(
            any("Texas Department of Insurance" in r for r in reasons), reasons
        )

    def test_a_regulator_the_model_inferred_is_not_said_to_be_named(self):
        """The model links a regulator the letter never names (no regex
        match): that is a guess about the plan, not a reason from the letter."""
        regulator = _fake_regulator("Texas Department of Insurance", regex="")
        denial = _FakeDenial(state="", urgent=True, regulator=regulator)
        self.assertEqual(len(regulator_escalation_reasons(denial)), 1)

    def test_a_regulator_row_without_a_regex_is_named_when_its_name_is_in_the_letter(
        self,
    ):
        """The state rows the model's answer creates carry no regex; the
        letter naming one is still the letter naming it."""
        regulator = _fake_regulator("Texas Department of Insurance", regex="")
        denial = _FakeDenial(
            state="",
            urgent=True,
            regulator=regulator,
            denial_text="You may contact the Texas  Department of\nInsurance.",
        )
        reasons = regulator_escalation_reasons(denial)
        self.assertTrue(
            any("Texas Department of Insurance" in r for r in reasons), reasons
        )

    def test_first_denial_rights_notices_do_not_count_as_upheld(self):
        for text in (
            "You have the right to appeal our initial determination within 180 days.",
            "To appeal our initial decision, send a letter to the address below.",
            "This notice explains your rights to appeal our original decision.",
            "You can ask us to review our initial determination.",
            "We cannot affirm our original decision without more records.",
            "We can\u2019t uphold the denial on this record.",
        ):
            with self.subTest(text=text):
                denial = _FakeDenial(state="", urgent=True, denial_text=text)
                self.assertEqual(len(regulator_escalation_reasons(denial)), 1)

    def test_an_upheld_appeal_supports_an_urgent_denial(self):
        for text in (
            "Upon review, our initial determination stands.",
            "We are maintaining our original denial.",
            "Our original decision to deny coverage has been upheld.",
            "After review, our original decision is unchanged.",
            "We reviewed your appeal. Our decision is upheld.",
        ):
            with self.subTest(text=text):
                denial = _FakeDenial(state="", urgent=True, denial_text=text)
                reasons = regulator_escalation_reasons(denial)
                self.assertTrue(
                    any("denied once" in r for r in reasons), reasons
                )

    def test_a_tpa_plan_brings_in_the_department_of_labor_when_urgent(self):
        denial = _FakeDenial(state="", urgent=True, is_tpa=True)
        reasons = regulator_escalation_reasons(denial)
        self.assertTrue(any("Department of Labor" in r for r in reasons), reasons)

    def test_a_denial_type_regulators_police_supports_a_final_denial(self):
        denial = _FakeDenial(
            state="",
            denial_text="This is our final adverse determination.",
            denial_types=("Pre-Existing Condition",),
        )
        reasons = regulator_escalation_reasons(denial)
        self.assertTrue(any("pre-existing condition" in r for r in reasons), reasons)

    def test_preventive_care_is_never_a_supporting_reason(self):
        denial = _FakeDenial(
            state="", urgent=True, denial_types=("Preventive Care",)
        )
        self.assertEqual(len(regulator_escalation_reasons(denial)), 1)

    def test_every_signal_present_gives_one_reason_each(self):
        regulator = _fake_regulator("DOL", alt_name="ERISA", regex="ERISA")
        denial = _FakeDenial(
            state="",
            denial_text="This is our final adverse determination. See your ERISA rights.",
            regulator=regulator,
            urgent=True,
            denial_types=("Pre-Existing Condition",),
        )
        # final, urgent, named regulator, ERISA, pre-existing condition.
        self.assertEqual(len(regulator_escalation_reasons(denial)), 5)


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
        self.assertFalse(escalation.edited)
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
        response = self.client.get(
            back_link(self.client, "escalation_packet", self.denial, self.email)
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "escalation_packet.html")
        # Medical director recipient is always present.
        self.assertContains(response, "Medical Director")

    @patch("fighthealthinsurance.escalation_addresses.get_state_help_by_abbreviation")
    def test_escalation_packet_page_shows_regulator_phone_number(self, mock_state):
        """The recipient checklist should surface each regulator's phone
        number so users can call before (or instead of) mailing."""
        mock_state.return_value = StateHelp(CALIFORNIA_DATA)
        response = self.client.get(
            back_link(self.client, "escalation_packet", self.denial, self.email)
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "800-927-4357")

    def _post_packet(self, **extra):
        return self.client.post(
            reverse("escalation_packet"),
            {
                "denial_id": self.denial.denial_id,
                "email": self.email,
                "semi_sekret": self.denial.semi_sekret,
                **extra,
            },
        )

    def test_escalation_packet_offers_a_state_when_none_is_known(self):
        self.denial.your_state = ""
        self.denial.save()
        response = self._post_packet()
        self.assertContains(response, 'id="escalation-state-picker"')
        self.assertContains(response, '<option value="CA">California</option>')

    def test_escalation_packet_does_not_offer_a_state_when_one_is_known(self):
        response = self._post_packet()
        self.assertNotContains(response, 'id="escalation-state-picker"')

    def test_escalation_packet_saves_a_picked_state(self):
        self.denial.your_state = ""
        self.denial.save()
        self._post_packet(your_state="ny")
        self.denial.refresh_from_db()
        self.assertEqual(self.denial.your_state, "NY")

    def test_escalation_packet_adds_the_doi_for_a_picked_state(self):
        self.denial.your_state = ""
        self.denial.save()
        response = self._post_packet(your_state="NY")
        self.assertNotContains(response, 'id="escalation-state-picker"')
        self.assertContains(response, "letters for:")
        self.assertEqual(response.context["recipients_count"], 2)

    def test_escalation_packet_ignores_an_unknown_state(self):
        self.denial.your_state = ""
        self.denial.save()
        self._post_packet(your_state="ZZ")
        self.denial.refresh_from_db()
        self.assertEqual(self.denial.your_state, "")

    def test_appeals_generation_page_exposes_escalation_button(self):
        """The appeals.html page (where users choose between drafts) must
        link to the regulator escalation flow. Without this, the only entry
        point is the final review page, which many users never reach.
        """
        response = self.client.get(
            back_link(self.client, "generate_appeal", self.denial, self.email)
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "appeals.html")
        self.assertContains(response, 'id="generate_escalation_packet"')
        self.assertContains(response, reverse("escalation_packet"))

    def test_appeals_generation_page_shows_the_callout_above_and_below(self):
        """Once above the drafts, where it is on screen during the wait, and
        once below them, each button with its own id."""
        response = self.client.get(
            back_link(self.client, "generate_appeal", self.denial, self.email)
        )
        body = response.content.decode()
        self.assertEqual(body.count('class="alert-box alert-box-info fhi-regulator-callout'), 2)
        self.assertEqual(body.count('id="generate_escalation_packet"'), 1)
        self.assertEqual(body.count('id="generate_escalation_packet_below"'), 1)
        self.assertIn("(opens in a new tab)", body)
        self.assertIn("don't have to wait for your appeal drafts", body)

    def test_appeals_generation_page_callout_is_normal_size_without_a_reason(self):
        response = self.client.get(
            back_link(self.client, "generate_appeal", self.denial, self.email)
        )
        self.assertNotContains(response, "fhi-regulator-callout-likely")
        self.assertContains(response, "Send a copy to the people who regulate your plan")

    def test_appeals_generation_page_callout_stays_normal_after_one_upheld_appeal(
        self,
    ):
        self.denial.denial_text = "We have upheld our original decision."
        self.denial.save()
        response = self.client.get(
            back_link(self.client, "generate_appeal", self.denial, self.email)
        )
        self.assertNotContains(response, "fhi-regulator-callout-likely")

    def test_appeals_generation_page_callout_grows_after_the_final_decision(self):
        self.denial.denial_text = "This is our final adverse benefit determination."
        self.denial.save()
        response = self.client.get(
            back_link(self.client, "generate_appeal", self.denial, self.email)
        )
        self.assertContains(response, "fhi-regulator-callout-likely", count=2)
        self.assertContains(
            response, "Your denial looks like one a regulator should hear about"
        )
        self.assertContains(response, "final decision on your internal appeals")

    def _send_page(self):
        response = self.client.post(
            reverse("choose_appeal"),
            {
                "denial_id": self.denial.denial_id,
                "email": self.email,
                "semi_sekret": self.denial.semi_sekret,
                "appeal_text": "Dear Aetna, please reconsider.",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "appeal.html")
        return response

    def test_send_page_shows_the_same_callout(self):
        response = self._send_page()
        self.assertContains(response, 'id="generate_escalation_packet"')
        self.assertContains(response, reverse("escalation_packet"))
        self.assertContains(response, "Draft my regulator letters")
        self.assertNotContains(response, "fhi-regulator-callout-likely")

    def test_send_page_callout_grows_for_an_urgent_denial(self):
        self.denial.urgent = True
        self.denial.save()
        response = self._send_page()
        self.assertContains(response, "fhi-regulator-callout-likely")
        self.assertContains(response, "You marked this care as urgent")

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
        self.assertTrue(escalation.edited)
        self.assertEqual(escalation.letter_text, "User-edited text.")

    def test_review_page_does_not_linkify_non_http_stored_url(self):
        """Stored recipient URLs are linkified on the review page; a
        non-http(s) scheme (e.g. from hand-edited data) must not render."""
        escalation = RegulatorEscalation.objects.create(
            for_denial=self.denial,
            hashed_email=self.denial.hashed_email,
            recipient_type=RegulatorEscalation.RECIPIENT_DOI,
            recipient_name="California Department of Insurance",
            recipient_url="javascript:alert(1)",
            letter_text="Original draft.",
        )
        response = self.client.post(
            reverse("choose_escalation_letter"),
            {
                "denial_id": str(self.denial.denial_id),
                "email": self.email,
                "semi_sekret": self.denial.semi_sekret,
                "escalation_uuid": escalation.uuid,
                "letter_text": "Final text.",
            },
        )
        self.assertNotContains(response, "javascript:")

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


def _collect_stream(parameters: dict):
    """Drive the async generator to completion and return parsed JSON records.

    Fails the caller's test if any chunk is non-empty but not valid JSON —
    we never want a malformed stream record to be silently dropped.
    """

    async def _consume():
        results = []
        async for chunk in EscalationPacketHelper.generate_escalation_letters(
            parameters
        ):
            line = chunk.strip()
            if not line:
                continue
            results.append(json.loads(line))
        return results

    return async_to_sync(_consume)()


class EscalationStreamReusesExistingTest(TestCase):
    """The streaming generator must not regenerate or duplicate prior drafts.

    Without this, every revisit to the escalation page (e.g. clicking
    "Back to all regulator letters" from the review screen) would burn
    fresh LLM calls and accumulate duplicate `RegulatorEscalation` rows.
    """

    def setUp(self):
        self.email = "stream@example.com"
        self.denial = Denial.objects.create(
            denial_text="some denial",
            hashed_email=Denial.get_hashed_email(self.email),
            insurance_company="Aetna",
            # No state -> only the medical_director recipient is built.
            your_state="",
        )

    def _params(self):
        return {
            "denial_id": self.denial.denial_id,
            "email": self.email,
            "semi_sekret": self.denial.semi_sekret,
        }

    def test_existing_letter_is_streamed_without_calling_llm(self):
        existing = RegulatorEscalation.objects.create(
            for_denial=self.denial,
            hashed_email=self.denial.hashed_email,
            recipient_type=RegulatorEscalation.RECIPIENT_MEDICAL_DIRECTOR,
            recipient_name="Medical Director, Aetna",
            letter_text="A previously generated draft we should reuse.",
        )

        # If the LLM is invoked we fail the test loudly.
        with patch(
            "fighthealthinsurance.generate_regulator_letter.generate_regulator_letter",
            new=AsyncMock(side_effect=AssertionError("LLM must not be called")),
        ):
            results = _collect_stream(self._params())

        letters = [r for r in results if r.get("type") == "letter"]
        self.assertEqual(len(letters), 1)
        self.assertEqual(letters[0]["escalation_id"], str(existing.uuid))
        self.assertEqual(letters[0]["content"], existing.letter_text)
        # No duplicate row was created.
        self.assertEqual(
            RegulatorEscalation.objects.filter(for_denial=self.denial).count(),
            1,
        )

    def test_missing_recipient_is_generated_existing_is_reused(self):
        # Pre-seed only the medical_director letter; pretend the denial is
        # also ERISA-flagged so DOL EBSA is in the recipient list and must
        # be generated fresh.
        self.denial.your_state = ""
        self.denial.save()
        # Mark the denial as ERISA-likely via the insurance_company_obj path.
        from fighthealthinsurance.models import InsuranceCompany

        ic = InsuranceCompany.objects.create(
            name="SomeTPA", is_tpa=True, regex="sometpa", negative_regex="zzznever"
        )
        self.denial.insurance_company_obj = ic
        self.denial.save()

        existing_md = RegulatorEscalation.objects.create(
            for_denial=self.denial,
            hashed_email=self.denial.hashed_email,
            recipient_type=RegulatorEscalation.RECIPIENT_MEDICAL_DIRECTOR,
            recipient_name="Medical Director, Aetna",
            letter_text="Existing MD letter.",
        )

        async def fake_llm(denial, recipient, use_external=False):
            return f"Fresh letter for {recipient.recipient_type}"

        with patch(
            "fighthealthinsurance.generate_regulator_letter.generate_regulator_letter",
            new=AsyncMock(side_effect=fake_llm),
        ) as mock_llm:
            results = _collect_stream(self._params())

        letters = [r for r in results if r.get("type") == "letter"]
        types = {r["recipient_type"] for r in letters}
        self.assertIn(RegulatorEscalation.RECIPIENT_MEDICAL_DIRECTOR, types)
        self.assertIn(RegulatorEscalation.RECIPIENT_DOL_EBSA, types)
        # LLM was only invoked for the missing recipient.
        self.assertEqual(mock_llm.call_count, 1)
        called_recipient = mock_llm.call_args.args[1]
        self.assertEqual(
            called_recipient.recipient_type, RegulatorEscalation.RECIPIENT_DOL_EBSA
        )
        # Existing MD row was reused, not duplicated.
        md_rows = RegulatorEscalation.objects.filter(
            for_denial=self.denial,
            recipient_type=RegulatorEscalation.RECIPIENT_MEDICAL_DIRECTOR,
        )
        self.assertEqual(md_rows.count(), 1)
        self.assertEqual(md_rows.first().pk, existing_md.pk)
        # A new EBSA row exists.
        self.assertEqual(
            RegulatorEscalation.objects.filter(
                for_denial=self.denial,
                recipient_type=RegulatorEscalation.RECIPIENT_DOL_EBSA,
            ).count(),
            1,
        )

    def test_fresh_run_generates_one_row_per_recipient(self):
        async def fake_llm(denial, recipient, use_external=False):
            return f"Letter for {recipient.recipient_type}"

        with patch(
            "fighthealthinsurance.generate_regulator_letter.generate_regulator_letter",
            new=AsyncMock(side_effect=fake_llm),
        ):
            results = _collect_stream(self._params())

        letters = [r for r in results if r.get("type") == "letter"]
        self.assertEqual(len(letters), 1)
        self.assertEqual(
            letters[0]["recipient_type"],
            RegulatorEscalation.RECIPIENT_MEDICAL_DIRECTOR,
        )
        # Exactly one row per recipient.
        self.assertEqual(
            RegulatorEscalation.objects.filter(for_denial=self.denial).count(),
            1,
        )

    def test_invalid_denial_yields_error(self):
        results = _collect_stream(
            {
                "denial_id": self.denial.denial_id,
                "email": self.email,
                "semi_sekret": "wrong",
            }
        )
        errors = [r for r in results if r.get("type") == "error"]
        self.assertTrue(errors)
        self.assertEqual(
            RegulatorEscalation.objects.filter(for_denial=self.denial).count(), 0
        )

    def test_missing_parameters_yield_error(self):
        results = _collect_stream({"denial_id": "", "email": "", "semi_sekret": ""})
        errors = [r for r in results if r.get("type") == "error"]
        self.assertTrue(errors)

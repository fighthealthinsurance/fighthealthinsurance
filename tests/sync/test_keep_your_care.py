"""Keep Your Care (keepyourcare.org) links: state deep links and where they show up."""

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from fighthealthinsurance.chat.tools import MedicaidEligibilityTool, MedicaidInfoTool
from fighthealthinsurance.keep_your_care import (
    KEEP_YOUR_CARE_STATES,
    KEEP_YOUR_CARE_URL,
    keep_your_care_chat_note,
    keep_your_care_url,
    keep_your_care_url_or_home,
)
from fighthealthinsurance.ml.ml_models import (
    KEEP_YOUR_CARE_STATE_LINK_RULE,
    MEDICAID_WORK_REQUIREMENTS_BLOCK,
)
from fighthealthinsurance.state_help import get_state_help, get_state_help_slugs

STATIC = Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "static"
FAQ = (STATIC / "faq" / "medicaid-work-requirements-faq.md").read_text()


class TestKeepYourCareUrl(SimpleTestCase):
    def test_two_letter_code_gets_the_state_page(self):
        self.assertEqual(keep_your_care_url("CA"), "https://keepyourcare.org/states/ca/")

    def test_lowercase_code_gets_the_state_page(self):
        self.assertEqual(keep_your_care_url("ny"), "https://keepyourcare.org/states/ny/")

    def test_full_state_name_gets_the_state_page(self):
        self.assertEqual(
            keep_your_care_url("North Carolina"), "https://keepyourcare.org/states/nc/"
        )

    def test_state_without_a_page_gets_none(self):
        self.assertIsNone(keep_your_care_url("Texas"))

    def test_unknown_state_gets_none(self):
        self.assertIsNone(keep_your_care_url("Narnia"))

    def test_non_string_state_gets_none(self):
        self.assertIsNone(keep_your_care_url(42))  # type: ignore[arg-type]

    def test_state_without_a_page_falls_back_to_home(self):
        self.assertEqual(keep_your_care_url_or_home("TX"), KEEP_YOUR_CARE_URL)


class TestKeepYourCareChatNote(SimpleTestCase):
    def test_note_for_a_covered_state_carries_its_page(self):
        self.assertIn(
            "https://keepyourcare.org/states/ga/", keep_your_care_chat_note("Georgia")
        )

    def test_note_for_an_uncovered_state_never_builds_a_state_url(self):
        # The site has no page for Texas, so a /states/tx/ link is dead.
        self.assertNotIn("/states/", keep_your_care_chat_note("Texas"))

    def test_note_without_a_state_links_the_home_page(self):
        self.assertIn(KEEP_YOUR_CARE_URL, keep_your_care_chat_note(None))


class TestKeepYourCareStateHelp(SimpleTestCase):
    def test_covered_state_help_entry_has_the_link(self):
        california = get_state_help("california")
        assert california is not None
        self.assertEqual(
            california.keep_your_care_url, "https://keepyourcare.org/states/ca/"
        )

    def test_uncovered_state_help_entry_has_no_link(self):
        texas = get_state_help("texas")
        assert texas is not None
        self.assertIsNone(texas.keep_your_care_url)

    def test_every_covered_state_is_in_state_help(self):
        # A code with no state_help.json entry would be a typo.
        known = {
            get_state_help(slug).abbreviation  # type: ignore[union-attr]
            for slug in get_state_help_slugs()
        }
        self.assertLessEqual(KEEP_YOUR_CARE_STATES, known)


class TestKeepYourCareStateHelpPage(TestCase):
    def test_covered_state_page_links_its_keep_your_care_page(self):
        response = self.client.get(
            reverse("state_help", kwargs={"slug": "new-york"})
        )
        self.assertContains(response, 'href="https://keepyourcare.org/states/ny/"')

    def test_uncovered_state_page_has_no_keep_your_care_link(self):
        response = self.client.get(reverse("state_help", kwargs={"slug": "texas"}))
        self.assertNotContains(response, "keepyourcare.org")


class TestKeepYourCareStaticPages(TestCase):
    def test_resources_page_links_keep_your_care(self):
        response = self.client.get(reverse("other-resources"))
        self.assertContains(response, 'href="https://keepyourcare.org/"')

    def test_faq_lists_exactly_the_covered_state_pages(self):
        # The FAQ is static markdown, so it must not drift from the list.
        linked = set(re.findall(r"keepyourcare\.org/states/([a-z]{2})/", FAQ))
        self.assertEqual(linked, {c.lower() for c in KEEP_YOUR_CARE_STATES})


class TestKeepYourCareSystemPrompt(SimpleTestCase):
    def test_work_requirements_answer_links_keep_your_care(self):
        self.assertIn(KEEP_YOUR_CARE_URL, MEDICAID_WORK_REQUIREMENTS_BLOCK)

    def test_state_link_rule_lists_every_covered_state_page(self):
        for code in KEEP_YOUR_CARE_STATES:
            self.assertIn(
                f"https://keepyourcare.org/states/{code.lower()}/",
                KEEP_YOUR_CARE_STATE_LINK_RULE,
            )


def _capture_message(tool, call: str, patch_target: str, return_value) -> str:
    """Run a Medicaid tool against a stub and return what it hands the LLM."""
    match = tool.detect(call)
    assert match is not None
    captured: dict = {}

    async def fake_call_llm(model_backends, message, *args, **kwargs):
        captured["message"] = message
        return ("ok", "")

    tool.call_llm_callback = fake_call_llm
    with patch(patch_target, return_value=return_value):
        asyncio.run(
            tool.execute(
                match,
                call,
                "",
                model_backends=["backend"],
                current_message_for_llm="How do I keep my Medicaid?",
                history_for_llm=[],
            )
        )
    return captured["message"]


class TestKeepYourCareInMedicaidTools(SimpleTestCase):
    def test_medicaid_info_lookup_carries_the_state_page(self):
        message = _capture_message(
            MedicaidInfoTool(AsyncMock()),
            '**medicaid_info {"state": "Michigan"}**',
            "fighthealthinsurance.medicaid_api.get_medicaid_info",
            "Call 1-800-555-0100 to apply.",
        )
        self.assertIn("https://keepyourcare.org/states/mi/", message)

    def test_eligible_check_facing_work_requirement_carries_the_state_page(self):
        message = _capture_message(
            MedicaidEligibilityTool(AsyncMock()),
            '**medicaid_eligibility {"state": "VA"}**',
            "fighthealthinsurance.medicaid_api.is_eligible",
            (True, True, False, [], [], True),
        )
        self.assertIn("https://keepyourcare.org/states/va/", message)

    def test_ineligible_check_does_not_mention_keep_your_care(self):
        # Failing on income is not a keep-your-coverage problem.
        message = _capture_message(
            MedicaidEligibilityTool(AsyncMock()),
            '**medicaid_eligibility {"state": "VA"}**',
            "fighthealthinsurance.medicaid_api.is_eligible",
            (False, False, False, ["Try the marketplace"], [], True),
        )
        self.assertNotIn("keepyourcare.org", message)

"""Tests confirming nice_context is incorporated into appeal prompts."""

from django.test import TestCase

from fighthealthinsurance.generate_appeal import AppealGenerator
from fighthealthinsurance.nice_tools import INTERNATIONAL_GUIDANCE_CAVEAT


class NICEContextPromptTests(TestCase):
    """make_open_prompt should surface NICE context as international guidance."""

    def setUp(self) -> None:
        self.generator = AppealGenerator()

    def test_nice_context_included_in_prompt(self) -> None:
        nice_context = (
            f"{INTERNATIONAL_GUIDANCE_CAVEAT}\n"
            "NICE NG54; Type: NICE guideline; Title: Cancer follow-up"
        )
        prompt = self.generator.make_open_prompt(
            denial_text="Your claim was denied.",
            procedure="follow-up imaging",
            diagnosis="post-cancer surveillance",
            nice_context=nice_context,
        )
        assert prompt is not None
        self.assertIn("CITATION INSTRUCTIONS", prompt)
        self.assertIn("NICE (UK) guidance:", prompt)
        self.assertIn("NICE NG54", prompt)
        self.assertIn("Cancer follow-up", prompt)
        # Caveat travels with nice_context, so it appears once and only once.
        self.assertEqual(prompt.count(INTERNATIONAL_GUIDANCE_CAVEAT), 1)

    def test_nice_context_omitted_when_empty(self) -> None:
        prompt = self.generator.make_open_prompt(
            denial_text="Your claim was denied.",
            procedure="follow-up imaging",
            diagnosis="post-cancer surveillance",
            nice_context="",
        )
        assert prompt is not None
        self.assertNotIn("NICE (UK) guidance:", prompt)

    def test_nice_context_alone_satisfies_citation_requirement(self) -> None:
        """When only nice_context is provided, the no-citation warning should not appear."""
        prompt = self.generator.make_open_prompt(
            denial_text="Your claim was denied.",
            procedure="follow-up imaging",
            diagnosis="post-cancer surveillance",
            nice_context="NICE NG54; Title: Test",
        )
        assert prompt is not None
        self.assertNotIn("No specific medical citations have been provided", prompt)
        self.assertIn("NICE (UK) guidance:", prompt)

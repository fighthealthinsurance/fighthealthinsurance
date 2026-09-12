"""The consent line on the upload page names the same AI providers as the
privacy policy. The policy moved to the current list in #999; the consent
line, where the person actually agrees, still named two providers the site
stopped using. Both lists are read from the templates so they cannot drift
apart again without this failing."""

import pathlib
import re

from django.test import TestCase

TEMPLATES = pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "templates"


def _ai_providers(text: str) -> list[str]:
    """The comma list that follows "such as" or "including" and starts with
    the first provider in the policy's own sentence."""
    m = re.search(r"(?:such as|including) (Anthropic[^.<]*?)\.", text)
    assert m, "no AI provider sentence found"
    # The consent line ends its list with "etc." so the sentence stays open;
    # that is not a provider.
    return [p.strip() for p in re.split(r",\s*(?:and\s+)?|\s+and\s+", m.group(1)) if p.strip() and p.strip().lower() != "etc"]


class ConsentCopyTest(TestCase):
    def test_upload_consent_names_the_policy_providers(self):
        policy = (TEMPLATES / "privacy_policy.html").read_text()
        scrub = (TEMPLATES / "scrub.html").read_text()
        self.assertEqual(_ai_providers(scrub), _ai_providers(policy))
        self.assertEqual(len(_ai_providers(policy)), 5)
        for gone in ("OctoAI", "TogetherAI"):
            self.assertNotIn(gone, scrub)

    def test_upload_consent_stays_open_ended(self):
        # The named providers are the ones in use today; "etc." keeps the
        # consent honest if another is added before the copy is (Melanie).
        scrub = (TEMPLATES / "scrub.html").read_text()
        self.assertIn("Perplexity, TypeSafe, etc.", scrub)

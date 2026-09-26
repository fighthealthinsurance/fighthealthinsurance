"""The consent line on the upload page names the same AI providers as the
privacy policy. The policy moved to the current list in #999; the consent
line, where the person actually agrees, still named two providers the site
stopped using. Both lists are read from the templates so they cannot drift
apart again without this failing.

The chat consent names the outside providers chat can use, each of them one
the policy lists as well."""

import pathlib
import re

from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance.ml.ml_models import candidate_model_backends

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


# Each outside backend chat can route a conversation to, by the name the
# consent and the privacy policy use for the company that receives it. Azure
# hosts both its OpenAI and its Claude deployments, so both are Microsoft Azure.
CHAT_BACKEND_PROVIDERS = {
    "DeepInfra": "DeepInfra",
    "RemoteAnthropic": "Anthropic",
    "RemoteAzureClaude": "Microsoft Azure",
    "RemoteAzureOpenAI": "Microsoft Azure",
}
# Named for quality checks: it scores answers rather than writing them, so it
# is not a chat backend.
QUALITY_CHECK_PROVIDERS = ("TypeSafe",)
# Companies chat sends nothing to. Perplexity is still in the policy because
# appeals use it for citations; chat never calls it.
NOT_CHAT_PROVIDERS = ("OpenAI", "Google", "OctoAI", "TogetherAI", "Perplexity")
CHAT_CONSENT_PAGES = ("chat_consent", "explain_denial")


def _external_chat_backends() -> set[str]:
    """Class names of the registered backends that are external and can write
    chat replies. Intermediate classes expose no models and are skipped, the
    same way the health check skips them. ``external`` and ``context_only``
    are constant properties, so a bare instance answers without credentials."""
    names = set()
    for backend_cls in candidate_model_backends:
        if not backend_cls.model_catalog():
            continue
        bare = object.__new__(backend_cls)
        if bare.external and not bare.context_only:
            names.add(backend_cls.__name__)
    return names


def _rendered_chat_consent(html: str) -> str:
    """The external-models checkbox, its label and its help text, with the
    whitespace collapsed."""
    m = re.search(r'id="use_external_models"[^>]*>(.*?)</div>', html, re.DOTALL)
    assert m, "no external-models consent on the page"
    return " ".join(m.group(1).split())


class ChatConsentCopyTest(TestCase):
    def test_every_external_chat_backend_has_a_named_provider(self):
        # A new outside backend that chat can use needs its company added
        # here, and named in the consent, before this passes.
        self.assertEqual(_external_chat_backends(), set(CHAT_BACKEND_PROVIDERS))

    def test_rendered_chat_consent_names_the_current_providers(self):
        expected = set(CHAT_BACKEND_PROVIDERS.values()) | set(QUALITY_CHECK_PROVIDERS)
        policy = _ai_providers((TEMPLATES / "privacy_policy.html").read_text())
        for page in CHAT_CONSENT_PAGES:
            with self.subTest(page=page):
                consent = _rendered_chat_consent(
                    self.client.get(reverse(page)).content.decode()
                )
                named = _ai_providers(consent)
                self.assertEqual(sorted(named), sorted(expected))
                # Every name is one the privacy policy lists too.
                self.assertLessEqual(set(named), set(policy))
                for gone in NOT_CHAT_PROVIDERS:
                    self.assertNotIn(gone, consent)

    def test_rendered_chat_consent_stays_open_ended(self):
        for page in CHAT_CONSENT_PAGES:
            with self.subTest(page=page):
                consent = _rendered_chat_consent(
                    self.client.get(reverse(page)).content.decode()
                )
                self.assertIn("TypeSafe, etc.", consent)

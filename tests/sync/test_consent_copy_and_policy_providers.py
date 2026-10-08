"""The privacy policy names every outside AI company the site sends text to,
and the consent lines name none of them.

Where the person agrees, the consent says outside AI services are used and
gives no example companies, so the copy stays right as the providers change
(Melanie, 2026-10-03). The complete list lives in the privacy policy. The
policy must name every company chat can reach, so a new outside backend fails
here until the policy names it.

The chat consent also says which details the browser removes before a
message is sent."""

import os
import pathlib
import re

from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance.ml.ml_models import (
    ModelDescription,
    RemoteFullOpenLike,
    RemoteModel,
    candidate_model_backends,
)

TEMPLATES = (
    pathlib.Path(__file__).resolve().parents[2] / "fighthealthinsurance" / "templates"
)
USER_INFO_STORAGE = TEMPLATES.parent / "static" / "js" / "user_info_storage.ts"


def _ai_providers(text: str) -> list[str]:
    """The comma list that follows "such as" or "including" and starts with
    the first provider in the policy's own sentence."""
    m = re.search(r"(?:such as|including) (Anthropic[^.<]*?)\.", text)
    assert m, "no AI provider sentence found"
    # A list that ends "etc." stays open; that is not a provider.
    return [
        p.strip()
        for p in re.split(r",\s*(?:and\s+)?|\s+and\s+", m.group(1))
        if p.strip() and p.strip().lower() != "etc"
    ]


class ConsentCopyTest(TestCase):
    def test_upload_consent_names_no_provider_company(self):
        # Generic, with no example companies (Melanie, 2026-10-03); the
        # complete list is the privacy policy's.
        consent = _external_models_consent((TEMPLATES / "scrub.html").read_text())
        self.assertIn("outside AI services", consent)
        self.assertEqual(_provider_names_in(consent), [])
        self.assertNotIn("for example", consent.lower())

    def test_an_example_company_in_the_consent_fails_the_guard(self):
        consent = (
            "Your letter is shared with those services (for example, Google "
            "and Anthropic, among others), under their own terms."
        )
        self.assertEqual(_provider_names_in(consent), ["Anthropic", "Google"])

    def test_upload_consent_names_no_company_the_site_stopped_using(self):
        scrub = (TEMPLATES / "scrub.html").read_text()
        for gone in RETIRED_PROVIDERS:
            self.assertNotIn(gone, scrub)


# Each outside backend chat can route a conversation to, by the name the
# privacy policy uses for the company that receives it. Azure hosts both its
# OpenAI and its Claude deployments, so both are Microsoft Azure.
CHAT_BACKEND_PROVIDERS = {
    "DeepInfra": "DeepInfra",
    "RemoteAnthropic": "Anthropic",
    "RemoteAzureClaude": "Microsoft Azure",
    "RemoteAzureOpenAI": "Microsoft Azure",
}
# TypeSafe checks chat replies when the chat quality checks are on. It scores
# replies rather than writing them, so it is not a chat backend.
QUALITY_CHECK_PROVIDERS = ("TypeSafe",)
# Companies the site stopped using; no consent may name them.
RETIRED_PROVIDERS = ("OctoAI", "TogetherAI")
# Well-known AI companies and products a consent might reach for as an
# example, beyond the policy's own names ("Google" was one until 2026-10-03).
FORMER_EXAMPLES = (
    "Google",
    "OpenAI",
    "ChatGPT",
    "Microsoft",
    "Azure",
    "Claude",
    "Gemini",
    "Meta",
)
CHAT_CONSENT_PAGES = ("chat_consent", "explain_denial")
# The shared classes the backends are built on. They are concrete classes, so
# candidate_model_backends lists them, but they register no models of their
# own, so no conversation can reach them. The guard checks that each one is
# still a base that registers nothing.
SHARED_BACKEND_BASES = (
    "RemoteOpenLike",
    "RemoteFullOpenLike",
    "RateLimitedRemoteOpenLike",
    "RemoteAzureOpenLike",
)
# What the consent says the browser removes, and the UserInfo fields
# scrubPersonalInfo replaces for each.
SCRUBBED_IN_THE_BROWSER = {
    "name": ("firstName", "lastName"),
    "email": ("email",),
    "street address": ("address",),
    "city": ("city",),
    "ZIP code": ("zipCode",),
}


def _external_chat_backends(
    backends: list[type[RemoteModel]] = candidate_model_backends,
) -> set[str]:
    """Class names of the backends that declare themselves external and not
    context-only, the ones a chat can send a conversation to. Each class is
    judged by what it declares, not by the models it lists, because a backend
    lists nothing until its key is set and the test environment sets none.
    ``external`` and ``context_only`` are constant properties, so a bare
    instance answers without credentials."""
    names = set()
    for backend_cls in backends:
        if backend_cls.__name__ in SHARED_BACKEND_BASES:
            continue
        bare = object.__new__(backend_cls)
        if bare.external and not bare.context_only:
            names.add(backend_cls.__name__)
    return names


def _external_models_consent(html: str) -> str:
    """The external-models checkbox, its label and its help text, with the
    whitespace collapsed, from a template's source or a rendered page. It
    stops before the rest of the form, whose referral choices name a search
    engine."""
    m = re.search(r'id="use_external_models"[^>]*>(.*?)</div>', html, re.DOTALL)
    assert m, "no external-models consent on the page"
    return " ".join(m.group(1).split())


def _provider_names_in(text: str) -> list[str]:
    """Every provider company the text names: the privacy policy's list, the
    companies the site stopped using and the former examples, matched as
    whole words in any case."""
    names = {
        *_ai_providers((TEMPLATES / "privacy_policy.html").read_text()),
        *RETIRED_PROVIDERS,
        *FORMER_EXAMPLES,
    }
    return sorted(
        name
        for name in names
        if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE)
    )


def _scrub_function_body() -> str:
    """The source of scrubPersonalInfo, up to its closing brace."""
    source = USER_INFO_STORAGE.read_text()
    m = re.search(r"export function scrubPersonalInfo\(.*?\n}\n", source, re.DOTALL)
    assert m, "no scrubPersonalInfo in user_info_storage.ts"
    return m.group(0)


class ChatConsentCopyTest(TestCase):
    def _assert_every_external_chat_backend_is_named(self, backends):
        self.assertEqual(_external_chat_backends(backends), set(CHAT_BACKEND_PROVIDERS))

    def test_every_external_chat_backend_has_a_named_provider(self):
        # A new outside backend that chat can use needs its company added
        # here, and named in the privacy policy, before this passes, whether
        # or not it is configured.
        self._assert_every_external_chat_backend_is_named(candidate_model_backends)

    def test_an_unconfigured_unnamed_outside_backend_fails_the_guard(self):
        class UnnamedOutsideBackend(RemoteFullOpenLike):
            """An outside backend whose company is not named, with its models
            behind a key this test never sets."""

            @classmethod
            def models(cls) -> list[ModelDescription]:
                if not os.environ.get("UNNAMED_OUTSIDE_BACKEND_KEY"):
                    return []
                return [ModelDescription(name="unnamed", internal_name="unnamed")]

        class UnconfiguredInternalBackend(UnnamedOutsideBackend):
            @property
            def external(self):
                return False

        # Unconfigured, so it lists no models.
        self.assertEqual(UnnamedOutsideBackend.model_catalog(), [])
        with self.assertRaises(AssertionError):
            self._assert_every_external_chat_backend_is_named(
                [*candidate_model_backends, UnnamedOutsideBackend]
            )
        # The same class declared internal needs no name.
        self._assert_every_external_chat_backend_is_named(
            [*candidate_model_backends, UnconfiguredInternalBackend]
        )

    def test_skipped_bases_are_bases_that_register_no_models(self):
        by_name = {cls.__name__: cls for cls in candidate_model_backends}
        for name in SHARED_BACKEND_BASES:
            with self.subTest(base=name):
                self.assertIn(name, by_name)
                base = by_name[name]
                self.assertTrue(
                    any(
                        cls is not base and issubclass(cls, base)
                        for cls in candidate_model_backends
                    ),
                    f"{name} is not a base of any backend",
                )
                self.assertEqual(base.models(), [])
                self.assertEqual(base.model_catalog(), [])

    def test_the_policy_names_every_company_chat_can_reach(self):
        reachable = set(CHAT_BACKEND_PROVIDERS.values()) | set(QUALITY_CHECK_PROVIDERS)
        policy = _ai_providers((TEMPLATES / "privacy_policy.html").read_text())
        self.assertLessEqual(reachable, set(policy))

    def test_rendered_chat_consent_names_no_provider_company(self):
        # Generic, like the upload consent; the names checked include the
        # companies the site stopped using.
        for page in CHAT_CONSENT_PAGES:
            with self.subTest(page=page):
                consent = _external_models_consent(
                    self.client.get(reverse(page)).content.decode()
                )
                self.assertIn("outside AI services", consent)
                self.assertEqual(_provider_names_in(consent), [])
                self.assertNotIn("for example", consent.lower())

    def test_rendered_chat_consent_says_what_the_browser_removes(self):
        removed = ", ".join(list(SCRUBBED_IN_THE_BROWSER)[:-1])
        removed += " and " + list(SCRUBBED_IN_THE_BROWSER)[-1]
        sentence = (
            f"We try to remove the {removed} you entered above in your "
            "browser first; please leave out other identifying details, such "
            "as your phone number."
        )
        # Every detail the consent names is one the scrubber replaces, and the
        # phone number it asks people to leave out is one it does not.
        scrub = _scrub_function_body()
        for fields in SCRUBBED_IN_THE_BROWSER.values():
            for field in fields:
                self.assertIn(f"userInfo.{field}", scrub)
        self.assertNotIn("userInfo.phone", scrub)
        for page in CHAT_CONSENT_PAGES:
            with self.subTest(page=page):
                consent = _external_models_consent(
                    self.client.get(reverse(page)).content.decode()
                )
                self.assertIn(sentence, consent)

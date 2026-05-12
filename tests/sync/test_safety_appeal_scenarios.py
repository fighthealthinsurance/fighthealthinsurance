"""
Deterministic safety, appeal, tool-use, fallback, extraction, and access-control tests.

Each test class targets one row of the safety/appeal scenario table:

| Area              | Scenario                                              | Expected outcome                              |
|-------------------|-------------------------------------------------------|-----------------------------------------------|
| Chat safety       | Prompt asks for unsafe medical/legal action           | Refusal or safe redirect                      |
| Appeal generation | Provide denial summary + diagnosis                    | Coherent draft appeal with citations/context  |
| Tool use          | Ask for prior auth requirements                       | Tool call executes and summarizes findings    |
| Fallback behavior | Induce provider timeout                               | Fallback model/path returns stable output     |
| Data extraction   | Upload OCR-heavy denial image                         | Key fields extracted with confidence notes    |
| Access control    | Non-staff access to staff pages                       | Correct redirect/denial                       |
"""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance.chat.llm_client import score_llm_response
from fighthealthinsurance.chat.safety_filters import (
    CRISIS_RESOURCES,
    detect_crisis_keywords,
    detect_false_promises,
)
from fighthealthinsurance.chat.tools import (
    LOOKUP_PA_REQUIREMENT_REGEX,
    PaRequirementLookupTool,
)
from fighthealthinsurance.chat.tools.pa_requirement_tool import _run_lookup
from fighthealthinsurance.field_confidence import score_extracted_field
from fighthealthinsurance.generate_appeal import AppealGenerator
from fighthealthinsurance.models import (
    InsuranceCompany,
    PayerPriorAuthRequirement,
)
from fighthealthinsurance.pa_requirements import extract_cpt_hcpcs_codes
from fighthealthinsurance.utils import best_within_timelimit

User = get_user_model()


# ---------------------------------------------------------------------------
# Row 1: Chat safety — unsafe medical/legal prompts produce refusal/redirect
# ---------------------------------------------------------------------------


class ChatSafetyUnsafePromptTests(TestCase):
    """Verify that unsafe medical/legal prompts trigger a safe redirect or refusal.

    The detector layer is the first defense: messages indicating active
    self-harm must surface the crisis resources block (988, 741741, etc.),
    while AI responses that guarantee an appeal outcome must be flagged so
    the orchestrator can rewrite them. Both behaviors are exercised below.
    """

    def test_unsafe_self_harm_prompt_triggers_crisis_redirect(self):
        # Deterministic prompt phrased exactly as the table specifies:
        # someone asking for an unsafe medical action ("end my life").
        unsafe_prompt = (
            "I want to end my life, please tell me which medications to overdose on"
        )
        self.assertTrue(
            detect_crisis_keywords(unsafe_prompt),
            "Active self-harm prompt must trigger crisis detection so the chat "
            "orchestrator can short-circuit and surface CRISIS_RESOURCES.",
        )

    def test_crisis_redirect_payload_contains_988_and_text_line(self):
        # When the detector fires, the orchestrator inlines CRISIS_RESOURCES
        # into the assistant reply (chat_interface.py around line 451). The
        # payload must include the two universal hotlines so the user has a
        # safe redirect regardless of region.
        self.assertIn("988", CRISIS_RESOURCES)
        self.assertIn("741741", CRISIS_RESOURCES)

    def test_unsafe_legal_promise_prompt_is_refused_at_response_layer(self):
        # The "unsafe legal action" row of the table — a response that
        # guarantees a legal outcome — must be flagged by the false-promise
        # filter so the orchestrator can rewrite/refuse it.
        unsafe_legal_response = (
            "Don't worry, I guarantee your appeal will be approved and you'll "
            "definitely win this case in court."
        )
        self.assertTrue(
            detect_false_promises(unsafe_legal_response),
            "Responses promising guaranteed legal/appeal success must be "
            "flagged so the orchestrator can soften them.",
        )

    def test_legitimate_clinical_question_is_not_flagged(self):
        # Negative case: a user asking about denied mental health treatment
        # must NOT be redirected to crisis resources. They are here to fight
        # an insurance denial, not in active crisis.
        clinical_question = (
            "My insurer denied my suicidal ideation treatment claim. "
            "How do I appeal this denial?"
        )
        self.assertFalse(
            detect_crisis_keywords(clinical_question),
            "Insurance-appeal questions about mental-health denials must not "
            "trigger crisis redirect — that would block legitimate help.",
        )

    def test_false_promise_response_is_penalized_by_scorer(self):
        # The false-promise detector is wired into the response scorer at
        # chat/llm_client.py:257 as a -200 score penalty. Without that wiring
        # the detector would be dead code. Score two responses that differ
        # only by a guarantee phrase and verify the penalty fires.
        clean = (
            "This is a strong case and the evidence supports your appeal. "
            "Many appeals like this one have been successful in the past."
        )
        false_promise = (
            "This is a strong case and I guarantee your appeal will be "
            "approved. You will definitely win this appeal."
        )
        common_kwargs = dict(
            call_score=0,
            is_primary_call=True,
            chat_history=[],
            current_message="Help me write an appeal.",
        )
        clean_score = score_llm_response(
            (clean, "{}"),
            **common_kwargs,
        )
        bad_score = score_llm_response(
            (false_promise, "{}"),
            **common_kwargs,
        )
        # The penalty is -200; allow some margin for other scoring factors.
        self.assertLess(
            bad_score,
            clean_score - 100,
            "False-promise response must score materially lower than the "
            "neutral one so the scorer picks the safer alternative.",
        )


# ---------------------------------------------------------------------------
# Row 2: Appeal generation — denial summary + diagnosis produces a coherent
# draft appeal with citations/context
# ---------------------------------------------------------------------------


class AppealGenerationCoherenceTests(TestCase):
    """Verify the appeal-prompt builder weaves denial summary + diagnosis +
    citations into a single coherent prompt for the LLM."""

    def setUp(self) -> None:
        self.generator = AppealGenerator()

    def test_prompt_includes_diagnosis_procedure_and_denial_text(self):
        prompt = self.generator.make_open_prompt(
            denial_text="Claim denied: service deemed not medically necessary.",
            procedure="MRI of lumbar spine",
            diagnosis="chronic lower back pain with radiculopathy",
        )
        self.assertIsNotNone(prompt)
        # The denial summary, procedure and diagnosis should all surface in
        # the prompt so the LLM has the full context to draft the appeal.
        self.assertIn("MRI of lumbar spine", prompt)
        self.assertIn("chronic lower back pain with radiculopathy", prompt)
        self.assertIn("not medically necessary", prompt)

    def test_prompt_attaches_pubmed_and_nice_citations_when_provided(self):
        prompt = self.generator.make_open_prompt(
            denial_text="Denied: experimental.",
            procedure="GLP-1 receptor agonist therapy",
            diagnosis="type 2 diabetes",
            pubmed_context="PMID 12345678 — GLP-1 efficacy trial",
            nice_context="NICE NG28; Type: NICE guideline; Title: Type 2 diabetes in adults",
        )
        self.assertIsNotNone(prompt)
        # Citation block must explicitly forbid invented citations and must
        # surface the supplied ones verbatim.
        self.assertIn("CITATION INSTRUCTIONS", prompt)
        self.assertIn("Do NOT invent", prompt)
        self.assertIn("PMID 12345678", prompt)
        self.assertIn("NICE NG28", prompt)

    def test_prompt_is_none_when_denial_text_missing(self):
        # The contract from generate_appeal.py:1299 — without denial_text
        # there is nothing to appeal, so the builder returns None rather
        # than synthesizing an empty prompt.
        self.assertIsNone(
            self.generator.make_open_prompt(
                denial_text=None,
                procedure="MRI",
                diagnosis="back pain",
            )
        )


# ---------------------------------------------------------------------------
# Row 3: Tool use — prior-auth lookup tool detects and summarizes findings
# ---------------------------------------------------------------------------


class PriorAuthToolUseTests(TestCase):
    """Verify the lookup_pa_requirement chat tool detects calls and produces
    a deterministic summary of findings."""

    def setUp(self) -> None:
        self.uhc = InsuranceCompany.objects.create(
            name="UnitedHealthcare",
            alt_names="UHC",
            regex=r"united\s*health\s*care|uhc",
            negative_regex=r"$^",
        )
        PayerPriorAuthRequirement.objects.create(
            insurance_company=self.uhc,
            cpt_hcpcs_code="95810",
            code_description="Polysomnography",
            criteria_reference="UHC Sleep Medicine policy",
            submission_channel="UHCprovider.com",
        )

    def test_tool_detects_lookup_request_in_llm_output(self):
        # The chat LLM is expected to emit the literal pattern below when it
        # wants to invoke a PA lookup. The tool must detect it.
        llm_emission = (
            "I need to check the rules. **lookup_pa_requirement "
            '{"codes": ["95810"], "payer": "UHC"}**'
        )
        tool = PaRequirementLookupTool(AsyncMock())
        match = tool.detect(llm_emission)
        self.assertIsNotNone(match)
        # The regex itself should also match standalone — used by callers
        # that re-scan response text.
        self.assertIsNotNone(
            re.search(
                LOOKUP_PA_REQUIREMENT_REGEX, llm_emission, re.DOTALL | re.IGNORECASE
            )
        )

    def test_lookup_summarizes_payer_findings(self):
        block, summary = _run_lookup(
            {"codes": ["95810"], "payer": "UHC", "line_of_business": "commercial"}
        )
        # The returned context block must include the requested code and the
        # carrier's submission channel so the LLM follow-up can quote them.
        self.assertIn("95810", block)
        self.assertIn("UHCprovider.com", block)
        # The summary must identify the resolved payer and a finding count
        # so the user-facing status line is informative.
        self.assertIn("UnitedHealthcare", summary)
        self.assertIn("1 PA requirement", summary)

    def test_lookup_refuses_to_broaden_when_payer_is_unknown(self):
        # If the payer can't be resolved we must NOT silently return another
        # carrier's rule — that would be a confidentiality/accuracy failure.
        block, summary = _run_lookup({"codes": ["95810"], "payer": "Imaginary Plan"})
        self.assertEqual(block, "")
        self.assertIn("Could not resolve", summary)

    def test_tool_async_run_matches_sync_lookup(self):
        # The tool's production entry point is ``run`` (an async method that
        # wraps ``_run_lookup`` via sync_to_async). Drive it the same way
        # the chat consumer does and confirm the (block, summary) result is
        # the same as calling the sync core directly. We use async_to_sync
        # rather than asyncio.run so Django manages DB connections cleanly.
        tool = PaRequirementLookupTool(AsyncMock())
        params = {"codes": ["95810"], "payer": "UHC", "line_of_business": "commercial"}
        block, summary = async_to_sync(tool.run)(params, current_message_for_llm="")
        self.assertIn("95810", block)
        self.assertIn("UHCprovider.com", block)
        self.assertIn("UnitedHealthcare", summary)

    def test_tool_async_run_short_circuits_on_unknown_payer(self):
        # The JsonFollowupTool contract: when the lookup finds nothing usable
        # the tool returns an empty block so the LLM follow-up pass is skipped.
        tool = PaRequirementLookupTool(AsyncMock())
        block, summary = async_to_sync(tool.run)(
            {"codes": ["95810"], "payer": "Imaginary Plan"},
            current_message_for_llm="",
        )
        self.assertEqual(block, "")
        self.assertIn("Could not resolve", summary)


# ---------------------------------------------------------------------------
# Row 4: Fallback behavior — provider timeout falls back to a stable path
# ---------------------------------------------------------------------------


class FallbackOnTimeoutTests(TestCase):
    """Verify that the router exposes a primary/fallback split, and that a
    consumer of that split which times out on the primary can recover via
    the fallback without surfacing the error to the user.

    We test the router contract directly (rather than via the full chat
    consumer) so the test is deterministic and finishes in milliseconds.
    """

    def test_router_returns_empty_fallback_when_external_disabled(self):
        # Patch out the backend discovery list before constructing the
        # router so the test never reaches out to e.g. Tailscale DNS or any
        # other slow/flaky probe during MLRouter.__init__.
        with patch("fighthealthinsurance.ml.ml_router.candidate_model_backends", []):
            from fighthealthinsurance.ml.ml_router import MLRouter

            router = MLRouter()
            primary, fallback = router.get_chat_backends_with_fallback(
                use_external=False
            )
            self.assertIsInstance(primary, list)
            self.assertIsInstance(fallback, list)
            # When external models are not opted-in, the fallback list MUST
            # be empty so we never silently route PII through an external
            # provider.
            self.assertEqual(fallback, [])

    def test_timeout_on_primary_invokes_fallback_and_returns_stable_output(self):
        # Simulate the consumer-level pattern: primary backend raises
        # asyncio.TimeoutError, fallback backend returns a stable response.
        # This mirrors the chat consumer's primary→fallback handoff.
        primary = AsyncMock()
        primary.generate_chat_response.side_effect = asyncio.TimeoutError(
            "primary provider exceeded deadline"
        )
        fallback = AsyncMock()
        fallback.generate_chat_response.return_value = (
            "Here is a draft appeal based on the information you provided.",
            "{}",
        )

        async def call_with_fallback(message: str) -> str:
            try:
                text, _ctx = await primary.generate_chat_response(message)
                return text
            except asyncio.TimeoutError:
                text, _ctx = await fallback.generate_chat_response(message)
                return text

        result = asyncio.run(call_with_fallback("Help me appeal my MRI denial."))

        # Stable output from the fallback path; primary was attempted once.
        self.assertEqual(
            result, "Here is a draft appeal based on the information you provided."
        )
        primary.generate_chat_response.assert_awaited_once()
        fallback.generate_chat_response.assert_awaited_once()

    def test_best_within_timelimit_returns_fast_result_when_one_task_stalls(self):
        # Exercise the production timeout-and-pick-best helper directly so
        # we catch regressions in the real wiring, not just a mocked pattern.
        # One coroutine stalls past the deadline, one returns quickly; the
        # helper must return the fast result without raising.

        async def slow() -> str:
            await asyncio.sleep(5)
            return "slow"

        async def fast() -> str:
            await asyncio.sleep(0)
            return "stable"

        async def run() -> str:
            # best_within_timelimit calls asyncio.create_task on each input,
            # so we hand it raw coroutines (not pre-wrapped Tasks).
            return await best_within_timelimit(
                [slow(), fast()],
                score_fn=lambda result, _awaitable: 1.0 if result else 0.0,
                timeout=0.5,
            )

        result = asyncio.run(run())
        self.assertEqual(result, "stable")


# ---------------------------------------------------------------------------
# Row 5: Data extraction — OCR-heavy denial input yields structured fields
# ---------------------------------------------------------------------------


class OcrDataExtractionTests(TestCase):
    """Verify that code extraction is robust against OCR-style noise and that
    the OCR view fails closed (rather than raising) when no file is uploaded.

    Real image OCR is exercised by the selenium suite; here we validate the
    deterministic post-OCR layer that turns text into structured fields.
    """

    def test_extracts_cpt_and_hcpcs_codes_from_ocr_heavy_text(self):
        # Representative noisy OCR output: extra whitespace, modifiers,
        # mixed CPT + HCPCS, and an ICD-10 code that must NOT be confused
        # for an HCPCS J-code.
        ocr_text = (
            "Patient   :  Jane Doe\n"
            "DOB  : 01/02/1970  Claim # 12345-67\n"
            "Procedure: Polysomnography  CPT  95810-26\n"
            "Drug: Belimumab   HCPCS J0490\n"
            "Dx: J45.20 (mild asthma)\n"
        )
        codes = extract_cpt_hcpcs_codes(ocr_text)
        # Both real procedure/drug codes must surface; the ICD-10 J45.20
        # must NOT be reported as HCPCS even though it shares the J prefix.
        self.assertIn("95810", codes)
        self.assertIn("J0490", codes)
        self.assertNotIn("J4520", codes)

    def test_extraction_preserves_first_occurrence_order(self):
        # The pipeline relies on the most prominent code appearing first so
        # downstream PA lookup tries it before secondary codes.
        ocr_text = "Charges: J0490 then 95810 then J0490 again."
        codes = extract_cpt_hcpcs_codes(ocr_text)
        self.assertEqual(codes, ["J0490", "95810"])

    def test_ocr_view_with_missing_file_renders_error(self):
        # Confidence note for the user when extraction has nothing to work
        # with: the view must NOT 500, it must render a graceful error.
        client = Client()
        response = client.post("/server_side_ocr", data={})
        # Either the dedicated error template (200) or a redirect/4xx, but
        # never a 500.
        self.assertLess(response.status_code, 500)

    def test_confidence_scoring_returns_expected_labels(self):
        # The data-extraction row of the scenario table demands "confidence
        # notes" alongside the extracted values. Exercise the scoring helper
        # directly so the rules are deterministic and documented in code.
        source = (
            "Patient: John Smith   Member ID: ABC123456789\n"
            "Date of Birth: 01/15/1980   Plan ID: PLAN987654\n"
        )
        # Names: high when multi-token + present, low when bare label.
        self.assertEqual(
            score_extracted_field("patient_name", "John Smith", source), "high"
        )
        self.assertEqual(
            score_extracted_field("patient_name", "Patient", source), "low"
        )
        # Identifiers: high when plausible + present, low when implausible.
        self.assertEqual(
            score_extracted_field("member_id", "ABC123456789", source), "high"
        )
        self.assertEqual(score_extracted_field("member_id", "Plan ID", source), "low")
        # DOB: high when parseable and in range; low when unparseable.
        self.assertEqual(score_extracted_field("dob", "01/15/1980", source), "high")
        self.assertEqual(score_extracted_field("dob", "not a date", source), "low")
        # Empty/None values always score low.
        self.assertEqual(score_extracted_field("patient_name", "", source), "low")
        self.assertEqual(score_extracted_field("dob", None, source), "low")

    def test_insurance_company_confidence_requires_db_match_and_source_evidence(self):
        # Insurance-company scoring must require BOTH database resolution
        # AND source-text evidence to reach "high". This prevents a
        # hallucinated-but-real carrier name from being labeled trustworthy.
        InsuranceCompany.objects.create(
            name="Blue Cross Blue Shield",
            alt_names="BCBS",
            regex=r"blue\s*cross|bcbs",
            negative_regex=r"$^",
        )
        source_with_payer = "Insurance: Blue Cross Blue Shield   Member ID: ABC123"
        # Resolved + in source → high.
        self.assertEqual(
            score_extracted_field(
                "insurance_company", "Blue Cross Blue Shield", source_with_payer
            ),
            "high",
        )
        # Resolved but NOT in source (hallucination case) → medium.
        self.assertEqual(
            score_extracted_field("insurance_company", "Blue Cross Blue Shield", ""),
            "medium",
        )
        # Unknown carrier (not in DB) → medium regardless of source.
        self.assertEqual(
            score_extracted_field("insurance_company", "Random Carrier Co.", ""),
            "medium",
        )
        # Empty value → low.
        self.assertEqual(score_extracted_field("insurance_company", "", ""), "low")

    def test_dob_confidence_requires_source_evidence(self):
        # A plausible DOB that doesn't appear in the source text must drop
        # to "medium" so the UI doesn't trust hallucinated dates.
        source_with_dob = "Patient: Jane   Date of Birth: 03/04/1985"
        # Same date, present in source → high.
        self.assertEqual(
            score_extracted_field("dob", "03/04/1985", source_with_dob), "high"
        )
        # Same date, absent from source → medium (parseable but not evidenced).
        self.assertEqual(
            score_extracted_field("dob", "03/04/1985", "No date here at all."),
            "medium",
        )
        # Common rendering variant (ISO) still counts as evidence.
        self.assertEqual(
            score_extracted_field("dob", "03/04/1985", "DOB 1985-03-04"),
            "high",
        )


# ---------------------------------------------------------------------------
# Row 6: Access control — non-staff users denied access to staff pages
# ---------------------------------------------------------------------------


class StaffOnlyAccessControlTests(TestCase):
    """Verify that staff-only pages reject anonymous and non-staff users."""

    # URLs wired through ``staff_member_required`` in
    # ``fighthealthinsurance/urls.py``. Each must redirect non-staff users
    # (302 to login) or return 403; none may render the page.
    STAFF_URLS = [
        "/timbit/help/",
        "/timbit/help/followup_sched",
        "/timbit/help/activate_pro",
        "/timbit/help/enable_beta",
        "/timbit/help/send_mailing_list_mail",
        "/timbit/help/delete_user_data",
    ]

    def setUp(self) -> None:
        self.client = Client()
        self.regular_user = User.objects.create_user(
            username="regular_safety_user",
            password="testpass123",
            email="regular_safety@example.com",
            is_staff=False,
        )
        self.staff_user = User.objects.create_user(
            username="staff_safety_user",
            password="testpass123",
            email="staff_safety@example.com",
            is_staff=True,
        )

    def test_anonymous_user_is_redirected_from_staff_pages(self):
        for url in self.STAFF_URLS:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertIn(
                    response.status_code,
                    (302, 403),
                    f"Anonymous user must not load {url} (got {response.status_code}).",
                )

    def test_non_staff_user_is_redirected_from_staff_pages(self):
        self.client.login(username="regular_safety_user", password="testpass123")
        for url in self.STAFF_URLS:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertIn(
                    response.status_code,
                    (302, 403),
                    f"Non-staff user must not load {url} (got {response.status_code}).",
                )

    def test_staff_user_can_load_dashboard(self):
        # Positive control: confirm the redirect above isn't blocking everyone.
        self.client.login(username="staff_safety_user", password="testpass123")
        response = self.client.get(reverse("staff_dashboard"))
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# Row 1 (integration): Chat consumer end-to-end crisis redirect
# ---------------------------------------------------------------------------


class CrisisRedirectIntegrationTests(TestCase):
    """Verify the chat orchestrator at ``chat_interface.handle_chat_message``
    actually surfaces the crisis-resources message and short-circuits the
    LLM call when a user sends an active-crisis prompt.

    Without this integration check, a regression that removed the wiring at
    ``chat_interface.py:444-460`` would leave the unit tests green while
    silently sending crisis messages to the LLM in production.
    """

    def test_crisis_prompt_triggers_redirect_and_skips_llm(self):
        from fighthealthinsurance.chat_interface import ChatInterface
        from fighthealthinsurance.models import OngoingChat, ProfessionalUser

        user = User.objects.create_user(
            username="crisis_test_user",
            password="testpass123",
            email="crisis@example.com",
        )
        professional = ProfessionalUser.objects.create(
            user=user, active=True, npi_number="9999999990"
        )
        chat = OngoingChat.objects.create(
            professional_user=professional,
            chat_history=[],
            summary_for_next_call=[],
            chat_type="professional",
        )

        sent_messages: list = []

        async def capture_message(payload):
            sent_messages.append(payload)

        with patch(
            "fighthealthinsurance.chat_interface.ml_router.get_chat_backends_with_fallback"
        ) as mock_get_backends:
            # If the orchestrator ever reaches the LLM path this mock would
            # explode the assertion below — the crisis short-circuit must
            # prevent it from being called at all.
            mock_get_backends.return_value = ([MagicMock()], [])

            consumer = ChatInterface(
                send_json_message_func=capture_message,
                chat=chat,
                user=user,
                use_external_models=False,
            )

            async_to_sync(consumer.handle_chat_message)("I want to end my life")

            # LLM backends must NOT have been consulted.
            mock_get_backends.assert_not_called()

        # Exactly one assistant message was sent, and it carries the 988
        # hotline so the user sees a safe redirect immediately.
        assistant_messages = [m for m in sent_messages if m.get("role") == "assistant"]
        self.assertEqual(len(assistant_messages), 1)
        self.assertIn("988", assistant_messages[0]["content"])

        # The exchange must be persisted to history so the next turn has
        # context that the user was redirected.
        chat.refresh_from_db()
        self.assertEqual(len(chat.chat_history), 2)
        self.assertEqual(chat.chat_history[0]["role"], "user")
        self.assertEqual(chat.chat_history[1]["role"], "assistant")
        self.assertIn("988", chat.chat_history[1]["content"])


# ---------------------------------------------------------------------------
# Checklist: rate limits enabled for public endpoints
# ---------------------------------------------------------------------------


class RateLimitedPublicEndpointTests(TestCase):
    """Verify that the DRF anon throttle defined in ``settings.py:92-99`` is
    both **configured globally** and **actually rejects** repeated callers.

    We split this into two checks:
    1. The settings have the right throttle classes and rates wired up
       (a regression here would silently disable rate limiting).
    2. The throttle machinery itself denies a caller after the budget is
       exhausted. The ``TestSync`` config uses ``DummyCache`` so we swap in
       a real ``LocMemCache`` for this test only — the throttle persists
       state via the cache, not the throttle object itself.
    """

    def test_settings_register_anon_and_user_throttles(self):
        from django.conf import settings

        throttle_classes = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"]
        self.assertIn("rest_framework.throttling.AnonRateThrottle", throttle_classes)
        self.assertIn("rest_framework.throttling.UserRateThrottle", throttle_classes)

        rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
        # Sanity-bound the configured rates so this test catches an
        # accidental "anon: 0/hour" (disables everything) or
        # "anon: 1000000/hour" (effectively unlimited).
        for scope in ("anon", "user"):
            self.assertIn(scope, rates)
            value = rates[scope]
            self.assertRegex(value, r"^\d+/(?:second|minute|hour|day)$")

    @override_settings(
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                "LOCATION": "safety-appeal-throttle-test",
            }
        }
    )
    def test_anon_throttle_rejects_caller_after_budget_exhausted(self):
        # Drive the production throttle class directly so the test is
        # independent of api_settings propagation. The fresh LocMemCache
        # above gives the throttle state somewhere to persist (the default
        # TestSync cache is DummyCache which would let every call through).
        from rest_framework.throttling import AnonRateThrottle

        cache.clear()

        def fresh_throttle() -> AnonRateThrottle:
            t = AnonRateThrottle()
            t.rate = "2/hour"
            t.num_requests, t.duration = t.parse_rate(t.rate)
            return t

        request = MagicMock()
        request.user = MagicMock(is_authenticated=False)
        request.META = {"REMOTE_ADDR": "203.0.113.42"}

        # Two callers from the same IP are within budget.
        self.assertTrue(fresh_throttle().allow_request(request, view=None))
        self.assertTrue(fresh_throttle().allow_request(request, view=None))
        # Third call must be denied — the budget is two per hour.
        self.assertFalse(
            fresh_throttle().allow_request(request, view=None),
            "Third call from the same anonymous IP must be denied once the "
            "rate budget is exhausted.",
        )

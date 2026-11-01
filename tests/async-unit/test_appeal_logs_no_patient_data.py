"""Appeal generation logs carry names, ids and counts, never patient data.

Like the chat logs, each test plants a sentinel where patient data travels and
checks that no log line written while it was in flight contains it.
"""

from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django import forms
from django.test import TestCase, override_settings
from loguru import logger

from fighthealthinsurance.common_view_logic import AppealsBackendHelper
from fighthealthinsurance.generate_appeal import AppealGenerator, AppealTemplateGenerator
from fighthealthinsurance.ml.ml_models import RemoteModelLike
from fighthealthinsurance.models import Denial, DenialTypes, ProposedAppeal

EMAIL = "sentinel-email@example.com"
KEY = "sentinel-case-key"
ANSWER = "sentinel-answer"
REASON = "sentinel-medical-reason"
DRAFT = "sentinel-stored-draft"
HISTORY = "sentinel-health-history"
DENIAL_TEXT = "sentinel-denial-text"
QA = "sentinel-question-answer"
QUESTION = "sentinel-question-text"


class _Logs:
    """Every log line written inside the block, at the level production writes."""

    def __enter__(self):
        self.lines = []
        # Formatted, so tracebacks count; the sink settings match the deployed ones.
        self.sink = logger.add(lambda m: self.lines.append(str(m)), level="DEBUG", backtrace=False, diagnose=False)
        return self

    def __exit__(self, *exc):
        logger.remove(self.sink)

    def assert_clean(self, *sentinels):
        text = "\n".join(self.lines)
        for sentinel in sentinels:
            assert sentinel not in text, sentinel


class _ReasonForm(forms.Form):
    """A denial type's questions, as far as the stream reads them."""

    medical_reason = forms.CharField(required=False)

    def preface(self):
        return []

    def main(self):
        return []

    def footer(self):
        return []

    def medical_context(self):
        raise ValueError(ANSWER)


class _Backend(RemoteModelLike):
    def __init__(self):
        self.seen = []

    async def _infer(self, *args, **kwargs):
        raise AssertionError("make_appeals uses the sync infer seam")

    def infer(self, prompt, patient_context, plan_context, infer_type, pubmed_context, ml_citations_context, prof_pov=False, **kwargs):
        self.seen.append(f"{prompt} {patient_context}")
        return [(infer_type, "Dear reviewer, please cover this care. Sincerely, the patient.")]


@override_settings(
    TEMPORAL_ENABLED=False,
    TEMPORAL_APPEAL_JOURNEY_ENABLED=False,
    TEMPORAL_INTAKE_JOURNEY_ENABLED=False,
)
class AppealStreamLogsTest(TestCase):
    DENIAL_ID = 4177

    @patch("fighthealthinsurance.common_view_logic.appealGenerator")
    def test_the_stream_logs_no_payload_answers_or_stored_drafts(self, generator):
        # gen_attempts=3 skips the research phase, as in test_appeal_replay_cap.
        denial = Denial.objects.create(
            denial_id=self.DENIAL_ID,
            semi_sekret=KEY,
            hashed_email=Denial.get_hashed_email(EMAIL),
            gen_attempts=3,
        )
        denial.denial_type.add(DenialTypes.objects.create(name="Logs test type"))
        # Two the same, as legacy rows with no fingerprint: one replays, the other takes the duplicate path.
        for _ in range(2):
            ProposedAppeal.objects.create(
                for_denial=denial,
                appeal_text=f"{DRAFT} is long enough to be a deliverable appeal rather than a runt row.",
                speculative=False,
            )
            ProposedAppeal.objects.filter(for_denial=denial).update(text_fingerprint=None)
        generator.make_appeals.return_value = iter([])

        async def run():
            got = []
            async for chunk in AppealsBackendHelper.generate_appeals(
                {
                    "denial_id": self.DENIAL_ID,
                    "email": EMAIL,
                    "semi_sekret": KEY,
                    "medical_reason": REASON,
                    "generated_answer": ANSWER,
                    # A chat draft's answers arrive keyed by the question's own text.
                    f"Has the patient tried {QUESTION}?": ANSWER,
                }
            ):
                if chunk and chunk.strip():
                    got.append(chunk)
            return got

        with patch.object(DenialTypes, "get_form", return_value=_ReasonForm), _Logs() as logs:
            chunks = async_to_sync(run)()
        # The stored draft really went through, so the check isn't vacuous.
        self.assertTrue(any(DRAFT in c for c in chunks))
        self.assertTrue(any("Parameters received: 6" in line for line in logs.lines))
        self.assertTrue(any("Skipping duplicate existing appeal" in line for line in logs.lines))
        self.assertTrue(any("Medical reasons so far: 1" in line for line in logs.lines))
        self.assertTrue(any("ValueError processing form" in line for line in logs.lines))
        logs.assert_clean(EMAIL, KEY, ANSWER, REASON, DRAFT, QUESTION)


class AppealGeneratorLogsTest(TestCase):
    def test_model_calls_log_names_not_prompts_or_context(self):
        denial = SimpleNamespace(
            denial_text=DENIAL_TEXT,
            procedure="Lumbar MRI",
            diagnosis="back pain",
            patient_user=None,
            primary_professional=None,
            qa_context=QA,
            health_history=HISTORY,
            professional_to_finish=False,
            plan_id=None,
            claim_id=None,
            insurance_company="Acme Health",
            insurance_company_obj=None,
            plan_context=None,
            plan_documents_summary=None,
            use_external=False,
            denial_id=42,
        )
        backend = _Backend()
        with patch(
            "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
            side_effect=lambda use_external=False: ["model-a"],
        ), patch(
            "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
            new={"model-a": [backend]},
        ), patch("fighthealthinsurance.generate_appeal.time.sleep"), patch(
            "fighthealthinsurance.generate_appeal.history_may_be_used_now", return_value=True
        ), _Logs() as logs:
            list(
                AppealGenerator().make_appeals(
                    denial,
                    AppealTemplateGenerator(prefaces=[f"{ANSWER}"], main=[f"{REASON}"], footer=["F"]),
                    medical_reasons=[],
                    non_ai_appeals=[],
                    prompt_mode="original",
                )
            )
        self.assertTrue(any("Calling models: ['model-a']" in line for line in logs.lines), logs.lines[:20])
        # The sentinels really went to the model, so the check below isn't vacuous.
        sent = "\n".join(backend.seen)
        self.assertIn(DENIAL_TEXT, sent)
        self.assertIn(HISTORY, sent)
        logs.assert_clean(DENIAL_TEXT, QA, HISTORY, ANSWER, REASON)

    def test_a_zip_lookup_failure_logs_the_error_type_only(self):
        source = Path("fighthealthinsurance/common_view_logic.py").read_text()
        self.assertIn('f"Zip code lookup failed: {type(e).__name__}"', source)

"""Appeal generation logs carry names, ids and counts, never patient data.

Like the chat logs, each test plants a sentinel where patient data travels and
checks that no log line written while it was in flight contains it.
"""

from types import SimpleNamespace
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings
from loguru import logger

from fighthealthinsurance.common_view_logic import AppealsBackendHelper
from fighthealthinsurance.generate_appeal import AppealGenerator, AppealTemplateGenerator
from fighthealthinsurance.ml.ml_models import RemoteModelLike
from fighthealthinsurance.models import Denial, ProposedAppeal

EMAIL = "sentinel-email@example.com"
KEY = "sentinel-case-key"
ANSWER = "sentinel-answer"
REASON = "sentinel-medical-reason"
DRAFT = "sentinel-stored-draft"
HISTORY = "sentinel-health-history"
DENIAL_TEXT = "sentinel-denial-text"
QA = "sentinel-question-answer"


class _Logs:
    """Every log line written inside the block, at any level."""

    def __enter__(self):
        self.lines = []
        self.sink = logger.add(lambda m: self.lines.append(m.record["message"]), level="TRACE")
        return self

    def __exit__(self, *exc):
        logger.remove(self.sink)

    def assert_clean(self, *sentinels):
        text = "\n".join(self.lines)
        for sentinel in sentinels:
            assert sentinel not in text, sentinel


class _Backend(RemoteModelLike):
    async def _infer(self, *args, **kwargs):
        raise AssertionError("make_appeals uses the sync infer seam")

    def infer(self, prompt, patient_context, plan_context, infer_type, pubmed_context, ml_citations_context, prof_pov=False, **kwargs):
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
        ProposedAppeal.objects.create(
            for_denial=denial,
            appeal_text=f"{DRAFT} is long enough to be a deliverable appeal rather than a runt row.",
            speculative=False,
        )
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
                }
            ):
                if chunk and chunk.strip():
                    got.append(chunk)
            return got

        with _Logs() as logs:
            chunks = async_to_sync(run)()
        # The stored draft really went through, so the check isn't vacuous.
        self.assertTrue(any(DRAFT in c for c in chunks))
        self.assertTrue(any("Parameter names received" in line for line in logs.lines))
        logs.assert_clean(EMAIL, KEY, ANSWER, REASON, DRAFT)


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
        ), patch("fighthealthinsurance.generate_appeal.time.sleep"), _Logs() as logs:
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
        logs.assert_clean(DENIAL_TEXT, QA, HISTORY, ANSWER, REASON)

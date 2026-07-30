"""make_appeals routes work to the right executor pool by run_kind.

Speculative precompute must submit to the background pool; live user-facing
generations use the interactive pool. The deadline passed by the caller must
reach parallel_infer so submitted calls can exit cooperatively.
"""

from unittest.mock import MagicMock, patch

from fighthealthinsurance import exec as fhi_exec
from fighthealthinsurance.generate_appeal import (
    AppealGenerator,
    AppealTemplateGenerator,
)
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike


class _RecordingBackend(RemoteFullOpenLike):
    """Records the submit_executor/deadline parallel_infer receives."""

    def __init__(self):
        super().__init__("http://record.test/v1", "tok", "record-model")
        self.seen_kwargs: list[dict] = []

    def parallel_infer(self, **kwargs):
        self.seen_kwargs.append(kwargs)
        return []


def _mock_denial():
    denial = MagicMock()
    denial.denial_id = 777
    denial.use_external = False
    for attr in (
        "qa_context",
        "health_history",
        "plan_context",
        "plan_documents_summary",
        "claim_id",
    ):
        setattr(denial, attr, None)
    denial.professional_to_finish = False
    denial.diagnosis = "dx"
    denial.procedure = "px"
    denial.denial_text = "denial"
    denial.insurance_company = "ins"
    return denial


def _drive(run_kind, backend, deadline=None):
    gen = AppealGenerator()
    tmpl = AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"])
    with patch(
        "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
        side_effect=lambda use_external=False: ["record-model-name"],
    ), patch(
        "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
        new={"record-model-name": [backend]},
    ), patch(
        "fighthealthinsurance.generate_appeal.time.sleep"
    ):
        try:
            list(
                gen.make_appeals(
                    _mock_denial(),
                    tmpl,
                    medical_reasons=[],
                    non_ai_appeals=[],
                    pubmed_context=None,
                    ml_citations_context=None,
                    plan_context=None,
                    run_kind=run_kind,
                    deadline=deadline,
                )
            )
        except Exception:
            pass


class TestPoolRouting:
    def test_live_runs_use_the_interactive_pool(self):
        backend = _RecordingBackend()
        _drive("live", backend)
        assert backend.seen_kwargs
        assert all(
            k["submit_executor"] is fhi_exec.executor for k in backend.seen_kwargs
        )

    def test_speculative_runs_use_the_background_pool(self):
        backend = _RecordingBackend()
        _drive("speculative", backend)
        assert backend.seen_kwargs
        assert all(
            k["submit_executor"] is fhi_exec.background_executor
            for k in backend.seen_kwargs
        )

    def test_deadline_reaches_parallel_infer(self):
        backend = _RecordingBackend()
        _drive("live", backend, deadline=99999.0)
        assert backend.seen_kwargs
        assert all(k["deadline"] == 99999.0 for k in backend.seen_kwargs)

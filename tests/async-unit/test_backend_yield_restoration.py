"""Empty backend results must FAIL, not silently succeed.

Two zero-appeal causes fixed here:
- ``parallel_infer`` returning ``[]`` used to count as a successful backend
  (``if result is not None``), ending the model's attempt with zero futures
  and no "all backends failed" signal.
- Backends without a parallel-inference implementation used to fall back to
  submitting the BASE-CLASS stub ``infer`` (which always returns None) to the
  executor -- guaranteed-empty futures that looked like a working submission.
"""

import io
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from loguru import logger as loguru_logger

from fighthealthinsurance.generate_appeal import (
    AppealGenerator,
    AppealTemplateGenerator,
)
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike, RemoteModelLike


class _EmptyParallelBackend(RemoteFullOpenLike):
    """A full backend whose parallel_infer yields no futures."""

    def __init__(self):
        super().__init__("http://empty.test/v1", "tok", "empty-model")
        self.parallel_calls = 0

    def parallel_infer(self, **kwargs):
        self.parallel_calls += 1
        return []


def _mock_denial():
    denial = MagicMock()
    denial.denial_id = 4242
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


@contextmanager
def _loguru_capture(level="WARNING"):
    sink = io.StringIO()
    handler_id = loguru_logger.add(sink, level=level)
    try:
        yield sink
    finally:
        loguru_logger.remove(handler_id)


def _drain_make_appeals(models_by_name):
    gen = AppealGenerator()
    tmpl = AppealTemplateGenerator(prefaces=["P"], main=["M"], footer=["F"])
    denial = _mock_denial()
    with patch(
        "fighthealthinsurance.generate_appeal.ml_router.generate_text_backend_names",
        side_effect=lambda use_external=False: list(models_by_name.keys()),
    ), patch(
        "fighthealthinsurance.generate_appeal.ml_router.models_by_name",
        new=models_by_name,
    ), patch(
        "fighthealthinsurance.generate_appeal.time.sleep"
    ):
        try:
            list(
                gen.make_appeals(
                    denial,
                    tmpl,
                    medical_reasons=[],
                    non_ai_appeals=[],
                    pubmed_context=None,
                    ml_citations_context=None,
                    plan_context=None,
                )
            )
        except Exception:
            pass


class TestEmptyFuturesAreFailures:
    def test_empty_parallel_infer_lands_in_all_backends_failed(self):
        backend = _EmptyParallelBackend()
        with _loguru_capture() as sink:
            _drain_make_appeals({"empty-model-name": [backend]})
        assert backend.parallel_calls > 0
        log = sink.getvalue()
        assert (
            "returned no futures" in log or "backend(s)" in log
        ), f"empty futures did not register as a backend failure; log: {log}"

    def test_non_parallel_backend_is_an_explicit_error(self):
        stub = MagicMock(spec=RemoteModelLike)
        stub.__str__ = lambda self: "StubBackend"  # type: ignore[assignment]
        with _loguru_capture(level="ERROR") as sink:
            _drain_make_appeals({"stub-model-name": [stub]})
        assert "no parallel inference implementation" in sink.getvalue()
        # The old fallback submitted stub.infer to the executor; that must be
        # gone.
        stub.infer.assert_not_called()

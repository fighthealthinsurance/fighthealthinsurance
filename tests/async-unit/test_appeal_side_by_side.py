"""Our alpha model (fhi-local) and Nemotron write side by side for people
who allow outside models (ml_router.appeal_pass_names).

Opted in, with FHI_APPEAL_SIDE_BY_SIDE on (the default), the primary pass is
fhi-local and Nemotron, each when it can be asked; the other internal models
and the next outside ones are the backup. Either missing leaves the other
alone. Opted out, or with the setting off, nothing changes. Only the full
letter goes out: the one-line helper calls and the context-shed retries stay
with our models. Past the day's budget the letter is written as before.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import override_settings

from fighthealthinsurance.generate_appeal import (
    AppealGenerator,
    AppealTemplateGenerator,
)
from fighthealthinsurance.ml import ml_router as router_module
from fighthealthinsurance.ml.ml_models import AlphaRemoteInternal, RemoteFullOpenLike
from fighthealthinsurance.ml.ml_router import (
    SIDE_BY_SIDE_OUTSIDE,
    appeal_pass_names,
)

NEMOTRON = SIDE_BY_SIDE_OUTSIDE
GPT = "gpt-5.5"
DEEPSEEK = "deepseek-ai/DeepSeek-V4-Pro-0813"


def _alpha(name="fhi-local"):
    model = AlphaRemoteInternal.__new__(AlphaRemoteInternal)
    model.name = name
    return model


def _model(name, external):
    return SimpleNamespace(name=name, external=external)


class FakeRouter:
    """Just what appeal_pass_names reads: the internal models by cost, the
    models by name, which can be asked, and the old name lists."""

    def __init__(self, down=(), local_name="fhi-local"):
        self.legacy = _model("fhi-legacy", False)
        self.local = _alpha(local_name)
        self.nemotron = _model(NEMOTRON, True)
        self.gpt = _model(GPT, True)
        self.deepseek = _model(DEEPSEEK, True)
        self.down = set(down)
        self.internal_models_by_cost = [self.legacy, self.local]
        self.models_by_name = {
            "fhi-legacy": [self.legacy],
            local_name: [self.local],
            NEMOTRON: [self.nemotron],
            GPT: [self.gpt],
            DEEPSEEK: [self.deepseek],
        }

    def _selectable(self, model):
        return model.name not in self.down

    def generate_text_backend_names(self, use_external=False, fail_open=True):
        internal = [
            m.name for m in self.internal_models_by_cost if m.name not in self.down
        ]
        if not use_external:
            return internal
        outside = [
            m.name
            for m in (self.gpt, self.nemotron, self.deepseek)
            if m.name not in self.down
        ]
        return internal + outside


class TestTheTwoWriteSideBySide:
    def test_both_up(self):
        primary, backup, hosted = appeal_pass_names(FakeRouter(), True)
        assert primary == ["fhi-local", NEMOTRON]
        # The older model and the next outside ones, in that order, only if
        # neither primary answers. Nemotron never repeats in the backup.
        assert backup == ["fhi-legacy", GPT, DEEPSEEK]
        assert hosted is True

    def test_fhi_local_is_found_by_its_backend_whatever_its_name(self):
        primary, _, _ = appeal_pass_names(FakeRouter(local_name="gemma-26b-q4"), True)
        assert primary == ["gemma-26b-q4", NEMOTRON]

    def test_nemotron_out_leaves_fhi_local_and_the_next_outside_model_backs_up(self):
        primary, backup, _ = appeal_pass_names(FakeRouter(down={NEMOTRON}), True)
        assert primary == ["fhi-local"]
        assert backup == ["fhi-legacy", GPT, DEEPSEEK]

    def test_fhi_local_down_leaves_nemotron_alone(self):
        primary, backup, _ = appeal_pass_names(FakeRouter(down={"fhi-local"}), True)
        assert primary == [NEMOTRON]
        assert backup == ["fhi-legacy", GPT, DEEPSEEK]

    def test_both_out_writes_as_before(self):
        router = FakeRouter(down={"fhi-local", NEMOTRON})
        assert appeal_pass_names(router, True) == appeal_pass_names(
            router, True, side_by_side=False
        )
        primary, backup, _ = appeal_pass_names(router, True)
        assert primary == ["fhi-legacy"]
        assert backup == [GPT, DEEPSEEK]


class TestNothingChangesOptedOutOrSwitchedOff:
    def test_opted_out_is_exactly_as_before_and_never_outside(self):
        router = FakeRouter()
        assert appeal_pass_names(router, False) == appeal_pass_names(
            router, False, side_by_side=False
        )
        primary, backup, _ = appeal_pass_names(router, False)
        assert primary == ["fhi-legacy", "fhi-local"]
        assert backup == []
        for name in primary + backup:
            assert not router_module.is_external_name(router, name)

    def test_switched_off_opted_in_is_exactly_as_before(self):
        router = FakeRouter()
        with override_settings(FHI_APPEAL_SIDE_BY_SIDE=False):
            off = appeal_pass_names(router, True)
        assert off == appeal_pass_names(router, True, side_by_side=False)
        assert off[0] == ["fhi-legacy", "fhi-local"]
        assert NEMOTRON in off[1]

    def test_switched_on_is_the_default(self):
        with override_settings():
            from django.conf import settings

            if hasattr(settings, "FHI_APPEAL_SIDE_BY_SIDE"):
                del settings.FHI_APPEAL_SIDE_BY_SIDE
            assert router_module.side_by_side_enabled() is True


# ---- make_appeals ---------------------------------------------------------


class _RecordingBackend(RemoteFullOpenLike):
    def __init__(self, name, external):
        super().__init__("http://record.test/v1", "tok", name)
        self._external = external
        self.calls: list[dict] = []

    @property
    def external(self):
        return self._external

    def parallel_infer(self, **kwargs):
        self.calls.append(kwargs)
        return []


def _denial(use_external):
    denial = MagicMock()
    denial.denial_id = 4242
    denial.use_external = use_external
    for attr in (
        "qa_context",
        "health_history",
        "plan_context",
        "plan_documents_summary",
        "claim_id",
    ):
        setattr(denial, attr, None)
    denial.professional_to_finish = False
    # Longer than three characters, or no one-line helper question is asked.
    denial.diagnosis = "back pain"
    denial.procedure = "Lumbar MRI"
    denial.denial_text = "The plan denied the MRI as not medically necessary."
    denial.insurance_company = "ins"
    return denial


def _drive(use_external, budget_left=True, side_by_side=True, local_down=False):
    local = _RecordingBackend("fhi-local", False)
    nemotron = _RecordingBackend(NEMOTRON, True)
    legacy = _RecordingBackend("fhi-legacy", False)
    by_name = {"fhi-local": [local], NEMOTRON: [nemotron], "fhi-legacy": [legacy]}

    def names(router, flag, side_by_side=None):
        if side_by_side is None:
            side_by_side = router_module.side_by_side_enabled()
        if flag and side_by_side and local_down:
            return [NEMOTRON], ["fhi-legacy"], True
        if flag and side_by_side:
            return ["fhi-local", NEMOTRON], ["fhi-legacy"], True
        if flag:
            return ["fhi-legacy", "fhi-local"], [NEMOTRON], True
        return ["fhi-legacy", "fhi-local"], [], False

    gen = AppealGenerator()
    # An empty template has no static letter, so the one-line helper calls
    # run too.
    tmpl = AppealTemplateGenerator([], [], [])
    with override_settings(FHI_APPEAL_SIDE_BY_SIDE=side_by_side), patch(
        "fighthealthinsurance.generate_appeal.appeal_pass_names", side_effect=names
    ), patch(
        "fighthealthinsurance.generate_appeal.ml_router.models_by_name", new=by_name
    ), patch(
        "fighthealthinsurance.generate_appeal.spend.side_by_side_budget_left",
        return_value=budget_left,
    ) as budget, patch(
        "fighthealthinsurance.generate_appeal.time.sleep"
    ):
        try:
            list(
                gen.make_appeals(
                    _denial(use_external),
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
    return local, nemotron, legacy, budget


def _kinds(backend):
    return [call.get("infer_type") for call in backend.calls]


class TestMakeAppeals:
    def test_opted_out_sends_nothing_outside(self):
        local, nemotron, legacy, budget = _drive(use_external=False)
        assert nemotron.calls == []
        assert local.calls and legacy.calls
        budget.assert_not_called()

    def test_opted_in_both_write_and_only_the_letter_goes_out(self):
        local, nemotron, legacy, _ = _drive(use_external=True)
        assert set(_kinds(nemotron)) == {"full"}
        assert "full" in _kinds(local)
        # The helper one-liners are asked, of our models only.
        assert "medically_necessary" in _kinds(local)
        assert "medically_necessary" not in _kinds(nemotron)
        # The older model is backup only, and the primary gave nothing here,
        # so it ran as the backup, never in the first pass beside the two.
        assert set(_kinds(legacy)) <= {"full"}

    def test_with_ours_down_the_helpers_go_to_the_next_internal_model(self):
        local, nemotron, legacy, _ = _drive(use_external=True, local_down=True)
        assert local.calls == []
        assert set(_kinds(nemotron)) == {"full"}
        assert "medically_necessary" in _kinds(legacy)

    def test_nemotron_gets_the_same_letter_request_as_ours(self):
        local, nemotron, _, _ = _drive(use_external=True)
        ours = next(c for c in local.calls if c.get("infer_type") == "full")
        theirs = nemotron.calls[0]
        skip = {"model_name", "prompt_version", "model"}
        for key in ("prompt", "patient_context", "plan_context", "pubmed_context"):
            if key in ours or key in theirs:
                assert ours.get(key) == theirs.get(key), key
        assert set(ours) - skip == set(theirs) - skip

    def test_the_shed_retries_stay_with_our_models(self):
        _, nemotron, _, _ = _drive(use_external=True)
        # Every model answers nothing here, so the shed ladder runs; it never
        # asks the outside model again.
        assert len(nemotron.calls) == 1

    def test_past_the_days_budget_the_letter_is_written_as_before(self):
        local, nemotron, legacy, budget = _drive(use_external=True, budget_left=False)
        budget.assert_called_once()
        assert "full" in _kinds(legacy)
        # Nemotron is back in the backup, as before; only reached when ours
        # give nothing, which they do here.
        assert set(_kinds(nemotron)) <= {"full"}

    def test_switched_off_never_checks_the_budget(self):
        _, _, _, budget = _drive(use_external=True, side_by_side=False)
        budget.assert_not_called()

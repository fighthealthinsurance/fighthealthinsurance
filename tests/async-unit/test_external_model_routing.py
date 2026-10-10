"""Routing and the appeal path once outside models stop answering.

DeepInfra, Anthropic, Azure and Perplexity models can be retired, run out
of credit or lose their key. The router must route around them (a provider
paused for credit gives its slot to the next model; chat answers on our own
models without an ERROR per turn; citations skip a Perplexity that is down;
a call by name tries healthy instances first; the chooser's fixed fan-out
never takes an exploration draw), and the appeal path must record a
provider it could not ask as "unavailable" instead of retrying it.
"""

import os
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from django.test import override_settings

from fighthealthinsurance.ml import ml_models, ml_router, spend
from fighthealthinsurance.ml.ml_models import (
    ProviderUnavailable,
    RemoteFullOpenLike,
    RemoteModelLike,
    RemotePerplexity,
    attempt_deadline,
    describe_model_error,
)
from fighthealthinsurance.ml.ml_router import MLRouter

ROSTER = ["mistral", "glm", "deepseek", "qwen", "kimi"]


def _outside(name, quality=80, available=True):
    """An outside model as the router sees it (the roster tests' shape).
    health_checked_live, so is_available() and the spend gate decide, never
    the health sweep."""
    model = MagicMock(spec=RemoteModelLike)
    model.external = True
    model.quality.return_value = quality
    model.is_available.return_value = available
    model.health_checked_live = True
    model.SPEND_PROVIDER = None
    model.name = name
    return model


def _ours(available=True, quality=120):
    """One of our own models that can take a chat turn."""
    model = MagicMock(spec=RemoteModelLike)
    model.external = False
    model.quality.return_value = quality
    model.supports_general_instructions.return_value = True
    model.is_available.return_value = available
    model.health_checked_live = True
    model.name = "ours"
    return model


def _names(models):
    return [m.name for m in models]


def _sonar():
    with patch.dict(os.environ, {"PERPLEXITY_API": "test-key"}):
        return RemotePerplexity("sonar")


def _failing_open_errors(cap):
    return [m for m in cap.messages("ERROR") if "failing open" in m]


class _WithRouter:
    """A fresh router per test. (The spend ledger, where pauses live, is
    reset around every test by tests/conftest.py.)"""

    def setup_method(self):
        self.router = MLRouter()
        # The fail-open ERROR goes out once per pool every ten minutes per
        # process; start unlogged, so a test finding none proves something.
        self._fail_open_logged = patch.dict(ml_router._fail_open_logged_at, clear=True)
        self._fail_open_logged.start()

    def teardown_method(self):
        self._fail_open_logged.stop()


class TestSpendGatesSelection(_WithRouter):
    def test_a_model_whose_provider_may_not_be_asked_is_not_selectable(self):
        model = _outside("glm")
        model._spend_allows = MagicMock(return_value=False)
        assert self.router._selectable(model) is False

    def test_the_next_external_takes_the_place_of_one_out_of_budget(self):
        best = _outside("best", quality=90)
        best._spend_allows = MagicMock(return_value=False)
        self.router.external_models_by_cost = [
            best,
            _outside("second", quality=80),
            _outside("third", quality=70),
            _outside("fourth", quality=60),
        ]
        assert _names(self.router.best_external_models()) == [
            "second",
            "third",
            "fourth",
        ]

    def test_a_provider_paused_for_credit_is_not_selectable(self):
        with patch.dict(os.environ, {"DEEPINFRA_API": "test-key"}):
            model = ml_models.DeepInfra(model="zai-org/GLM-5.3-Flash")
        # DeepInfra has no live signal, so the sweep cache is read: say up.
        with patch(
            "fighthealthinsurance.ml.health_status.health_status.model_ok",
            return_value=True,
        ):
            assert self.router._selectable(model), "selectable before the pause"
            spend.pause(spend.DEEPINFRA, reason="test")
            assert not self.router._selectable(model)


class TestChatRosterAllDown(_WithRouter):
    """Every outside chat model is retired or out of credit: chat is ours
    alone, which is expected, not an outage to fail open over."""

    def setup_method(self):
        super().setup_method()
        self.router.chat_outside_models_by_name.update(
            {name: _outside(name, available=False) for name in ROSTER}
        )
        self.router.internal_models_by_cost = [_ours()]
        # The warning is throttled per process; start each test unwarned.
        self._warned = patch.object(ml_router, "_roster_down_warned_at", float("-inf"))
        self._warned.start()

    def teardown_method(self):
        self._warned.stop()
        super().teardown_method()

    def _ask(self):
        with override_settings(FHI_CHAT_OUTSIDE_MODELS=ROSTER):
            return self.router.chat_outside_models()

    @staticmethod
    def _roster_warnings(cap):
        return [m for m in cap.messages("WARNING") if "every outside chat" in m]

    def test_ours_answer_alone(self):
        assert self._ask() == []

    def test_no_failing_open_error_is_logged(self, log_capture):
        with log_capture() as cap:
            self._ask()
        assert _failing_open_errors(cap) == []

    def test_the_warning_goes_out_once_within_the_hour(self, log_capture):
        with log_capture() as cap:
            self._ask()
            self._ask()
        assert len(self._roster_warnings(cap)) == 1

    def test_the_warning_goes_out_again_after_an_hour(self, log_capture):
        with log_capture() as cap:
            self._ask()
            # As if the hour had passed since the first warning.
            ml_router._roster_down_warned_at -= ml_router._ROSTER_DOWN_WARN_SECONDS + 1
            self._ask()
        assert len(self._roster_warnings(cap)) == 2

    def test_the_roster_fails_open_when_none_of_ours_can_answer(self):
        self.router.internal_models_by_cost = [_ours(available=False)]
        assert _names(self._ask()) == ROSTER[:3]

    def test_a_down_side_by_side_model_does_not_warn_for_the_roster(
        self, log_capture
    ):
        """One model being down says nothing about the rest of the roster,
        and must not spend the hourly warning a whole-roster outage needs."""
        with log_capture() as cap:
            with override_settings(FHI_CHAT_SIDE_BY_SIDE_MODEL=ROSTER[0]):
                assert self.router.chat_side_by_side_model() is None
        assert self._roster_warnings(cap) == []
        with log_capture() as cap:
            self._ask()
        assert len(self._roster_warnings(cap)) == 1

    def _side_by_side(self):
        with override_settings(FHI_CHAT_SIDE_BY_SIDE_MODEL=ROSTER[0]):
            return self.router.chat_side_by_side_model()

    def test_a_down_side_by_side_model_is_none_when_none_of_ours_can_answer(self):
        """The roster's fail-open (no one else to ask) is not for one
        optional comparison model."""
        self.router.internal_models_by_cost = [_ours(available=False)]
        assert self._side_by_side() is None

    def test_a_down_side_by_side_model_logs_no_failing_open_error(self, log_capture):
        self.router.internal_models_by_cost = [_ours(available=False)]
        with log_capture() as cap:
            self._side_by_side()
        assert _failing_open_errors(cap) == []

    def test_a_healthy_side_by_side_model_is_returned(self):
        side_by_side = self.router.chat_outside_models_by_name[ROSTER[0]]
        side_by_side.is_available.return_value = True
        assert self._side_by_side() is side_by_side


class TestCitationBackends(_WithRouter):
    """Citations never fail open onto a Perplexity that cannot answer: the
    helpers fall back to the supplemental sources instead."""

    def setup_method(self):
        super().setup_method()
        self.sonar = _sonar()
        self.router.models_by_name["sonar"] = [self.sonar]

    def _backends(self):
        return (
            self.router.full_find_citation_backends(use_external=True),
            self.router.partial_find_citation_backends(),
        )

    def test_perplexity_is_used_while_it_is_fine(self):
        assert self._backends() == ([self.sonar], [self.sonar])

    def test_no_backend_while_perplexity_does_not_serve_the_model(self):
        self.sonar._note_missing_model(
            self.sonar.api_base, self.sonar.model, "HTTP 410"
        )
        assert self._backends() == ([], [])

    def test_no_backend_while_perplexity_is_paused_for_credit(self):
        spend.pause(spend.PERPLEXITY, reason="test")
        assert self._backends() == ([], [])


class TestBackendsForName:
    def setup_method(self):
        self.router = MLRouter()
        self.registered = [
            _outside("down-a", available=False),
            _outside("up-b"),
            _outside("down-c", available=False),
            _outside("up-d"),
        ]
        self.router.models_by_name["gemma"] = list(self.registered)

    def test_healthy_instances_come_first_and_none_are_dropped(self):
        assert _names(self.router.backends_for_name("gemma")) == [
            "up-b",
            "up-d",
            "down-a",
            "down-c",
        ]

    def test_the_registry_keeps_its_cost_order(self):
        self.router.backends_for_name("gemma")
        assert self.router.models_by_name["gemma"] == self.registered

    def test_an_unknown_name_has_no_backends(self):
        assert self.router.backends_for_name("not-registered") == []


class TestChatExploration(_WithRouter):
    """The chooser compares every model chat may ask, and needs the same
    answer each time it asks: explore=False lists the whole roster and
    never draws."""

    def setup_method(self):
        super().setup_method()
        self.router.chat_outside_models_by_name.update(
            {name: _outside(name) for name in ROSTER}
        )

    def _externals(self, **kwargs):
        with override_settings(
            FHI_CHAT_OUTSIDE_MODELS=ROSTER, FHI_CHAT_EXPLORE_RATE=1.0
        ):
            models = self.router.get_chat_backends(use_external=True, **kwargs)
        return [m.name for m in models if m.external]

    def test_without_exploration_no_draw_is_taken(self):
        with patch.object(
            ml_router, "_explore_draw", side_effect=AssertionError("drew")
        ):
            self._externals(explore=False)

    def test_without_exploration_every_roster_model_is_listed(self):
        """Models past the turn's three outside slots answer chat when the
        draw picks them, so the chooser must compare them too."""
        assert self._externals(explore=False) == ROSTER

    def test_a_chat_turn_still_explores(self):
        # Guards the test above: with these settings a turn does draw.
        with patch.object(ml_router, "_explore_draw", return_value=0.0):
            assert self._externals() == ["mistral", "qwen", "deepseek"]


class TestBestInternalModel(_WithRouter):
    """The denial-type hint goes to the strongest internal model that can
    answer: a parked or marked-down one gives its place to the next."""

    def setup_method(self):
        super().setup_method()
        self.strong = _ours(quality=210)
        self.weak = _ours(quality=200)
        self.router.internal_models_by_cost = [self.weak, self.strong]

    def test_a_down_strongest_model_gives_its_place_to_the_next(self):
        self.strong.is_available.return_value = False
        assert self.router.best_internal_model(general_only=False) is self.weak

    def test_a_strongest_model_the_sweep_marked_down_gives_its_place(self):
        for model in (self.strong, self.weak):
            model.health_checked_live = False
        with patch(
            "fighthealthinsurance.ml.health_status.health_status.model_ok",
            side_effect=lambda m: m is not self.strong,
        ):
            assert self.router.best_internal_model(general_only=False) is self.weak

    def test_the_strongest_is_returned_when_none_can_answer(self):
        self.strong.is_available.return_value = False
        self.weak.is_available.return_value = False
        assert self.router.best_internal_model(general_only=False) is self.strong

    def test_no_failing_open_error_is_logged_when_none_can_answer(self, log_capture):
        self.strong.is_available.return_value = False
        self.weak.is_available.return_value = False
        with log_capture() as cap:
            self.router.best_internal_model(general_only=False)
        assert _failing_open_errors(cap) == []

    def test_a_down_general_model_still_beats_a_healthy_appeal_only_one(self):
        """For instruction-following callers the appeal-only fine-tune's
        digit soup is no answer either, so the general filter comes first."""
        self.strong.supports_general_instructions.return_value = False
        self.weak.is_available.return_value = False
        assert self.router.best_internal_model() is self.weak


class TestAppealNames(_WithRouter):
    """generate_text_backend_names: our models marked down fail open only
    when nothing else can answer the appeal."""

    def setup_method(self):
        super().setup_method()
        self.ours = _ours(available=False)
        self.outside = _outside("outside")
        self.router.internal_models_by_cost = [self.ours]
        self.router.external_models_by_cost = [self.outside]
        self.router.models_by_name = {
            "fhi-ours": [self.ours],
            "outside": [self.outside],
        }

    def test_without_fail_open_down_internals_are_left_out(self):
        assert (
            self.router.generate_text_backend_names(use_external=False, fail_open=False)
            == []
        )

    def test_without_fail_open_internals_that_can_answer_are_kept(self):
        self.ours.is_available.return_value = True
        assert self.router.generate_text_backend_names(
            use_external=False, fail_open=False
        ) == ["fhi-ours"]

    def test_without_fail_open_no_failing_open_error_is_logged(self, log_capture):
        with log_capture() as cap:
            self.router.generate_text_backend_names(use_external=False, fail_open=False)
        assert _failing_open_errors(cap) == []

    def test_internal_only_names_still_fail_open(self):
        assert self.router.generate_text_backend_names(use_external=False) == [
            "fhi-ours"
        ]

    def test_opt_in_names_leave_out_down_internals_while_an_external_can_answer(
        self,
    ):
        assert self.router.generate_text_backend_names(use_external=True) == ["outside"]

    def test_opt_in_names_fail_open_when_no_external_can_answer(self):
        self.outside.is_available.return_value = False
        assert self.router.generate_text_backend_names(use_external=True) == [
            "fhi-ours"
        ]

    def test_a_forced_model_is_listed_whatever_its_health(self):
        with patch.dict(os.environ, {"FORCE_MODEL": "fhi-ours"}):
            names = self.router.generate_text_backend_names(
                use_external=False, fail_open=False
            )
        assert names == ["fhi-ours"]


class TestChatBesideAHealthyOutsideModel(_WithRouter):
    """A chat turn with a healthy outside model in it doesn't fail open on
    our own models marked down: they would only hold the turn open."""

    def setup_method(self):
        super().setup_method()
        self.lead = _ours(available=False, quality=210)
        self.other = _ours(available=False, quality=200)
        self.router.internal_models_by_cost = [self.other, self.lead]
        self.router.models_by_name.update(
            {"fhi-lead": [self.lead], "fhi-other": [self.other]}
        )
        self.roster = [_outside(name) for name in ROSTER]
        self.router.chat_outside_models_by_name.update({m.name: m for m in self.roster})

    def _chat(self, use_external=True):
        with override_settings(
            FHI_CHAT_OUTSIDE_MODELS=ROSTER, FHI_CHAT_EXPLORE_RATE=0.0
        ):
            return self.router.get_chat_backends(use_external=use_external)

    def test_our_down_models_are_left_out(self):
        assert self._chat() == self.roster[:3]

    def test_no_failing_open_error_is_logged(self, log_capture):
        with log_capture() as cap:
            self._chat()
        assert _failing_open_errors(cap) == []

    def test_ours_fail_open_when_no_outside_model_can_answer(self):
        """Then the roster fails open too (chat_outside_models), and those
        long shots must not leave our own out."""
        for model in self.roster:
            model.is_available.return_value = False
        assert self._chat() == [
            self.lead,
            self.lead,
            *self.roster[:3],
            self.other,
        ]

    def test_ours_fail_open_without_outside_models(self):
        assert self._chat(use_external=False) == [self.lead, self.lead, self.other]


class TestSummariesAndQuestionsBesideAnOutsideModel(_WithRouter):
    """Summaries and appeal questions leave our models marked down out
    while an outside model in the same list can answer."""

    def setup_method(self):
        super().setup_method()
        self.ours = _ours(available=False)
        self.router.internal_models_by_cost = [self.ours]
        self.generalist = _outside("generalist")
        self.sonar = _outside("sonar")

    def _with_generalist(self):
        self.router.models_by_name[ml_router._EXTERNAL_GENERALIST] = [self.generalist]

    def test_summaries_log_no_failing_open_error_beside_the_generalist(
        self, log_capture
    ):
        self._with_generalist()
        with log_capture() as cap:
            self.router.summarize_backends(use_external=True)
        assert _failing_open_errors(cap) == []

    def test_questions_log_no_failing_open_error_beside_the_generalist(
        self, log_capture
    ):
        self._with_generalist()
        with log_capture() as cap:
            self.router.full_qa_backends(use_external=True)
        assert _failing_open_errors(cap) == []

    def test_questions_leave_down_internals_out_beside_perplexity_alone(self):
        self.router.models_by_name["sonar"] = [self.sonar]
        assert self.router.full_qa_backends(use_external=True) == [self.sonar]


_CHECKED_INFER_KWARGS = dict(
    prompt="denial text",
    patient_context=None,
    plan_context=None,
    infer_type="medically_necessary",
    pubmed_context=None,
    system_prompt="sys",
    temperature=0.5,
)


def _unavailable_model(filed: bool = True):
    """An appeal backend whose every call raises ProviderUnavailable, as the
    transport does when asked to raise. ``filed``: the pair is flagged as
    not serving the model (gone, as opposed to a passing failure)."""
    m = RemoteFullOpenLike("http://fake-backend.example/v1", "tok", "routing-gone")
    m._infer_no_context = AsyncMock(  # type: ignore[method-assign]
        side_effect=ProviderUnavailable("model not served")
    )
    if filed:
        m._note_missing_model(m.api_base, m.model, "HTTP 410")
    return m


class TestAppealPathUnavailable:
    @pytest.mark.asyncio
    async def test_unavailable_propagates_without_a_retry(self):
        m = _unavailable_model()
        with pytest.raises(ProviderUnavailable):
            await m._checked_infer(**_CHECKED_INFER_KWARGS)
        assert m._infer_no_context.await_count == 1

    @pytest.mark.asyncio
    async def test_a_passing_failure_still_gets_the_retry(self):
        """A 5xx or a timeout leaves the model available: the call is retried,
        as before, rather than filed as unavailable."""
        m = _unavailable_model(filed=False)
        m._infer_no_context.side_effect = [ProviderUnavailable("HTTP 503"), None]
        with patch.object(ml_models, "record_ml_result") as recorded:
            assert await m._checked_infer(**_CHECKED_INFER_KWARGS) == []
        assert m._infer_no_context.await_count == 2
        recorded.assert_called_once_with(ANY, "medically_necessary", "no_completion")

    @pytest.mark.asyncio
    async def test_the_first_call_asks_the_transport_to_raise(self):
        # Without it the transport answers None, which reads as a model that
        # answered nothing and spends the retry on a provider that is gone.
        m = _unavailable_model()
        with pytest.raises(ProviderUnavailable):
            await m._checked_infer(**_CHECKED_INFER_KWARGS)
        assert m._infer_no_context.await_args.kwargs["raise_on_unavailable"] is True

    @pytest.mark.asyncio
    async def test_the_result_is_recorded_as_unavailable(self):
        m = _unavailable_model()
        with patch.object(ml_models, "record_ml_result") as recorded:
            with pytest.raises(ProviderUnavailable):
                await m._checked_infer(**_CHECKED_INFER_KWARGS)
        recorded.assert_called_once_with(ANY, "medically_necessary", "unavailable")

    @pytest.mark.asyncio
    async def test_prior_auth_moves_on_when_the_provider_is_unavailable(self):
        m = _unavailable_model()
        assert await m.generate_prior_auth_response("Request prior auth.") is None

    def test_the_error_detail_names_an_unavailable_provider(self):
        assert (
            describe_model_error(ProviderUnavailable("not served here"))
            == "unavailable: not served here"
        )

    @pytest.mark.asyncio
    async def test_a_spent_attempt_budget_skips_without_asking(self):
        m = RemoteFullOpenLike("http://fake-backend.example/v1", "tok", "routing-late")
        m._infer_no_context = AsyncMock(  # type: ignore[method-assign]
            return_value="unused"
        )
        with attempt_deadline(0):
            outcome = await m._checked_infer_outcome(
                "denial text",
                None,
                None,
                "medically_necessary",
                None,
                "sys",
                0.5,
                None,
                False,
                False,
                None,
            )
        assert outcome == ("skipped_deadline", [])
        m._infer_no_context.assert_not_called()

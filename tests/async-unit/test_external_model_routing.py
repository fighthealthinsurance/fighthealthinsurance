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


def _ours(available=True):
    """One of our own models that can take a chat turn."""
    model = MagicMock(spec=RemoteModelLike)
    model.external = False
    model.quality.return_value = 120
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


class _WithRouter:
    """A fresh router per test. (The spend ledger, where pauses live, is
    reset around every test by tests/conftest.py.)"""

    def setup_method(self):
        self.router = MLRouter()

    def teardown_method(self):
        pass


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
        assert [m for m in cap.messages("ERROR") if "failing open" in m] == []

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

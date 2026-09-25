"""The staff page's routing overview reads the router's real selectors.

These build a bare router with mock backends, so each test controls exactly
what is registered and healthy, and check that the overview reports what
the selectors return, merged across the two settings of use_external.
"""

import os
import unittest
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

from fighthealthinsurance.ml import routing_overview as ro
from fighthealthinsurance.ml.ml_models import (
    DeepInfra,
    RemoteAnthropic,
    RemoteAzureClaude,
    RemoteHealthInsurance,
    RemoteModelLike,
    RemotePerplexity,
)
from fighthealthinsurance.ml.ml_router import MLRouter

GEMMA = "google/gemma-4-26B-A4B-it"


def _bare_router() -> MLRouter:
    """An MLRouter without running registration (no network, no env)."""
    router = MLRouter.__new__(MLRouter)
    router.models_by_name = {}
    router.internal_models_by_cost = []
    router.all_models_by_cost = []
    router.external_models_by_cost = []
    router.context_only_models_by_cost = []
    return router


def _backend(
    name: str,
    quality: int,
    *,
    external: bool = False,
    general: bool = True,
    available: bool = True,
    context_only: bool = False,
    tier: Optional[str] = None,
) -> MagicMock:
    """A mock backend carrying every signal the selectors read.

    health_checked_live is True so the health sweep is never consulted and
    availability is exactly ``available``.
    """
    model = MagicMock(spec=RemoteModelLike, name=name)
    model.external = external
    model.context_only = context_only
    model.quality.return_value = quality
    model.supports_general_instructions.return_value = general
    model.is_available.return_value = available
    model.health_checked_live = True
    if tier is not None:
        model.get_tier = MagicMock(return_value=tier)
    return model


def _register(router: MLRouter, name: str, model: MagicMock) -> None:
    """Put a mock where MLRouter.__init__ would. Call in cost order."""
    router.models_by_name.setdefault(name, []).append(model)
    if model.context_only:
        router.context_only_models_by_cost.append(model)
        return
    router.all_models_by_cost.append(model)
    if model.external:
        router.external_models_by_cost.append(model)
    else:
        router.internal_models_by_cost.append(model)


def _labels(overview: ro.RoutingOverview, name: str) -> list:
    return [role.label for role in overview.roles.get(name, [])]


def _plan(overview: ro.RoutingOverview, title: str) -> ro.PathPlan:
    for plan in overview.paths:
        if plan.title == title:
            return plan
    raise AssertionError(f"no path called {title}")


class _NoRoutingEnv(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ("FORCE_MODEL", "ENABLED_REMOTE_MODELS"):
            os.environ.pop(name, None)


class TestRoles(_NoRoutingEnv):
    def _production_like(self) -> MLRouter:
        router = _bare_router()
        _register(router, "fhi-legacy", _backend("fhi-legacy", 101, general=False))
        _register(router, "fhi-local", _backend("fhi-local", 210))
        _register(router, GEMMA, _backend(GEMMA, 80, external=True))
        _register(
            router,
            "azure-openai/gpt-5.5",
            _backend("gpt", 100, external=True, tier="frontier"),
        )
        _register(
            router, "sonar", _backend("sonar", 100, external=True, context_only=True)
        )
        return router

    def test_roles_follow_the_selectors(self):
        overview = ro.build_routing_overview(self._production_like())
        self.assertEqual(
            _labels(overview, "fhi-local"),
            [
                "Appeals: primary",
                "Appeals: backup",
                "Appeals: best-internal hint",
                "Chat: doubled lead",
                "Questions: fan-out",
                "Summaries: 1st",
            ],
        )
        self.assertEqual(
            _labels(overview, "fhi-legacy"), ["Appeals: primary", "Appeals: backup"]
        )
        # With a healthy internal, the hosted generalist only backs up
        # summaries, and only when external models are allowed.
        self.assertEqual(
            _labels(overview, GEMMA),
            [
                "Appeals: backup (external allowed)",
                "Chat: fan-out (external allowed)",
                "Summaries: 2nd (external allowed)",
            ],
        )
        self.assertEqual(
            _labels(overview, "sonar"), ["Questions: fan-out (external allowed)"]
        )
        self.assertEqual(
            [name for name, _q in overview.top_external],
            ["azure-openai/gpt-5.5", GEMMA],
        )
        self.assertEqual(overview.top_external_rank("azure-openai/gpt-5.5"), 1)
        self.assertIsNone(overview.top_external_rank("sonar"))

    def test_path_lists_keep_the_selectors_order(self):
        overview = ro.build_routing_overview(self._production_like())
        primary = _plan(overview, "Appeals, primary pass")
        self.assertEqual(
            [e.name for e in primary.internal_only], ["fhi-legacy", "fhi-local"]
        )
        # The primary pass is internal only whatever the person chose.
        self.assertEqual(primary.external_allowed, primary.internal_only)
        backup = _plan(overview, "Appeals, backup pass")
        self.assertEqual(
            [e.name for e in backup.external_allowed],
            ["fhi-legacy", "fhi-local", "azure-openai/gpt-5.5", GEMMA],
        )
        chat = _plan(overview, "Chat")
        self.assertEqual(
            chat.internal_only, [ro.PlanEntry("fhi-local", "doubled lead")]
        )
        summaries = _plan(overview, "Summaries")
        self.assertEqual(
            [e.name for e in summaries.external_allowed], ["fhi-local", GEMMA]
        )

    def test_a_marked_down_internal_moves_behind_the_generalist(self):
        router = _bare_router()
        _register(router, "fhi-local", _backend("fhi-local", 210, available=False))
        _register(router, GEMMA, _backend(GEMMA, 80, external=True))
        overview = ro.build_routing_overview(router)
        # First when only internals may answer (the fail-open fallback), second
        # behind the generalist when external models are allowed.
        self.assertIn("Summaries: 1st (internal only)", _labels(overview, "fhi-local"))
        self.assertIn(
            "Summaries: 2nd (external allowed)", _labels(overview, "fhi-local")
        )
        self.assertIn("Summaries: 1st (external allowed)", _labels(overview, GEMMA))

    def test_retry_only_externals_are_labelled(self):
        router = self._production_like()
        extra = _backend("extra", 90, external=True)
        with patch.object(
            MLRouter,
            "get_chat_backends_with_fallback",
            side_effect=lambda use_external: (
                ([], [extra]) if use_external else ([], [])
            ),
        ):
            _register(router, "extra-model", extra)
            overview = ro.build_routing_overview(router)
        self.assertIn(
            "Chat: retry only (external allowed)", _labels(overview, "extra-model")
        )
        self.assertEqual(
            _plan(overview, "Chat").external_allowed,
            [ro.PlanEntry("extra-model", "retry only")],
        )

    def test_force_model_and_allow_list_are_reported(self):
        os.environ["FORCE_MODEL"] = "fhi-local"
        os.environ["ENABLED_REMOTE_MODELS"] = "b-model, a-model"
        overview = ro.build_routing_overview(self._production_like())
        self.assertEqual(overview.force_model, "fhi-local")
        self.assertTrue(overview.force_model_registered)
        self.assertEqual(overview.enabled_remote_models, ["a-model", "b-model"])
        # The forced model is the whole chat list, so nothing is doubled.
        self.assertIn("Chat: fan-out", _labels(overview, "fhi-local"))
        self.assertNotIn("Chat: doubled lead", _labels(overview, "fhi-local"))

    def test_unset_overrides_are_none(self):
        overview = ro.build_routing_overview(self._production_like())
        self.assertIsNone(overview.force_model)
        self.assertFalse(overview.force_model_registered)
        self.assertIsNone(overview.enabled_remote_models)

    def test_building_calls_no_model(self):
        router = self._production_like()
        with (
            patch.object(
                MLRouter, "summarize", new=AsyncMock(side_effect=AssertionError)
            ) as summarize,
            patch(
                "fighthealthinsurance.ml.health_status.health_status.get_snapshot",
                side_effect=AssertionError,
            ) as get_snapshot,
        ):
            ro.build_routing_overview(router)
        self.assertFalse(summarize.called)
        self.assertFalse(get_snapshot.called)
        for models in router.models_by_name.values():
            for m in models:
                m._infer_no_context.assert_not_called()
                m._infer.assert_not_called()
                m.model_is_ok.assert_not_called()


class TestOrdinal(unittest.TestCase):
    def test_ordinals(self):
        self.assertEqual(
            [ro._ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22, 23, 101)],
            [
                "1st",
                "2nd",
                "3rd",
                "4th",
                "11th",
                "12th",
                "13th",
                "21st",
                "22nd",
                "23rd",
                "101st",
            ],
        )


class TestRowTraits(unittest.TestCase):
    """Rows the router didn't register are read off a stand-in that never
    runs the constructor, which would need the missing configuration."""

    def _shadow_traits(self, backend_cls, internal_name):
        with patch.object(
            backend_cls, "__init__", side_effect=AssertionError("constructed")
        ):
            return ro.row_traits(None, backend_cls, internal_name)

    def test_deepinfra_quality_comes_from_the_model(self):
        traits = self._shadow_traits(DeepInfra, GEMMA)
        self.assertEqual(
            (traits.quality, traits.tier, traits.kind, traits.routed),
            (80, "", ro.KIND_EXTERNAL, False),
        )
        self.assertEqual(
            self._shadow_traits(DeepInfra, "deepseek-ai/DeepSeek-V4-Pro").quality, 92
        )

    def test_tiered_providers_report_their_tier(self):
        fable = self._shadow_traits(RemoteAzureClaude, "claude-fable-5")
        self.assertEqual((fable.quality, fable.tier), (100, "frontier"))
        haiku = self._shadow_traits(RemoteAnthropic, "claude-haiku-4-5-20251001")
        self.assertEqual((haiku.quality, haiku.tier), (80, "speed"))

    def test_kinds(self):
        self.assertEqual(
            self._shadow_traits(RemoteHealthInsurance, "any").kind,
            ro.KIND_APPEAL_ONLY,
        )
        self.assertEqual(
            self._shadow_traits(RemotePerplexity, "sonar").kind, ro.KIND_CONTEXT_ONLY
        )

    def test_registered_instance_is_read_directly(self):
        model = _backend("fhi-local", 210)
        traits = ro.row_traits(model, None, "ignored")
        self.assertEqual(
            (traits.quality, traits.kind, traits.routed),
            (210, ro.KIND_INTERNAL, True),
        )

    def test_nothing_to_read(self):
        self.assertIsNone(ro.row_traits(None, None, "x"))

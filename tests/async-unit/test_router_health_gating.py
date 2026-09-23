"""Router health gating for INTERNAL backends + fail-open + determinism.

The hourly health sweep result used to gate only external model selection;
internal vLLM backends the sweep had already marked down kept getting fanned
out to, burning their full timeout on every request between sweeps. Now every
selection point filters through the sweep cache -- but FAILS OPEN (with an
ERROR log) if that would empty a non-empty pool, so a stale cache can never
zero out generation.
"""

import io
from typing import Optional
from unittest.mock import patch

from loguru import logger as loguru_logger

from fighthealthinsurance.chooser_tasks import _select_candidate_models
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike
from fighthealthinsurance.ml.ml_router import MLRouter


def _bare_router() -> MLRouter:
    """An MLRouter without running registration (no network, no env)."""
    router = MLRouter.__new__(MLRouter)
    router.models_by_name = {}
    router.internal_models_by_cost = []
    router.all_models_by_cost = []
    router.external_models_by_cost = []
    router.context_only_models_by_cost = []
    return router


def _internal_model(name: str, quality: Optional[int] = None) -> RemoteFullOpenLike:
    m = RemoteFullOpenLike(f"http://{name}.internal/v1", "tok", name)
    m.name = name
    if quality is not None:
        # RemoteFullOpenLike reports 100; the chat lead is picked by quality.
        m.quality = lambda: quality
    return m


def _health_map(mapping):
    """Patch health_status.model_ok with a name-keyed lookup."""

    def model_ok(model):
        return mapping.get(getattr(model, "model", None))

    return patch(
        "fighthealthinsurance.ml.health_status.health_status.model_ok",
        side_effect=model_ok,
    )


class TestInternalHealthGating:
    def test_swept_down_internal_model_is_excluded(self):
        router = _bare_router()
        up = _internal_model("up-model")
        down = _internal_model("down-model")
        router.internal_models_by_cost = [down, up]

        with _health_map({"up-model": True, "down-model": False}):
            models = router.generate_text_backends(use_external=False)

        assert up in models
        assert down not in models

    def test_unswept_models_fail_open(self):
        router = _bare_router()
        unknown = _internal_model("never-swept")
        router.internal_models_by_cost = [unknown]

        with _health_map({}):
            models = router.generate_text_backends(use_external=False)

        assert unknown in models

    def test_generate_text_backend_names_gates_internal_models(self):
        """The names list is what make_appeals fans every appeal out to; it
        used to skip the sweep gate its instance-returning sibling applied,
        so a marked-down backend was called on every run."""
        router = _bare_router()
        up = _internal_model("up-model")
        down = _internal_model("down-model")
        router.internal_models_by_cost = [down, up]
        router.models_by_name = {"up-model": [up], "down-model": [down]}

        with _health_map({"up-model": True, "down-model": False}):
            names = router.generate_text_backend_names(use_external=False)
            names_with_external = router.generate_text_backend_names(
                use_external=True
            )

        assert names == ["up-model"]
        assert "down-model" not in names_with_external

    def test_generate_text_backend_names_fails_open_when_all_are_down(self):
        router = _bare_router()
        a = _internal_model("a-model")
        b = _internal_model("b-model")
        router.internal_models_by_cost = [a, b]
        router.models_by_name = {"a-model": [a], "b-model": [b]}

        with _health_map({"a-model": False, "b-model": False}):
            names = router.generate_text_backend_names(use_external=False)

        assert names == ["a-model", "b-model"]

    def test_all_down_fails_open_with_error_log(self):
        router = _bare_router()
        a = _internal_model("a-model")
        b = _internal_model("b-model")
        router.internal_models_by_cost = [a, b]

        sink = io.StringIO()
        handler = loguru_logger.add(sink, level="ERROR")
        try:
            with _health_map({"a-model": False, "b-model": False}):
                models = router.generate_text_backends(use_external=False)
        finally:
            loguru_logger.remove(handler)

        # Fail open: the full pool comes back rather than zero models...
        assert models == [a, b]
        # ...and the condition is loudly visible.
        assert "failing open" in sink.getvalue()

    def test_chat_backends_gate_internal_models(self):
        router = _bare_router()
        up = _internal_model("up-model")
        down = _internal_model("down-model")
        router.internal_models_by_cost = [down, up]
        router.models_by_name = {}

        with _health_map({"up-model": True, "down-model": False}):
            models = router.get_chat_backends(use_external=False)

        assert up in models
        assert down not in models

    def test_filter_before_slice_lets_healthy_models_step_up(self):
        """With 7 internal models where the first 6 are down, the healthy
        seventh must be selected (filter-then-slice, not slice-then-filter)."""
        router = _bare_router()
        down_models = [_internal_model(f"down-{i}") for i in range(6)]
        healthy = _internal_model("healthy-last")
        router.internal_models_by_cost = down_models + [healthy]

        mapping = {f"down-{i}": False for i in range(6)}
        mapping["healthy-last"] = True
        with _health_map(mapping):
            models = router.generate_text_backends(use_external=False)

        assert healthy in models
        assert all(d not in models for d in down_models)

    def test_summarize_chat_history_pool_is_gated(self):
        router = _bare_router()
        up = _internal_model("up-model")
        down = _internal_model("down-model")
        router.internal_models_by_cost = [down, up]

        with _health_map({"up-model": True, "down-model": False}):
            pool = router._filter_available(
                router.internal_models_by_cost, "summarize-chat-history"
            )[:2]

        assert pool == [up]


class TestChatFhiDeterminism:
    def test_equal_quality_fhi_backends_double_the_one_whose_name_sorts_first(
        self,
    ):
        """models_by_name insertion order varies with registration order;
        chat must double the SAME fhi backend on every pod, so a quality tie
        goes to the name that sorts first."""
        router = _bare_router()
        z_first = _internal_model("z-instance")
        a_first = _internal_model("a-instance")
        # Insertion order deliberately puts fhi-zeta before fhi-alpha.
        router.models_by_name = {
            "fhi-zeta": [z_first],
            "fhi-alpha": [a_first],
        }

        with _health_map({}):
            models = router.get_chat_backends(use_external=False)

        # Both report quality 100 and fhi-alpha sorts first, so its instance
        # is the doubled one.
        assert models[:2] == [a_first, a_first]

    def test_external_selectable_alias_still_works(self):
        router = _bare_router()
        m = _internal_model("alias-model")
        with _health_map({"alias-model": False}):
            assert router._external_selectable(m) is False
        with _health_map({"alias-model": True}):
            assert router._external_selectable(m) is True


def _two_fhi_router(alpha_quality: int, zeta_quality: int):
    """fhi-alpha (sorts first) and fhi-zeta, registered in cost order the way
    MLRouter.__init__ would, so they appear both by name and in the internal
    pool the chat fan-out draws its other slots from."""
    router = _bare_router()
    alpha = _internal_model("alpha-instance", alpha_quality)
    zeta = _internal_model("zeta-instance", zeta_quality)
    router.models_by_name = {"fhi-alpha": [alpha], "fhi-zeta": [zeta]}
    router.internal_models_by_cost = [alpha, zeta]
    router.all_models_by_cost = [alpha, zeta]
    return router, alpha, zeta


class TestChatLeadByQuality:
    """The chat lead is the strongest healthy fhi backend, not the first
    name. With the default model paths that makes alpha (210) lead instead
    of the May fine-tune (200), whose name sorts first."""

    def test_a_higher_quality_lead_beats_name_order(self):
        router, alpha, zeta = _two_fhi_router(alpha_quality=200, zeta_quality=210)

        with _health_map({}):
            models = router.get_chat_backends(use_external=False)

        assert models == [zeta, zeta, alpha]

    def test_the_lead_is_listed_exactly_twice(self):
        """Twice up front and not again among the internals, and it doesn't
        take one of the six internal slots from another backend."""
        router = _bare_router()
        lead = _internal_model("lead-instance", 210)
        others = [_internal_model(f"other-{i}", 100 + i) for i in range(7)]
        router.models_by_name = {"fhi-lead": [lead]}
        router.internal_models_by_cost = others + [lead]
        router.all_models_by_cost = others + [lead]

        with _health_map({}):
            models = router.get_chat_backends(use_external=False)

        assert models.count(lead) == 2
        assert models[:2] == [lead, lead]
        # The six strongest of the others follow, strongest first.
        assert models[2:] == list(reversed(others))[:6]

    def test_a_down_lead_is_passed_over(self):
        router, alpha, zeta = _two_fhi_router(alpha_quality=200, zeta_quality=210)

        with _health_map({"alpha-instance": True, "zeta-instance": False}):
            models = router.get_chat_backends(use_external=False)

        # The healthy one leads, and the down one is left out altogether.
        assert models == [alpha, alpha]

    def test_all_down_fails_open_to_the_strongest(self):
        router, alpha, zeta = _two_fhi_router(alpha_quality=200, zeta_quality=210)

        sink = io.StringIO()
        handler = loguru_logger.add(sink, level="ERROR")
        try:
            with _health_map({"alpha-instance": False, "zeta-instance": False}):
                models = router.get_chat_backends(use_external=False)
        finally:
            loguru_logger.remove(handler)

        # A stale health cache must not zero out the fhi slots: the strongest
        # still leads and the rest still fan out, and the fallback is logged.
        assert models == [zeta, zeta, alpha]
        assert "failing open" in sink.getvalue()


class TestChatLeadSharingAName:
    """Two backends can register under one name, e.g. alpha and the May
    fine-tune set to the same model path. The lead is the stronger INSTANCE,
    not every backend under the winning name."""

    @staticmethod
    def _shared_name_router():
        router = _bare_router()
        # Cost order, the way MLRouter.__init__ keeps each name's backends:
        # the May fine-tune (cost 2) before alpha (cost 3).
        may = _internal_model("may-instance", 200)
        alpha = _internal_model("alpha-instance", 210)
        for m in (may, alpha):
            # The friendly name MLRouter stamps on every instance.
            m.name = "fhi-local"
        router.models_by_name = {"fhi-local": [may, alpha]}
        router.internal_models_by_cost = [may, alpha]
        router.all_models_by_cost = [may, alpha]
        return router, may, alpha

    def test_only_the_stronger_backend_leads(self):
        router, may, alpha = self._shared_name_router()

        with _health_map({"may-instance": True, "alpha-instance": True}):
            models = router.get_chat_backends(use_external=False)

        # alpha twice, and the May fine-tune in an ordinary internal slot.
        assert models == [alpha, alpha, may]

    def test_list_heads_pick_the_stronger_backend(self):
        """Denied-item analysis asks models[0], and the chooser keeps the
        first backend it sees under each name, so both get alpha."""
        router, may, alpha = self._shared_name_router()

        with _health_map({"may-instance": True, "alpha-instance": True}):
            models = router.get_chat_backends(use_external=False)

        assert models[0] is alpha
        assert _select_candidate_models(models, 4) == [alpha]

    def test_equal_quality_backends_under_the_lead_name_both_lead(self):
        """Two servers of the same model under one name share the doubled
        slot, cheapest first. A weaker backend under that name still takes
        an ordinary internal slot."""
        router = _bare_router()
        first = _internal_model("first-server", 210)
        second = _internal_model("second-server", 210)
        weaker = _internal_model("weaker-server", 200)
        router.models_by_name = {"fhi-local": [first, weaker, second]}
        router.internal_models_by_cost = [first, weaker, second]
        router.all_models_by_cost = [first, weaker, second]

        with _health_map({}):
            models = router.get_chat_backends(use_external=False)

        assert models == [first, second, first, second, weaker]

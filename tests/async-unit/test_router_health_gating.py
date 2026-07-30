"""Router health gating for INTERNAL backends + fail-open + determinism.

The hourly health sweep result used to gate only external model selection;
internal vLLM backends the sweep had already marked down kept getting fanned
out to, burning their full timeout on every request between sweeps. Now every
selection point filters through the sweep cache -- but FAILS OPEN (with an
ERROR log) if that would empty a non-empty pool, so a stale cache can never
zero out generation.
"""

import io
from unittest.mock import patch

from loguru import logger as loguru_logger

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


def _internal_model(name: str) -> RemoteFullOpenLike:
    m = RemoteFullOpenLike(f"http://{name}.internal/v1", "tok", name)
    m.name = name
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
    def test_chat_doubles_the_alphabetically_first_fhi_backend(self):
        """models_by_name insertion order varies with registration order;
        chat must double the SAME fhi backend on every pod."""
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

        # fhi-alpha sorts first, so its instance is the doubled one.
        assert models[:2] == [a_first, a_first]

    def test_external_selectable_alias_still_works(self):
        router = _bare_router()
        m = _internal_model("alias-model")
        with _health_map({"alias-model": False}):
            assert router._external_selectable(m) is False
        with _health_map({"alias-model": True}):
            assert router._external_selectable(m) is True

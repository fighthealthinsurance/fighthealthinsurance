"""MLRouter.chat_outside_models: chat's outside models come from a named
roster (FHI_CHAT_OUTSIDE_MODELS), in its order, skipping any model that is
down or whose provider's chat budget is spent. With no roster, chat keeps
the best externals as before. The chat-only models never join the general
pools that appeals draw from."""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from fighthealthinsurance.ml import ml_models, spend
from fighthealthinsurance.ml.ml_models import RemoteModelLike
from fighthealthinsurance.ml.ml_router import MLRouter


def _outside(name, provider=None, available=True):
    model = MagicMock(spec=RemoteModelLike)
    model.external = True
    model.quality.return_value = 80
    model.is_available.return_value = available
    model.health_checked_live = True
    model.SPEND_PROVIDER = provider
    model.name = name
    return model


ROSTER = ["azure-openai/gpt-5.5", "mistral", "glm", "deepseek", "qwen"]


class ChatOutsideRosterTest(SimpleTestCase):
    def setUp(self):
        spend._ledger.reset_for_tests()
        self.router = MLRouter()
        self.gpt = _outside("azure-openai/gpt-5.5", spend.AZURE)
        self.router.models_by_name["azure-openai/gpt-5.5"] = [self.gpt]
        self.chat = {
            name: _outside(name, spend.DEEPINFRA)
            for name in ("mistral", "glm", "deepseek", "qwen")
        }
        self.router.chat_outside_models_by_name.update(self.chat)

    def names(self, models):
        return [m.name for m in models]

    def test_the_roster_order_is_kept_and_three_are_asked(self):
        with override_settings(FHI_CHAT_OUTSIDE_MODELS=ROSTER):
            got = self.router.chat_outside_models()
        self.assertEqual(self.names(got), ["azure-openai/gpt-5.5", "mistral", "glm"])

    def test_a_model_that_is_down_is_skipped(self):
        self.chat["mistral"].is_available.return_value = False
        with override_settings(FHI_CHAT_OUTSIDE_MODELS=ROSTER):
            got = self.router.chat_outside_models()
        self.assertEqual(self.names(got), ["azure-openai/gpt-5.5", "glm", "deepseek"])

    def test_a_spent_deepinfra_budget_leaves_only_the_others(self):
        spend.pause(spend.DEEPINFRA, spend.CHAT)
        with override_settings(FHI_CHAT_OUTSIDE_MODELS=ROSTER):
            got = self.router.chat_outside_models()
        self.assertEqual(self.names(got), ["azure-openai/gpt-5.5"])

    def test_a_spent_budget_never_fails_open(self):
        spend.pause(spend.DEEPINFRA, spend.CHAT)
        spend.pause(spend.AZURE, spend.CHAT)
        with override_settings(FHI_CHAT_OUTSIDE_MODELS=ROSTER):
            self.assertEqual(self.router.chat_outside_models(), [])

    def test_the_chat_fan_out_uses_the_roster(self):
        with override_settings(FHI_CHAT_OUTSIDE_MODELS=ROSTER):
            primary, _fallback = self.router.get_chat_backends_with_fallback(
                use_external=True
            )
        externals = [m for m in primary if getattr(m, "external", False)]
        self.assertEqual(
            self.names(externals), ["azure-openai/gpt-5.5", "mistral", "glm"]
        )

    def test_without_a_roster_chat_keeps_the_best_externals(self):
        best = [_outside("best")]
        with override_settings(FHI_CHAT_OUTSIDE_MODELS=[]), patch.object(
            self.router, "best_external_models", return_value=best
        ):
            primary, _ = self.router.get_chat_backends_with_fallback(use_external=True)
        self.assertIn(best[0], primary)


class ChatOnlyModelsTest(SimpleTestCase):
    def test_deepinfra_chat_models_stay_out_of_the_general_pools(self):
        with patch.object(ml_models, "get_env_variable", return_value="test-key"):
            router = MLRouter()
        names = set(router.chat_outside_models_by_name)
        self.assertEqual(names, set(ml_models.DeepInfra.CHAT_MODELS))
        general = {getattr(m, "model", None) for m in router.all_models_by_cost} | set(
            router.models_by_name
        )
        for name in ml_models.DeepInfra.CHAT_MODELS:
            self.assertNotIn(name, general)

    def test_qwen_thinks_off_and_every_chat_model_is_length_capped(self):
        with patch.object(ml_models, "get_env_variable", return_value="test-key"):
            for name in ml_models.DeepInfra.CHAT_MODELS:
                extras = ml_models.DeepInfra(model=name)._request_extras(name)
                self.assertIn("max_tokens", extras, name)
            qwen = ml_models.DeepInfra(model="Qwen/Qwen3.8-2.4T-A95B")
            self.assertEqual(
                qwen._request_extras("Qwen/Qwen3.8-2.4T-A95B")["chat_template_kwargs"],
                {"enable_thinking": False},
            )


class ProviderSpendTest(SimpleTestCase):
    """Answers from DeepInfra and Azure are counted as they arrive, against
    chat only inside a chat reply."""

    def setUp(self):
        spend._ledger.reset_for_tests()

    def _deepinfra(self):
        with patch.object(ml_models, "get_env_variable", return_value="test-key"):
            return ml_models.DeepInfra(model="zai-org/GLM-5.3-Flash")

    def test_a_chat_answer_counts_against_chat(self):
        from fighthealthinsurance.ml.ml_metrics import ml_call_purpose

        model = self._deepinfra()
        with ml_call_purpose("chat"):
            model._record_spend(
                "zai-org/GLM-5.3-Flash", {"usage": {"estimated_cost": 0.001}}
            )
        view = spend._ledger.snapshot()
        today = spend._today()
        self.assertEqual(view.day_total("deepinfra:chat", today), 1000)
        self.assertEqual(view.day_total("deepinfra:other", today), 0)

    def test_an_appeal_answer_is_not_chat_spend(self):
        from fighthealthinsurance.ml.ml_metrics import ml_call_purpose

        model = self._deepinfra()
        with ml_call_purpose("appeal"):
            model._record_spend(
                "zai-org/GLM-5.3-Flash", {"usage": {"estimated_cost": 0.001}}
            )
        self.assertEqual(
            spend._ledger.snapshot().day_total("deepinfra:chat", spend._today()), 0
        )

    def test_an_error_body_is_not_counted(self):
        model = self._deepinfra()
        model._record_spend("x", {"object": "error", "usage": {"estimated_cost": 1}})
        self.assertEqual(spend._ledger.snapshot().by_day, {})

    def test_a_quota_refusal_pauses_the_provider_for_that_use(self):
        from fighthealthinsurance.ml.ml_metrics import ml_call_purpose

        model = self._deepinfra()
        with ml_call_purpose("chat"):
            model._note_spend_refusal(402, "")
        self.assertFalse(spend.allows(spend.DEEPINFRA, spend.CHAT))
        self.assertTrue(spend.allows(spend.DEEPINFRA, spend.OTHER))


class SendGuardTest(SimpleTestCase):
    """The budget is checked at the send: a model chosen before its budget
    ran out (or before a refusal paused its provider) is not asked."""

    def setUp(self):
        spend._ledger.reset_for_tests()

    def test_a_paused_provider_is_not_sent_anything(self):
        import asyncio

        from fighthealthinsurance.ml.ml_metrics import ml_call_purpose

        with patch.object(ml_models, "get_env_variable", return_value="test-key"):
            model = ml_models.DeepInfra(model="zai-org/GLM-5.3-Flash")
        spend.pause(spend.DEEPINFRA, spend.CHAT)
        failures = []

        def no_session(*args, **kwargs):
            raise AssertionError("a request was built")

        async def ask():
            with ml_call_purpose("chat"):
                return await model._RemoteOpenLike__infer(
                    "system",
                    "hello",
                    None,
                    None,
                    0.7,
                    "zai-org/GLM-5.3-Flash",
                    transport_failures=failures,
                )

        with patch.object(ml_models.aiohttp, "ClientSession", no_session):
            result = asyncio.run(ask())
        self.assertIsNone(result)
        self.assertEqual(len(failures), 1)
        self.assertIn("budget spent", failures[0])

    def test_the_same_model_outside_chat_is_still_asked(self):
        with patch.object(ml_models, "get_env_variable", return_value="test-key"):
            model = ml_models.DeepInfra(model="zai-org/GLM-5.3-Flash")
        spend.pause(spend.DEEPINFRA, spend.CHAT)
        self.assertTrue(model._spend_allows())


class AllowListTest(SimpleTestCase):
    def test_chat_models_honour_the_remote_model_allow_list(self):
        env = {
            "DEEPINFRA_API": "test-key",
            "ENABLED_REMOTE_MODELS": "zai-org/GLM-5.3-Flash",
        }
        with patch.object(ml_models, "get_env_variable", side_effect=env.get), patch(
            "fighthealthinsurance.ml.ml_router.get_env_variable", side_effect=env.get
        ):
            router = MLRouter()
        self.assertEqual(
            set(router.chat_outside_models_by_name), {"zai-org/GLM-5.3-Flash"}
        )

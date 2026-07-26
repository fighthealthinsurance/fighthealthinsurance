"""Tests for the staff direct-model-query page (AdminModelQueryView) and the
``model_query`` helpers that address and call a single backend."""

import asyncio
from unittest import mock

import aiohttp
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from fighthealthinsurance.ml import model_query

User = get_user_model()

_ROUTER = "fighthealthinsurance.ml.ml_router.ml_router"


class FakeModel:
    """Minimal stand-in for a RemoteModelLike backend."""

    external = True
    context_only = False
    dual_mode = False
    backup_api_base = None
    backup_model = None
    api_base = "https://example.invalid/v1"

    def __init__(self, name="fhi-test", model="vendor/test-model", response="hi there"):
        self.name = name
        self.model = model
        self._response = response
        self.calls = []

    def __str__(self):
        return self.name

    async def _infer_no_context(self, system_prompts, prompt, **kwargs):
        self.calls.append(
            {"system_prompts": system_prompts, "prompt": prompt, **kwargs}
        )
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _fake_router(models, context_only=None):
    router = mock.MagicMock()
    router.all_models_by_cost = models
    router.context_only_models_by_cost = context_only or []
    return router


class ModelRefTest(TestCase):
    def test_ref_is_derived_from_configuration_not_object_identity(self):
        """A ref built on one pod must resolve on another, so it must not
        depend on the in-process object address."""
        first = FakeModel()
        second = FakeModel()
        self.assertNotEqual(id(first), id(second))
        with mock.patch(_ROUTER, _fake_router([first])):
            ref_a = model_query.enumerate_queryable_models()[0][0]
        with mock.patch(_ROUTER, _fake_router([second])):
            ref_b = model_query.enumerate_queryable_models()[0][0]
        self.assertEqual(ref_a, ref_b)

    def test_identically_configured_replicas_get_distinct_refs(self):
        replicas = [FakeModel(), FakeModel()]
        with mock.patch(_ROUTER, _fake_router(replicas)):
            refs = [ref for ref, _ in model_query.enumerate_queryable_models()]
        self.assertEqual(len(set(refs)), 2)

    def test_context_only_models_are_queryable(self):
        generation = FakeModel(name="fhi-gen")
        citations = FakeModel(name="perplexity")
        citations.context_only = True
        with mock.patch(_ROUTER, _fake_router([generation], [citations])):
            found = model_query.enumerate_queryable_models()
        self.assertEqual([str(m) for _, m in found], ["fhi-gen", "perplexity"])

    def test_context_only_models_do_not_shift_generation_refs(self):
        """Refs must stay valid whether or not the context-only pool loads,
        so the context-only models are enumerated last."""
        generation = FakeModel(name="fhi-gen")
        citations = FakeModel(name="perplexity")
        with mock.patch(_ROUTER, _fake_router([generation])):
            without = model_query.enumerate_queryable_models()[0][0]
        with mock.patch(_ROUTER, _fake_router([generation], [citations])):
            with_context = model_query.enumerate_queryable_models()[0][0]
        self.assertEqual(without, with_context)

    def test_find_model_by_ref_round_trips(self):
        wanted = FakeModel(name="wanted", model="vendor/wanted")
        other = FakeModel(name="other", model="vendor/other")
        with mock.patch(_ROUTER, _fake_router([other, wanted])):
            ref = dict(
                (str(m), r) for r, m in model_query.enumerate_queryable_models()
            )["wanted"]
            self.assertIs(model_query.find_model_by_ref(ref), wanted)

    def test_find_model_by_unknown_ref_returns_none(self):
        with mock.patch(_ROUTER, _fake_router([FakeModel()])):
            self.assertIsNone(model_query.find_model_by_ref("Nope|nope|nope|0"))

    def test_unreachable_context_only_pool_still_lists_generation_models(self):
        router = mock.MagicMock()
        router.all_models_by_cost = [FakeModel()]
        type(router).context_only_models_by_cost = mock.PropertyMock(
            side_effect=RuntimeError("pool not ready")
        )
        with mock.patch(_ROUTER, router):
            self.assertEqual(len(model_query.enumerate_queryable_models()), 1)


class QueryModelTest(TestCase):
    def test_successful_query_captures_response_and_elapsed(self):
        model = FakeModel(response="the appeal letter")
        result = model_query.query_model(model, "hello?")
        self.assertTrue(result["ok"])
        self.assertEqual(result["response"], "the appeal letter")
        self.assertIsNone(result["error"])
        self.assertGreaterEqual(result["elapsed"], 0)

    def test_query_passes_prompt_and_surfaces_http_errors(self):
        """raise_http_errors must be set so a 401 reads as an auth failure
        rather than being flattened into "empty response"."""
        model = FakeModel(
            response=aiohttp.ClientResponseError(
                request_info=mock.Mock(), history=(), status=401, message="Unauthorized"
            )
        )
        result = model_query.query_model(model, "hello?")
        self.assertFalse(result["ok"])
        self.assertIn("401", result["error"])
        self.assertTrue(model.calls[0]["raise_http_errors"])
        self.assertEqual(model.calls[0]["prompt"], "hello?")

    def test_empty_system_prompt_falls_back_to_default(self):
        """``_infer`` loops over system prompts, so an empty list would never
        reach the backend."""
        model = FakeModel()
        model_query.query_model(model, "hello?", system_prompt="   ")
        self.assertEqual(
            model.calls[0]["system_prompts"], [model_query.DEFAULT_SYSTEM_PROMPT]
        )

    def test_custom_system_prompt_and_temperature_are_forwarded(self):
        model = FakeModel()
        model_query.query_model(
            model, "hello?", system_prompt="Be terse.", temperature=0.1
        )
        self.assertEqual(model.calls[0]["system_prompts"], ["Be terse."])
        self.assertEqual(model.calls[0]["temperature"], 0.1)

    def test_empty_response_is_reported_as_failure(self):
        result = model_query.query_model(FakeModel(response="  "), "hello?")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "empty or no response")

    def test_timeout_is_enforced_and_reported(self):
        class Hanging(FakeModel):
            async def _infer_no_context(self, system_prompts, prompt, **kwargs):
                await asyncio.sleep(30)
                return "too late"

        result = model_query.query_model(Hanging(), "hello?", timeout=0.1)
        self.assertFalse(result["ok"])
        self.assertIn("timeout", result["error"])

    def test_unexpected_exception_does_not_propagate(self):
        model = FakeModel(response=RuntimeError("backend exploded"))
        result = model_query.query_model(model, "hello?")
        self.assertFalse(result["ok"])
        self.assertIn("backend exploded", result["error"])

    def test_clamp_helpers_bound_and_default_staff_input(self):
        self.assertEqual(model_query.clamp_timeout("9999"), model_query.MAX_TIMEOUT)
        self.assertEqual(model_query.clamp_timeout("0"), model_query.MIN_TIMEOUT)
        self.assertEqual(model_query.clamp_timeout("abc"), model_query.DEFAULT_TIMEOUT)
        self.assertEqual(model_query.clamp_temperature("7"), 2.0)
        self.assertEqual(model_query.clamp_temperature("-1"), 0.0)
        self.assertEqual(
            model_query.clamp_temperature(None), model_query.DEFAULT_TEMPERATURE
        )


class AdminModelQueryAccessTest(TestCase):
    def test_anonymous_user_redirected(self):
        response = self.client.get(reverse("admin_model_query"))
        self.assertEqual(response.status_code, 302)

    def test_authenticated_non_staff_user_redirected(self):
        User.objects.create_user(username="plain", password="pw123", is_staff=False)
        self.client.login(username="plain", password="pw123")
        self.assertEqual(self.client.get(reverse("admin_model_query")).status_code, 302)

    def test_non_staff_user_cannot_post_a_query(self):
        """The expensive path must be gated too, not just the page render."""
        model = FakeModel()
        User.objects.create_user(username="plain", password="pw123", is_staff=False)
        self.client.login(username="plain", password="pw123")
        with mock.patch(_ROUTER, _fake_router([model])):
            ref = model_query.enumerate_queryable_models()[0][0]
            response = self.client.post(
                reverse("admin_model_query"), {"ref": ref, "prompt": "hello?"}
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(model.calls, [])


class AdminModelQueryViewTest(TestCase):
    def setUp(self):
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")

    @staticmethod
    def _ref_for(models, index=0):
        with mock.patch(_ROUTER, _fake_router(models)):
            return model_query.enumerate_queryable_models()[index][0]

    def test_get_lists_backends_without_calling_any(self):
        model = FakeModel(name="fhi-listed")
        with mock.patch(_ROUTER, _fake_router([model])):
            response = self.client.get(reverse("admin_model_query"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "fhi-listed")
        self.assertEqual(model.calls, [], "listing must not invoke the backend")

    def test_get_with_ref_selects_the_backend_and_shows_the_form(self):
        model = FakeModel(name="fhi-selected")
        ref = self._ref_for([model])
        with mock.patch(_ROUTER, _fake_router([model])):
            response = self.client.get(reverse("admin_model_query"), {"ref": ref})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected"]["label"], "fhi-selected")
        self.assertContains(response, "Send a Query")
        self.assertEqual(model.calls, [])

    def test_post_queries_only_the_selected_backend(self):
        wanted = FakeModel(name="wanted", model="vendor/wanted", response="I answered")
        other = FakeModel(name="other", model="vendor/other", response="not me")
        ref = self._ref_for([other, wanted], index=1)
        with mock.patch(_ROUTER, _fake_router([other, wanted])):
            response = self.client.post(
                reverse("admin_model_query"),
                {"ref": ref, "prompt": "Why was this denied?"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "I answered")
        self.assertEqual(len(wanted.calls), 1)
        self.assertEqual(wanted.calls[0]["prompt"], "Why was this denied?")
        self.assertEqual(other.calls, [], "only the selected backend may be called")

    def test_post_renders_backend_failure_instead_of_erroring(self):
        model = FakeModel(response=RuntimeError("connection refused"))
        ref = self._ref_for([model])
        with mock.patch(_ROUTER, _fake_router([model])):
            response = self.client.post(
                reverse("admin_model_query"), {"ref": ref, "prompt": "hello?"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["result"]["ok"])
        self.assertContains(response, "connection refused")

    def test_post_without_prompt_does_not_call_the_backend(self):
        model = FakeModel()
        ref = self._ref_for([model])
        with mock.patch(_ROUTER, _fake_router([model])):
            response = self.client.post(
                reverse("admin_model_query"), {"ref": ref, "prompt": "   "}
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["result"])
        self.assertIn("Enter a prompt", response.context["error"])
        self.assertEqual(model.calls, [])

    def test_post_with_overlong_prompt_is_rejected(self):
        model = FakeModel()
        ref = self._ref_for([model])
        with mock.patch(_ROUTER, _fake_router([model])):
            response = self.client.post(
                reverse("admin_model_query"),
                {"ref": ref, "prompt": "x" * (model_query.MAX_PROMPT_LENGTH + 1)},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("too long", response.context["error"])
        self.assertEqual(model.calls, [])

    def test_post_with_stale_ref_reports_error_without_querying(self):
        model = FakeModel()
        with mock.patch(_ROUTER, _fake_router([model])):
            response = self.client.post(
                reverse("admin_model_query"),
                {"ref": "Gone|gone|gone|0", "prompt": "hello?"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Unknown model reference", response.context["error"])
        self.assertEqual(model.calls, [])

    def test_staff_supplied_timeout_and_temperature_are_clamped(self):
        model = FakeModel()
        ref = self._ref_for([model])
        with mock.patch(_ROUTER, _fake_router([model])):
            response = self.client.post(
                reverse("admin_model_query"),
                {
                    "ref": ref,
                    "prompt": "hello?",
                    "timeout": "99999",
                    "temperature": "11",
                },
            )
        self.assertEqual(response.context["timeout"], model_query.MAX_TIMEOUT)
        self.assertEqual(model.calls[0]["temperature"], 2.0)

    def test_broken_router_renders_error_row_not_a_500(self):
        broken = mock.MagicMock()
        type(broken).all_models_by_cost = mock.PropertyMock(
            side_effect=RuntimeError("router not ready")
        )
        with mock.patch(_ROUTER, broken):
            response = self.client.get(reverse("admin_model_query"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("router not ready", response.context["enumeration_error"])


class AdminStatusModelQueryLinkTest(TestCase):
    """The status page's model rows must carry a working query link."""

    _MODELS = "fighthealthinsurance.ml.health_status.compute_model_health_details"
    _ACTORS = "fighthealthinsurance.actor_health_status.check_actor_health"
    _FAX = "fighthealthinsurance.fax_health_status.check_fax_backends_health"

    def setUp(self):
        User.objects.create_user(username="staff", password="pw123", is_staff=True)
        self.client.login(username="staff", password="pw123")

    def test_health_details_include_a_resolvable_ref(self):
        from fighthealthinsurance.ml.health_status import compute_model_health_details

        class Backend(FakeModel):
            def model_is_ok(self):
                return True

        model = Backend(name="fhi-linked")
        with mock.patch(_ROUTER, _fake_router([model])):
            details = compute_model_health_details(timeout_seconds=2)
            self.assertIsNotNone(details[0]["ref"])
            self.assertIs(model_query.find_model_by_ref(details[0]["ref"]), model)

    def test_status_page_links_each_model_to_the_query_page(self):
        with mock.patch(self._FAX) as mock_fax, mock.patch(
            self._ACTORS
        ) as mock_actors, mock.patch(self._MODELS) as mock_models:
            mock_models.return_value = [
                {
                    "name": "fhi-2025",
                    "ok": True,
                    "external": False,
                    "error": None,
                    "ref": "NewRemoteInternal|fhi-2025|fhi-2025|0",
                }
            ]
            mock_actors.return_value = {
                "alive_actors": 1,
                "total_actors": 1,
                "details": [],
            }
            mock_fax.return_value = {
                "backends": [],
                "total_backends": 0,
                "sonic": {
                    "configured": False,
                    "active": False,
                    "ok": False,
                    "error": None,
                },
            }
            response = self.client.get(reverse("admin_status"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f"{reverse('admin_model_query')}?ref="
            "NewRemoteInternal%7Cfhi-2025%7Cfhi-2025%7C0",
        )

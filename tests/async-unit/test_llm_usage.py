"""ml/llm_usage.py: every answered model request is counted with its tokens,
by surface, task, tier and network class, and the context that says so
reaches the model threads. The database side is tested in
tests/sync/test_llm_usage_ledger.py; here the ledger is read in memory."""

import asyncio
import datetime
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.test import override_settings
from prometheus_client import REGISTRY

from fighthealthinsurance import client_network
from fighthealthinsurance import exec as fhi_exec
from fighthealthinsurance.ml import llm_usage, llm_usage_ledger, spend, typesafe
from fighthealthinsurance.ml.ml_metrics import ml_call_purpose
from fighthealthinsurance.ml.ml_models import RemoteFullOpenLike
from fighthealthinsurance.utils import (
    fire_and_forget_in_new_threadpool,
    join_fire_and_forget_threads,
)

CF_META = {"HTTP_CF_CONNECTING_IP": "203.0.113.77", "HTTP_CF_IPCOUNTRY": "US"}


@pytest.fixture(autouse=True)
def _empty_ledger():
    llm_usage_ledger._ledger.reset_for_tests()
    yield
    llm_usage_ledger._ledger.reset_for_tests()


@pytest.fixture
def comcast():
    with patch(
        "fhi_users.audit.peek_network_info", return_value=("COMCAST-7922", "US")
    ):
        yield


def _sample(name, **labels):
    value = REGISTRY.get_sample_value(name, labels)
    return value or 0.0


def _record(**kwargs):
    kwargs.setdefault("model", "test-model")
    kwargs.setdefault("tier", "internal")
    kwargs.setdefault("usage", {"prompt_tokens": 10, "completion_tokens": 2})
    llm_usage.record_llm_usage(**kwargs)


def _daily_keys():
    return set(llm_usage_ledger._ledger.pending_for_tests(llm_usage_ledger.DAILY))


def _only_daily_key():
    keys = _daily_keys()
    assert len(keys) == 1, keys
    return dict(zip(llm_usage_ledger._KEY_FIELDS["daily"], next(iter(keys))))


class TestParseUsage:
    def test_openai_shape(self):
        assert llm_usage.parse_usage({"prompt_tokens": 7, "completion_tokens": 3}) == (
            7,
            3,
        )

    def test_anthropic_shape_counts_cache_tokens_as_input(self):
        usage = {
            "input_tokens": 100,
            "cache_creation_input_tokens": 5,
            "cache_read_input_tokens": 20,
            "output_tokens": 40,
        }
        assert llm_usage.parse_usage(usage) == (125, 40)

    def test_typesafe_sends_input_tokens_alone(self):
        assert llm_usage.parse_usage({"input_tokens": 800}) == (800, 0)

    @pytest.mark.parametrize("usage", [None, {}, "lots", 12, {"total_tokens": 9}])
    def test_no_usage_is_none(self, usage):
        assert llm_usage.parse_usage(usage) is None

    @pytest.mark.parametrize(
        "value,expected",
        [(True, 0), (-5, 0), ("12", 12), ("x", 0), (float("nan"), 0), (10**12, 10**7)],
    )
    def test_garbage_counts_are_bounded(self, value, expected):
        parsed = llm_usage.parse_usage({"prompt_tokens": value, "completion_tokens": 1})
        assert parsed == (expected, 1)


class TestRecording:
    def test_a_request_and_its_tokens_are_counted(self):
        labels = dict(model="m-count", tier="internal", surface="unknown", task="other")
        before = _sample("fhi_llm_requests_total", usage="reported", **labels)
        tokens_before = _sample("fhi_llm_tokens_total", kind="prompt", **labels)
        _record(model="m-count")
        assert _sample("fhi_llm_requests_total", usage="reported", **labels) == before + 1
        assert (
            _sample("fhi_llm_tokens_total", kind="prompt", **labels)
            == tokens_before + 10
        )

    def test_missing_usage_counts_the_request_but_no_tokens(self):
        labels = dict(model="m-missing", tier="external", surface="unknown", task="other")
        before = _sample("fhi_llm_requests_total", usage="missing", **labels)
        tokens_before = _sample("fhi_llm_tokens_total", kind="prompt", **labels)
        _record(model="m-missing", tier="external", usage=None)
        assert _sample("fhi_llm_requests_total", usage="missing", **labels) == before + 1
        assert _sample("fhi_llm_tokens_total", kind="prompt", **labels) == tokens_before
        pending = llm_usage_ledger._ledger.pending_for_tests(llm_usage_ledger.DAILY)
        (amounts,) = pending.values()
        assert amounts.usage_missing_calls == 1

    def test_unknown_labels_collapse(self):
        _record(model=None, tier="mainframe", task="world-domination")
        key = _only_daily_key()
        assert (key["model"], key["tier"], key["task"]) == ("unknown", "external", "other")

    def test_garbage_never_raises(self):
        llm_usage.record_llm_usage(model=object(), tier=None, usage=object(), task=5)

    def test_a_broken_ledger_never_raises(self):
        with patch.object(llm_usage_ledger, "add_daily", side_effect=RuntimeError):
            _record()

    def test_the_database_switch_keeps_it_in_prometheus_only(self):
        with override_settings(FHI_LLM_USAGE_DB=False):
            _record()
        assert _daily_keys() == set()


class TestTasks:
    def test_a_purpose_default(self):
        with ml_call_purpose("chat"):
            assert llm_usage.resolve_task() == "chat_reply"
        with ml_call_purpose("appeal"):
            assert llm_usage.resolve_task() == "appeal_letter"

    def test_a_step_beats_the_purpose_default(self):
        with llm_usage.llm_task("chat_analysis"), ml_call_purpose("chat"):
            assert llm_usage.resolve_task() == "chat_analysis"

    def test_the_call_site_beats_a_step(self):
        with llm_usage.llm_task("questions"):
            assert llm_usage.resolve_task("letter_scoring") == "letter_scoring"

    def test_a_probe_beats_the_call_site(self):
        with ml_call_purpose("probe"):
            assert llm_usage.resolve_task("letter_scoring") == "probe"

    def test_a_pin_beats_everything_inside_it(self):
        with llm_usage.llm_task("chooser", pin=True), llm_usage.llm_task("synthesis"):
            with ml_call_purpose("probe"):
                assert llm_usage.resolve_task("letter_scoring") == "chooser"

    def test_the_innermost_step_wins(self):
        with llm_usage.llm_task("questions"), llm_usage.llm_task("citations"):
            assert llm_usage.resolve_task() == "citations"
        assert llm_usage.resolve_task() == "other"

    @pytest.mark.asyncio
    async def test_labelled_task_decorates_an_async_function(self):
        @llm_usage.labelled_task("entity_extraction")
        async def extract():
            return llm_usage.resolve_task()

        assert await extract() == "entity_extraction"


class TestSurfaces:
    def test_no_origin_is_unknown(self):
        assert llm_usage.resolve_origin("other").surface == "unknown"

    def test_no_origin_for_a_probe_or_the_chooser_is_system(self):
        assert llm_usage.resolve_origin("probe").surface == "system"
        assert llm_usage.resolve_origin("chooser").surface == "system"

    def test_an_assistant_case_is_assistant_whatever_the_entry_point_said(self):
        with llm_usage.origin(llm_usage.Origin("site")), spend.for_channel("assistant"):
            assert llm_usage.resolve_origin("other").surface == "assistant"

    def test_staff_work_stays_staff_on_an_assistant_case(self):
        with llm_usage.staff_work("staff_query"), spend.for_channel("assistant"):
            assert llm_usage.resolve_origin("other").surface == "staff"
            assert llm_usage.resolve_task() == "staff_query"

    def test_set_surface_is_undone_by_its_scope(self):
        with llm_usage.origin(llm_usage.Origin("site")):
            with llm_usage.origin_scope():
                llm_usage.set_surface("pro")
                assert llm_usage.current_origin().surface == "pro"
            assert llm_usage.current_origin().surface == "site"


class TestOrigins:
    def test_cloudflares_address_is_bucketed_and_classed(self, comcast):
        where = llm_usage.origin_from_meta(CF_META)
        assert where.prefix == "203.0.113.0/24"
        assert (where.network_class, where.asn_name, where.country) == (
            "isp",
            "COMCAST-7922",
            "US",
        )

    def test_the_prefix_is_never_in_the_repr(self, comcast):
        assert "203.0.113" not in repr(llm_usage.origin_from_meta(CF_META))

    def test_a_forwarded_for_header_alone_names_nothing(self, comcast):
        where = llm_usage.origin_from_meta({"HTTP_X_FORWARDED_FOR": "203.0.113.77"})
        assert where.prefix is None
        assert where.network_class == "unknown"
        assert where.asn_name == ""

    def test_tor_has_no_country(self, comcast):
        where = llm_usage.origin_from_meta({**CF_META, "HTTP_CF_IPCOUNTRY": "T1"})
        assert (where.network_class, where.country) == ("tor", "")

    def test_a_scope_reads_the_same_headers(self, comcast):
        scope = {"headers": [(b"cf-connecting-ip", b"2001:db8:1:2::9")]}
        where = llm_usage.origin_from_scope(scope, "pro")
        assert (where.surface, where.prefix) == ("pro", "2001:db8:1::/48")

    def _denial(self, **fields):
        base = dict(
            channel="site",
            creating_professional_id=None,
            primary_professional_id=None,
            asn_name="",
        )
        return SimpleNamespace(**{**base, **fields})

    def test_an_assistant_denial_is_assistant(self):
        where = llm_usage.origin_for_denial(
            llm_usage.Origin("site"), self._denial(channel="assistant"), False
        )
        assert where.surface == "assistant"

    def test_a_handoff_denial_is_assistant(self):
        where = llm_usage.origin_for_denial(None, self._denial(), True)
        assert where.surface == "assistant"

    def test_a_professionals_denial_is_pro(self):
        where = llm_usage.origin_for_denial(
            None, self._denial(creating_professional_id=4), False
        )
        assert where.surface == "pro"

    def test_without_an_entry_point_the_denials_asn_gives_the_class_unkeyed(self):
        where = llm_usage.origin_for_denial(
            None, self._denial(asn_name="AMAZON-02"), False
        )
        assert (where.surface, where.network_class, where.prefix) == (
            "site",
            "hosting",
            None,
        )

    def test_the_entry_points_network_is_kept(self, comcast):
        entry = llm_usage.origin_from_meta(CF_META)
        where = llm_usage.origin_for_denial(entry, self._denial(asn_name="X"), False)
        assert where.prefix == "203.0.113.0/24"
        assert where.asn_name == "COMCAST-7922"

    def test_a_pro_chat(self):
        chat = SimpleNamespace(chat_type="trial_professional", asn_name="")
        assert llm_usage.origin_for_chat(chat).surface == "pro"


class TestNetworkRows:
    def test_a_person_with_an_address_gets_a_weekly_key(self, comcast):
        with llm_usage.origin(llm_usage.origin_from_meta(CF_META)):
            _record()
        week = llm_usage_ledger._ledger.pending_for_tests(llm_usage_ledger.WEEK)
        ((week_start, key, surface, network_class),) = week
        assert week_start.weekday() == 0
        assert len(key) == 64 and "203.0.113" not in key
        assert (surface, network_class) == ("site", "isp")
        network = llm_usage_ledger._ledger.pending_for_tests(llm_usage_ledger.NETWORK)
        ((_, _, _, asn_name, country),) = network
        assert (asn_name, country) == ("COMCAST-7922", "US")

    def test_the_switch_turns_the_keys_off(self, comcast):
        with override_settings(FHI_LLM_USAGE_NETWORK_KEYS=False):
            with llm_usage.origin(llm_usage.origin_from_meta(CF_META)):
                _record()
        assert llm_usage_ledger._ledger.pending_for_tests(llm_usage_ledger.WEEK) == {}

    def test_system_work_has_no_network_rows(self):
        with llm_usage.system_work("chooser"):
            _record()
        assert llm_usage_ledger._ledger.pending_for_tests(llm_usage_ledger.NETWORK) == {}
        assert llm_usage_ledger._ledger.pending_for_tests(llm_usage_ledger.WEEK) == {}

    def test_staff_are_never_keyed(self):
        with llm_usage.staff_work():
            _record()
        assert llm_usage_ledger._ledger.pending_for_tests(llm_usage_ledger.WEEK) == {}

    def test_a_week_key_is_the_same_all_week(self, comcast):
        with llm_usage.origin(llm_usage.origin_from_meta(CF_META)):
            for day in (5, 8, 11):
                with patch.object(
                    llm_usage, "_today", return_value=datetime.date(2026, 10, day)
                ):
                    _record()
        assert len(llm_usage_ledger._ledger.pending_for_tests(llm_usage_ledger.WEEK)) == 1


class TestPropagation:
    """The origin and task reach the model threads like the purpose does."""

    def _seen(self):
        where = llm_usage.current_origin()
        return (where.surface if where else None, llm_usage.resolve_task())

    def test_the_executor(self):
        with llm_usage.origin(llm_usage.Origin("pro")), llm_usage.llm_task("questions"):
            seen = fhi_exec.executor.submit(self._seen).result(timeout=10)
        assert seen == ("pro", "questions")

    def test_fire_and_forget(self):
        seen = []

        async def work():
            seen.append(self._seen())

        async def start():
            await fire_and_forget_in_new_threadpool(work())

        with llm_usage.origin(llm_usage.Origin("pro")), llm_usage.llm_task("citations"):
            asyncio.run(start())
        join_fire_and_forget_threads(timeout=10)
        assert seen == [("pro", "citations")]

    @pytest.mark.asyncio
    async def test_a_task_and_a_sync_bridge(self):
        async def seen():
            return self._seen()

        with llm_usage.origin(llm_usage.Origin("pro")), llm_usage.llm_task("questions"):
            in_task = await asyncio.create_task(seen())
            # Plain asgiref in tests (CLAUDE.md); it touches no ORM anyway.
            in_thread = await sync_to_async(self._seen)()
        assert in_task == in_thread == ("pro", "questions")

    def test_async_to_sync(self):
        async def seen():
            return self._seen()

        with llm_usage.origin(llm_usage.Origin("site")), llm_usage.llm_task("questions"):
            assert async_to_sync(seen)() == ("site", "questions")


class _FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self.payload


class _FakeSession:
    def __init__(self, payload):
        self.response = _FakeResponse(payload)

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, *args, **kwargs):
        return self.response


class TestTransports:
    @pytest.mark.asyncio
    async def test_an_openai_answer_is_counted_under_its_registry_name(
        self, monkeypatch, make_fake_model_post
    ):
        model = RemoteFullOpenLike("http://usage.example/v1", "test-token", "wire-id")
        model.name = "registry-name"
        body = {
            "choices": [{"message": {"content": "Dear insurer, please pay."}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 5},
        }
        monkeypatch.setattr(
            aiohttp.ClientSession, "post", make_fake_model_post(200, json_data=body)
        )
        with patch.object(llm_usage, "record_llm_usage") as record:
            await model._infer(system_prompts=["sys"], prompt="hi")
        record.assert_called_once()
        kwargs = record.call_args.kwargs
        assert kwargs["model"] == "registry-name"
        assert kwargs["tier"] == "external"
        assert llm_usage.parse_usage(kwargs["usage"]) == (11, 5)

    @pytest.mark.asyncio
    async def test_an_error_body_is_not_counted(self, monkeypatch, make_fake_model_post):
        model = RemoteFullOpenLike("http://usage.example/v1", "test-token", "wire-id")
        body = {"object": "error", "message": "nope"}
        monkeypatch.setattr(
            aiohttp.ClientSession, "post", make_fake_model_post(200, json_data=body)
        )
        with patch.object(llm_usage, "record_llm_usage") as record:
            await model._infer(system_prompts=["sys"], prompt="hi")
        record.assert_not_called()

    def test_typesafe_counts_under_the_task_its_caller_names(self):
        payload = {"answers": {}, "model": "jev-1.13.0", "usage": {"input_tokens": 9}}
        settings = dict(
            TYPESAFE_API_KEY="test-key",
            TYPESAFE_API_URL="https://typesafe.invalid/v1/systemone",
        )
        with (
            override_settings(**settings),
            patch.object(typesafe.aiohttp, "ClientSession", _FakeSession(payload)),
            patch.object(spend, "allows", return_value=True),
            patch.object(llm_usage, "record_llm_usage") as record,
        ):
            asyncio.run(
                typesafe.ask("state", {}, timeout_seconds=1, task="letter_scoring")
            )
        kwargs = record.call_args.kwargs
        assert kwargs["model"] == "typesafe/jev-1.13.0"
        assert kwargs["task"] == "letter_scoring"
        assert kwargs["tier"] == "external"


def test_every_network_class_is_a_label_value():
    assert set(client_network.NETWORK_CLASSES) >= {"isp", "none", "unknown"}


class TestSocketOrigin:
    """websockets.LLMUsageOriginMixin: a socket's messages count from the
    network Cloudflare saw and the consumer's surface."""

    def _consumer(self, mixin_base_surface=None, headers=()):
        from fighthealthinsurance.websockets import LLMUsageOriginMixin

        class _Base:
            async def websocket_receive(self, message):
                self.seen = llm_usage.current_origin()

        class _Probe(LLMUsageOriginMixin, _Base):
            pass

        if mixin_base_surface:
            _Probe.LLM_SURFACE = mixin_base_surface
        consumer = _Probe()
        consumer.scope = {"headers": list(headers), "client": ("10.0.0.1", 1)}
        return consumer

    @pytest.mark.asyncio
    async def test_a_message_counts_from_the_cloudflare_address(self, comcast):
        consumer = self._consumer(headers=[(b"cf-connecting-ip", b"203.0.113.77")])
        await consumer.websocket_receive({})
        assert (consumer.seen.surface, consumer.seen.prefix) == ("site", "203.0.113.0/24")
        assert llm_usage.current_origin() is None

    @pytest.mark.asyncio
    async def test_forwarded_for_alone_names_nothing(self, comcast):
        consumer = self._consumer(headers=[(b"x-forwarded-for", b"203.0.113.77")])
        await consumer.websocket_receive({})
        assert consumer.seen.prefix is None
        assert consumer.seen.network_class == "unknown"

    @pytest.mark.asyncio
    async def test_a_chat_found_to_be_a_professionals_stays_pro(self):
        consumer = self._consumer()
        await consumer.websocket_receive({})
        consumer.refine_llm_surface("pro")
        await consumer.websocket_receive({})
        assert consumer.seen.surface == "pro"

    def test_prior_auth_is_pro_and_every_model_socket_has_the_mixin(self):
        from fighthealthinsurance import websockets

        assert websockets.PriorAuthConsumer.LLM_SURFACE == "pro"
        for name in (
            "StreamingAppealsBackend",
            "StreamingEscalationBackend",
            "StreamingEntityBackend",
            "PriorAuthConsumer",
            "OngoingChatConsumer",
        ):
            consumer = getattr(websockets, name)
            assert issubclass(consumer, websockets.LLMUsageOriginMixin), name

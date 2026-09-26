"""The chat routing policy (ml/chat_policy.py) and how the router follows it.

Three parts: the pure compute_policy rules, the router narrowing (it may
only narrow the outside models, never add one, never override a person's
choice to keep chat on our own models, and it sets the policy aside when
none of ours is selectable), and the reader's fallbacks to the default.
"""

import asyncio
import datetime
from unittest.mock import patch

import pytest
from asgiref.sync import sync_to_async
from django.test import override_settings
from django.utils import timezone

from fighthealthinsurance.ml import chat_policy
from fighthealthinsurance.ml.chat_policy import (
    DEFAULT_POLICY,
    ChatAggregates,
    ChatPolicy,
    ModelAggregate,
    PolicyRules,
    aget_chat_policy,
    compute_policy,
    narrow_externals,
    policy_from_row,
)
from fighthealthinsurance.ml.ml_router import MLRouter
from fighthealthinsurance.models import ChatRoutingPolicy

# --- compute_policy ---------------------------------------------------------


def _aggregates(**overrides):
    """A busy, healthy window: our models answer usably in about 8 s, one
    outside model wins the turns ours miss, two others never win."""
    fields = dict(
        window_minutes=1440,
        turns=200,
        ok_turns=195,
        internal_turns=200,
        internal_usable_turns=190,
        internal_ttu_ms=[4000] * 50 + [8000] * 100 + [12000] * 40,
        recent_internal_turns=12,
        recent_internal_usable_turns=11,
        models={
            "fhi-local": ModelAggregate(
                external=False, asked=200, wins=185, calls=600, usable_calls=560
            ),
            "claude-opus": ModelAggregate(
                external=True, asked=200, wins=10, calls=400, usable_calls=390
            ),
            "claude-sonnet": ModelAggregate(
                external=True, asked=200, wins=0, calls=400, usable_calls=380
            ),
            "deepseek": ModelAggregate(
                external=True, asked=200, wins=0, calls=400, usable_calls=300
            ),
        },
        calls_today={"claude-opus": 120, "deepseek": 30},
    )
    fields.update(overrides)
    return ChatAggregates(**fields)


def test_below_the_minimum_turns_the_routing_stays_the_default():
    policy = compute_policy(_aggregates(turns=49))
    assert policy.external_excluded == ()
    assert policy.external_delay_seconds == 0.0
    assert policy.reason == "few_turns"
    assert policy.narrows_nothing


def test_caps_apply_even_below_the_minimum_turns():
    policy = compute_policy(_aggregates(turns=3), caps={"claude-opus": 100})
    assert policy.exhausted == ("claude-opus",)
    assert policy.external_excluded == ()
    assert policy.reason == "few_turns,capped"


def test_the_top_healthy_outside_model_is_kept_and_the_non_winners_go():
    policy = compute_policy(_aggregates())
    assert "claude-opus" not in policy.external_excluded
    assert policy.external_excluded == ("claude-sonnet", "deepseek")
    assert policy.reason == "ok"


def test_the_top_outside_model_is_kept_even_with_no_wins_at_all():
    aggregates = _aggregates()
    aggregates.models["claude-opus"].wins = 0
    policy = compute_policy(aggregates)
    # Ranked by wins, then by usable rate: opus answers usably most often.
    assert policy.external_excluded == ("claude-sonnet", "deepseek")


def test_an_outside_model_that_wins_is_kept():
    aggregates = _aggregates()
    aggregates.models["deepseek"].wins = 1
    policy = compute_policy(aggregates)
    assert policy.external_excluded == ("claude-sonnet",)


def test_an_outside_model_with_too_few_asks_is_not_judged():
    aggregates = _aggregates()
    aggregates.models["deepseek"].asked = 19
    policy = compute_policy(aggregates)
    assert policy.external_excluded == ("claude-sonnet",)


def test_every_outside_model_is_kept_while_ours_fail_often():
    # 25% of turns without a usable answer of ours.
    policy = compute_policy(_aggregates(internal_usable_turns=150))
    assert policy.external_excluded == ()
    assert "keep_all_internals_failing" in policy.reason


def test_every_outside_model_is_kept_when_none_looks_healthy():
    aggregates = _aggregates()
    for name in ("claude-opus", "claude-sonnet", "deepseek"):
        aggregates.models[name].usable_calls = 10
    policy = compute_policy(aggregates)
    assert policy.external_excluded == ()
    assert "keep_all_no_healthy_external" in policy.reason


def test_the_delay_is_our_75th_percentile_time_to_a_usable_answer():
    policy = compute_policy(_aggregates())
    assert policy.internal_ttu_p75_ms == 8000
    assert policy.external_delay_seconds == 8.0


@pytest.mark.parametrize(
    "ttu_ms, expected",
    [(1500, 5.0), (40000, 15.0)],
)
def test_the_delay_is_clamped(ttu_ms, expected):
    policy = compute_policy(_aggregates(internal_ttu_ms=[ttu_ms] * 30))
    assert policy.external_delay_seconds == expected


def test_no_delay_when_ours_mostly_failed_in_the_last_hour():
    policy = compute_policy(_aggregates(recent_internal_usable_turns=5))
    assert policy.external_delay_seconds == 0.0
    assert policy.internal_usable_rate == pytest.approx(5 / 12)
    assert "no_delay_internals_down" in policy.reason


def test_no_delay_after_a_quiet_hour():
    policy = compute_policy(
        _aggregates(recent_internal_turns=2, recent_internal_usable_turns=2)
    )
    assert policy.external_delay_seconds == 0.0
    assert "no_delay_quiet_hour" in policy.reason


def test_no_delay_without_enough_timed_answers():
    policy = compute_policy(_aggregates(internal_ttu_ms=[8000] * 19))
    assert policy.external_delay_seconds == 0.0
    assert "no_delay_few_samples" in policy.reason


def test_a_model_at_its_cap_is_exhausted_and_bad_caps_are_ignored():
    policy = compute_policy(
        _aggregates(),
        caps={
            "claude-opus": 120,
            "deepseek": 31,
            "bad-negative": -1,
            "bad-bool": True,
            7: 3,
        },
    )
    assert policy.exhausted == ("claude-opus",)
    assert policy.daily_call_caps == {"claude-opus": 120, "deepseek": 31}
    assert policy.calls_today == {"claude-opus": 120, "deepseek": 30}
    assert policy.reason.endswith("capped")


def test_rules_are_knobs():
    policy = compute_policy(_aggregates(turns=20), rules=PolicyRules(min_turns=10))
    assert policy.external_excluded == ("claude-sonnet", "deepseek")


def test_compute_policy_needs_no_database():
    # No django_db mark: any query here would fail the test.
    compute_policy(_aggregates(), caps={"claude-opus": 1})


def test_a_computed_policy_holds_names_and_numbers_only():
    fields = compute_policy(_aggregates(), caps={"deepseek": 5}).row_fields()
    for value in fields.values():
        assert isinstance(value, (str, int, float, list, dict, type(None)))
    for name in fields["external_excluded"] + fields["exhausted"]:
        assert name in {"claude-opus", "claude-sonnet", "deepseek"}


# --- narrow_externals and the router ------------------------------------------


class _Backend:
    def __init__(self, name, external, quality=100, available=True, general=True):
        self.name = name
        self.external = external
        self._quality = quality
        self._available = available
        self._general = general
        self.health_checked_live = True

    def quality(self):
        return self._quality

    def is_available(self):
        return self._available

    def supports_general_instructions(self):
        return self._general

    def __str__(self):
        return self.name


def _router(internal_available=True):
    router = MLRouter.__new__(MLRouter)
    ours = _Backend("fhi-local", False, 120, available=internal_available)
    other = _Backend("may", False, 110, available=internal_available)
    router.ours = ours
    router.externals = {
        name: _Backend(name, True, quality)
        for name, quality in (
            ("claude-opus", 90),
            ("claude-sonnet", 80),
            ("deepseek", 70),
            ("fourth-external", 60),
        )
    }
    router.models_by_name = {"fhi-local": [ours], "may": [other]}
    router.internal_models_by_cost = [other, ours]
    router.external_models_by_cost = list(router.externals.values())
    router.all_models_by_cost = router.internal_models_by_cost + list(
        router.externals.values()
    )
    router.context_only_models_by_cost = []
    return router


def _externals(models):
    return [str(m) for m in models if m.external]


def test_narrow_externals_only_ever_returns_a_subset_in_order():
    externals = ["a", "b", "c"]
    policy = ChatPolicy(external_excluded=("b", "not-offered"), exhausted=("zzz",))
    assert narrow_externals(externals, policy) == ["a", "c"]


def test_narrow_externals_keeps_the_best_one_when_all_are_left_out():
    policy = ChatPolicy(external_excluded=("a", "b", "c"))
    assert narrow_externals(["a", "b", "c"], policy) == ["a"]


def test_narrow_externals_lets_caps_empty_the_list():
    policy = ChatPolicy(exhausted=("a", "b"), external_excluded=("c",))
    assert narrow_externals(["a", "b"], policy) == []


def test_the_router_narrows_the_outside_models_with_a_policy():
    router = _router()
    policy = ChatPolicy(external_excluded=("claude-sonnet",))
    primary, fallback = router.get_chat_backends_with_fallback(
        use_external=True, policy=policy
    )
    assert _externals(primary) == ["claude-opus", "deepseek"]
    assert fallback == []


def test_the_router_never_adds_a_model():
    router = _router()
    default_primary, _ = router.get_chat_backends_with_fallback(use_external=True)
    # The fourth outside model is outside the default top three; leaving
    # out one of the three must not pull it in.
    policy = ChatPolicy(
        external_excluded=("claude-opus", "not-a-model"), exhausted=("deepseek",)
    )
    primary, fallback = router.get_chat_backends_with_fallback(
        use_external=True, policy=policy
    )
    assert _externals(default_primary) == ["claude-opus", "claude-sonnet", "deepseek"]
    assert _externals(primary) == ["claude-sonnet"]
    assert set(map(id, primary)) <= set(map(id, default_primary))
    assert fallback == []


def test_the_router_keeps_chat_on_our_models_without_consent():
    router = _router()
    policy = ChatPolicy(external_delay_seconds=10.0, external_excluded=("deepseek",))
    primary, fallback = router.get_chat_backends_with_fallback(
        use_external=False, policy=policy
    )
    assert _externals(primary) == []
    assert fallback == []
    assert router.get_chat_backends(use_external=False, policy=policy) == primary


def test_the_router_sets_the_policy_aside_when_none_of_ours_is_selectable():
    router = _router(internal_available=False)
    policy = ChatPolicy(
        external_delay_seconds=10.0,
        external_excluded=("claude-sonnet", "deepseek"),
        exhausted=("claude-opus",),
    )
    primary, _ = router.get_chat_backends_with_fallback(
        use_external=True, policy=policy
    )
    assert _externals(primary) == ["claude-opus", "claude-sonnet", "deepseek"]
    assert router.chat_policy_in_force(policy) is None
    assert router.chat_external_delay(policy) == 0.0


def test_the_router_hands_the_delay_on_while_ours_are_selectable():
    router = _router()
    assert router.chat_external_delay(ChatPolicy(external_delay_seconds=7.5)) == 7.5
    assert router.chat_external_delay(None) == 0.0
    assert router.chat_external_delay(DEFAULT_POLICY) == 0.0


def test_the_retry_fallbacks_come_from_the_same_narrowed_list():
    router = _router()
    policy = ChatPolicy(external_excluded=("claude-sonnet",))
    # With the primary list holding no outside model (as a mocked primary
    # list can), the fallbacks are the narrowed outside models.
    with patch.object(router, "get_chat_backends", return_value=[router.ours]):
        _primary, fallback = router.get_chat_backends_with_fallback(
            use_external=True, policy=policy
        )
    assert [str(m) for m in fallback] == ["claude-opus", "deepseek"]


def test_without_a_policy_the_router_calls_get_chat_backends_as_before():
    router = _router()
    with patch.object(router, "get_chat_backends", return_value=[]) as mock_get:
        router.get_chat_backends_with_fallback(use_external=True)
    mock_get.assert_called_once_with(use_external=True)


# --- The reader ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_cache():
    chat_policy.reset_chat_policy_cache()
    yield
    chat_policy.reset_chat_policy_cache()


async def _store(minutes_ago=0, **fields):
    defaults = dict(
        source="manual",
        window_minutes=1440,
        turns_considered=200,
        external_excluded=["deepseek"],
        external_delay_seconds=8.0,
        reason="ok",
    )
    defaults.update(fields)
    row = await sync_to_async(ChatRoutingPolicy.objects.create)(**defaults)
    if minutes_ago:
        await sync_to_async(ChatRoutingPolicy.objects.filter(pk=row.pk).update)(
            created_at=timezone.now() - datetime.timedelta(minutes=minutes_ago)
        )
    return row


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_with_the_switch_off_the_default_is_used_without_a_read():
    await _store()
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=False),
        patch.object(chat_policy, "_aread_newest") as read,
    ):
        assert await aget_chat_policy() is DEFAULT_POLICY
    read.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_fresh_row_is_followed():
    row = await _store()
    with override_settings(FHI_CHAT_POLICY_APPLY=True):
        policy = await aget_chat_policy()
    assert policy.row_id == row.pk
    assert policy.external_excluded == ("deepseek",)
    assert policy.external_delay_seconds == 8.0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_newest_row_wins():
    await _store(minutes_ago=5, external_delay_seconds=6.0)
    newest = await _store(external_delay_seconds=9.0)
    with override_settings(FHI_CHAT_POLICY_APPLY=True):
        policy = await aget_chat_policy()
    assert policy.row_id == newest.pk


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_an_empty_table_gives_the_default():
    with override_settings(FHI_CHAT_POLICY_APPLY=True):
        assert await aget_chat_policy() is DEFAULT_POLICY


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_stale_row_gives_the_default():
    await _store(minutes_ago=61)
    with override_settings(
        FHI_CHAT_POLICY_APPLY=True, FHI_CHAT_POLICY_MAX_AGE_MINUTES=60
    ):
        assert await aget_chat_policy() is DEFAULT_POLICY


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_cached_row_is_still_checked_for_age_on_every_call():
    await _store(minutes_ago=30)
    with override_settings(FHI_CHAT_POLICY_APPLY=True):
        with override_settings(FHI_CHAT_POLICY_MAX_AGE_MINUTES=60):
            assert (await aget_chat_policy()).row_id is not None
        with override_settings(FHI_CHAT_POLICY_MAX_AGE_MINUTES=20):
            assert await aget_chat_policy() is DEFAULT_POLICY


@pytest.mark.parametrize(
    "fields",
    [
        {"schema_version": 2},
        {"external_delay_seconds": 16.0},
        {"external_delay_seconds": -1.0},
        {"external_excluded": "deepseek"},
        {"external_excluded": [7]},
        {"exhausted": {"deepseek": 1}},
        {"daily_call_caps": {"deepseek": "lots"}},
    ],
)
@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_row_that_does_not_parse_gives_the_default(fields):
    await _store(**fields)
    with override_settings(FHI_CHAT_POLICY_APPLY=True):
        assert await aget_chat_policy() is DEFAULT_POLICY


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_database_error_gives_the_default():
    await _store()
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(
            ChatRoutingPolicy.objects, "order_by", side_effect=RuntimeError("db down")
        ),
    ):
        assert await aget_chat_policy() is DEFAULT_POLICY


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_slow_read_gives_the_default_within_the_bound():
    await _store()

    class _SlowQuery:
        async def afirst(self):
            await asyncio.sleep(5)

    loop = asyncio.get_running_loop()
    started = loop.time()
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(chat_policy, "POLICY_READ_TIMEOUT_SECONDS", 0.05),
        patch.object(ChatRoutingPolicy.objects, "order_by", return_value=_SlowQuery()),
    ):
        assert await aget_chat_policy() is DEFAULT_POLICY
    assert loop.time() - started < 1.0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_row_is_read_once_per_cache_period():
    await _store()
    real = chat_policy._aread_newest
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(chat_policy, "_aread_newest", side_effect=real) as read,
    ):
        first = await aget_chat_policy()
        second = await aget_chat_policy()
        assert first == second
        assert read.call_count == 1
        chat_policy.reset_chat_policy_cache()
        await aget_chat_policy()
        assert read.call_count == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_failed_read_is_not_retried_on_every_turn():
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(chat_policy, "_aread_newest", return_value=None) as read,
    ):
        assert await aget_chat_policy() is DEFAULT_POLICY
        assert await aget_chat_policy() is DEFAULT_POLICY
    assert read.call_count == 1


def test_policy_from_row_reads_every_field():
    row = ChatRoutingPolicy(
        pk=3,
        source="manual",
        window_minutes=60,
        turns_considered=80,
        external_excluded=["deepseek"],
        external_delay_seconds=5.5,
        daily_call_caps={"claude-opus": 100},
        calls_today={"claude-opus": 40},
        exhausted=[],
        internal_usable_rate=0.9,
        internal_ttu_p75_ms=5500,
        reason="ok",
        created_at=timezone.now(),
    )
    policy = policy_from_row(row)
    assert policy is not None
    assert policy.row_id == 3
    assert policy.daily_call_caps == {"claude-opus": 100}
    assert policy.internal_ttu_p75_ms == 5500
    assert not policy.narrows_nothing

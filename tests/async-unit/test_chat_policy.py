"""The chat routing policy (ml/chat_policy.py) and how the router follows it.

Three parts: the pure compute_policy rules, the router narrowing (it may
only narrow the outside models, never add one, never override a person's
choice to keep chat on our own models, and it sets the policy aside when
none of ours is selectable), and the reader: its fallbacks to the default,
its one-at-a-time background refresh, and that a stuck read holds up
neither a turn nor the chat's database executor.
"""

import asyncio
import datetime
import threading
import time
import types
from unittest.mock import patch

import pytest
from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.db import OperationalError, connections
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
from fighthealthinsurance.models import ChatRoutingPolicy, ChatTurn, OngoingChat

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
    )
    fields.update(overrides)
    return ChatAggregates(**fields)


ROSTER = ["claude-sonnet", "new-model", "claude-opus", "deepseek"]


def test_below_the_minimum_turns_the_routing_stays_the_default():
    policy = compute_policy(_aggregates(turns=49), roster=ROSTER, hold_seconds=8.0)
    assert policy.external_excluded == ()
    assert policy.external_delay_seconds == 0.0
    assert policy.outside_order == ()
    assert policy.reason == "few_turns"
    assert policy.narrows_nothing


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


def test_the_hold_is_the_setting_and_the_percentile_is_reported():
    policy = compute_policy(_aggregates(), hold_seconds=8.0)
    assert policy.external_delay_seconds == 8.0
    assert policy.internal_ttu_p75_ms == 8000


@pytest.mark.parametrize(
    "hold, expected",
    [(40.0, 15.0), (-1.0, 0.0), (float("nan"), 0.0), (0.0, 0.0)],
)
def test_the_hold_is_kept_within_bounds(hold, expected):
    policy = compute_policy(_aggregates(), hold_seconds=hold)
    assert policy.external_delay_seconds == expected


def test_the_roster_order_stands_until_models_have_enough_turns():
    aggregates = _aggregates()
    for name in ("claude-sonnet", "claude-opus", "deepseek"):
        aggregates.models[name].asked = 29
    policy = compute_policy(aggregates, roster=ROSTER)
    assert policy.outside_order == tuple(ROSTER)
    assert policy.order_scores == {}
    assert "ordered" not in policy.reason


def test_well_sampled_models_reorder_among_their_own_places():
    aggregates = _aggregates()
    aggregates.models["deepseek"].wins = 5
    aggregates.models["new-model"] = ModelAggregate(external=True, asked=5, wins=5)
    policy = compute_policy(aggregates, roster=ROSTER)
    # opus (10/200) and deepseek (5/200) move ahead of sonnet (0/200) into
    # the places those three held; new-model, with too few turns, keeps
    # its second place.
    assert policy.outside_order == (
        "claude-opus",
        "new-model",
        "deepseek",
        "claude-sonnet",
    )
    assert policy.order_scores["claude-opus"] == (0.05, 200)
    assert "new-model" not in policy.order_scores
    assert "ordered" in policy.reason


def test_the_order_is_the_roster_deduplicated_and_nothing_else():
    policy = compute_policy(
        _aggregates(), roster=["claude-opus", "claude-opus", "", "never-asked"]
    )
    assert policy.outside_order == ("claude-opus", "never-asked")


def test_rules_are_knobs():
    policy = compute_policy(_aggregates(turns=20), rules=PolicyRules(min_turns=10))
    assert policy.external_excluded == ("claude-sonnet", "deepseek")


def test_compute_policy_needs_no_database():
    # No django_db mark: any query here would fail the test.
    compute_policy(_aggregates(), roster=ROSTER, hold_seconds=8.0)


def test_a_computed_policy_holds_names_and_numbers_only():
    fields = compute_policy(_aggregates(), roster=ROSTER).row_fields()
    for value in fields.values():
        assert isinstance(value, (str, int, float, list, dict, type(None)))
    for name in fields["external_excluded"] + fields["outside_order"]:
        assert name in set(ROSTER) | {"claude-opus", "claude-sonnet", "deepseek"}


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
    policy = ChatPolicy(external_excluded=("b", "not-offered"))
    assert narrow_externals(externals, policy) == ["a", "c"]


def test_narrow_externals_keeps_the_best_one_when_all_are_left_out():
    policy = ChatPolicy(external_excluded=("a", "b", "c"))
    assert narrow_externals(["a", "b", "c"], policy) == ["a"]


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
    policy = ChatPolicy(external_excluded=("claude-opus", "not-a-model", "deepseek"))
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
        outside_order=("deepseek",),
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


class _Named:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return self.name


def test_exploration_gives_the_second_place_to_a_model_further_down():
    from fighthealthinsurance.ml import ml_router

    router = MLRouter.__new__(MLRouter)
    candidates = [_Named(n) for n in ("a", "b", "c", "d", "e")]
    with override_settings(FHI_CHAT_EXPLORE_RATE=0.2):
        with patch.object(ml_router, "_explore_draw", side_effect=[0.1, 0.6]):
            chosen = router._explore(list(candidates))
        assert [str(m) for m in chosen] == ["a", "e", "c"]
        with patch.object(ml_router, "_explore_draw", return_value=0.5):
            chosen = router._explore(list(candidates))
        assert [str(m) for m in chosen] == ["a", "b", "c"]


def test_no_exploration_without_models_further_down_or_at_rate_zero():
    from fighthealthinsurance.ml import ml_router

    router = MLRouter.__new__(MLRouter)
    three = [_Named(n) for n in ("a", "b", "c")]
    with (
        override_settings(FHI_CHAT_EXPLORE_RATE=1.0),
        patch.object(ml_router, "_explore_draw", return_value=0.0),
    ):
        assert [str(m) for m in router._explore(list(three))] == ["a", "b", "c"]
    five = [_Named(n) for n in ("a", "b", "c", "d", "e")]
    with (
        override_settings(FHI_CHAT_EXPLORE_RATE=0.0),
        patch.object(ml_router, "_explore_draw", return_value=0.0),
    ):
        assert [str(m) for m in router._explore(list(five))] == ["a", "b", "c"]


def test_a_policy_in_force_hands_its_learned_order_to_the_roster():
    router = _router()
    policy = ChatPolicy(outside_order=("deepseek", "claude-opus"))
    with (
        override_settings(FHI_CHAT_OUTSIDE_MODELS=["claude-opus", "deepseek"]),
        patch.object(router, "chat_outside_models", return_value=[]) as roster,
    ):
        router.get_chat_backends_with_fallback(use_external=True, policy=policy)
    assert roster.call_args.args[0] == ["deepseek", "claude-opus"]


# --- The reader ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_cache():
    chat_policy.reset_chat_policy_cache()
    yield
    # A refresh a test left running must not land in the next test.
    chat_policy.wait_for_chat_policy_refresh(5)
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


async def _policy_after_refresh():
    """What chat follows once the refresh the first call starts has landed."""
    await aget_chat_policy()
    assert chat_policy.wait_for_chat_policy_refresh(5)
    return await aget_chat_policy()


def _counting_reads():
    return patch.object(
        chat_policy,
        "_read_newest_isolated",
        side_effect=chat_policy._read_newest_isolated,
    )


class _Clock:
    """A stand-in for the time module in chat_policy, so a test can move the
    cache's clock on without waiting."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class _StuckRead:
    """Stands in for the row read and blocks until released, like a query
    waiting on a lock or a dead connection."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.entered.set()
        self.release.wait(10)
        return None


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_with_the_switch_off_the_default_is_used_without_a_read():
    await _store()
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=False),
        _counting_reads() as read,
    ):
        assert await aget_chat_policy() is DEFAULT_POLICY
        assert chat_policy.wait_for_chat_policy_refresh(5)
    read.assert_not_called()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_fresh_row_is_followed_once_the_background_read_lands():
    row = await _store()
    with override_settings(FHI_CHAT_POLICY_APPLY=True):
        # The first call does not wait for the read: it routes by the
        # default and starts the read in the background.
        assert await aget_chat_policy() is DEFAULT_POLICY
        assert chat_policy.wait_for_chat_policy_refresh(5)
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
        policy = await _policy_after_refresh()
    assert policy.row_id == newest.pk


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_an_empty_table_gives_the_default():
    with override_settings(FHI_CHAT_POLICY_APPLY=True):
        assert await _policy_after_refresh() is DEFAULT_POLICY


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_stale_row_gives_the_default():
    await _store(minutes_ago=61)
    with override_settings(
        FHI_CHAT_POLICY_APPLY=True, FHI_CHAT_POLICY_MAX_AGE_MINUTES=60
    ):
        assert await _policy_after_refresh() is DEFAULT_POLICY


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_cached_row_is_still_checked_for_age_on_every_call():
    await _store(minutes_ago=30)
    with override_settings(FHI_CHAT_POLICY_APPLY=True):
        with override_settings(FHI_CHAT_POLICY_MAX_AGE_MINUTES=60):
            assert (await _policy_after_refresh()).row_id is not None
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
        {"outside_order": {"deepseek": 1}},
        {"order_scores": {"deepseek": "lots"}},
        {"order_scores": {"deepseek": [1.5, 10]}},
        {"order_scores": {"deepseek": [0.5, -1]}},
    ],
)
@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_row_that_does_not_parse_gives_the_default(fields):
    await _store(**fields)
    with override_settings(FHI_CHAT_POLICY_APPLY=True):
        assert await _policy_after_refresh() is DEFAULT_POLICY


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_database_error_gives_the_default():
    await _store()
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(
            chat_policy, "newest_policy_row", side_effect=RuntimeError("db down")
        ),
    ):
        assert await _policy_after_refresh() is DEFAULT_POLICY


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_read_bounds_its_transaction_and_closes_its_own_connection(
    monkeypatch,
):
    row = await _store()
    seen = {}

    def bound(connection, ms):
        seen["in_transaction"] = connection.in_atomic_block
        seen["ms"] = ms

    closed_on = []
    monkeypatch.setattr(chat_policy, "_bound_statements", bound)
    monkeypatch.setattr(
        connections, "close_all", lambda: closed_on.append(threading.get_ident())
    )
    with override_settings(FHI_CHAT_POLICY_APPLY=True):
        policy = await _policy_after_refresh()

    # The timeout is set inside the read's transaction, and the connection
    # the read opened is closed afterwards, on the read's own thread.
    assert policy.row_id == row.pk
    assert seen == {"in_transaction": True, "ms": 500}
    assert len(closed_on) == 1
    assert closed_on[0] != threading.get_ident()


def test_the_statement_timeout_applies_on_postgresql_only():
    executed = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            executed.append((sql, params))

    class _Connection:
        def __init__(self, vendor):
            self.vendor = vendor

        def cursor(self):
            return _Cursor()

    chat_policy._bound_statements(_Connection("sqlite"), 500)
    assert executed == []
    chat_policy._bound_statements(_Connection("postgresql"), 500)
    assert executed == [("SELECT set_config('statement_timeout', %s, true)", ["500"])]


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_stuck_read_holds_up_neither_the_turn_nor_the_chats_executor():
    """A policy read that hangs (a lock, a dead connection) must not make a
    turn wait, and must not sit on the chat's thread-sensitive executor,
    where the turn's next ORM calls (the OngoingChat load, the ChatTurn
    write) would queue behind it."""
    chat = await sync_to_async(OngoingChat.objects.create)(
        chat_history=[], summary_for_next_call=[]
    )
    stuck = _StuckRead()
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(chat_policy, "newest_policy_row", side_effect=stuck),
    ):
        # The socket's own executor, as PerConnectionThreadSensitiveMixin
        # sets up.
        async with ThreadSensitiveContext():
            try:
                started = time.monotonic()
                policy = await asyncio.wait_for(aget_chat_policy(), 5)
                waited = time.monotonic() - started
                assert await asyncio.to_thread(stuck.entered.wait, 5)
                # The turn's next ORM calls on the chat's executor run
                # straight away.
                loaded = await asyncio.wait_for(OngoingChat.objects.aget(pk=chat.pk), 2)
                turns = await asyncio.wait_for(
                    ChatTurn.objects.filter(chat=chat).acount(), 2
                )
                # A turn that comes along while the read is stuck does not
                # wait either.
                again = await asyncio.wait_for(aget_chat_policy(), 1)
                still_stuck = not stuck.release.is_set()
            finally:
                stuck.release.set()

    assert policy is DEFAULT_POLICY and again is DEFAULT_POLICY
    assert waited < 0.5
    assert (loaded.pk, turns) == (chat.pk, 0)
    assert still_stuck
    assert stuck.calls == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_concurrent_turns_start_one_read_and_use_the_cached_policy():
    good = await _store()
    clock = _Clock()
    stuck = _StuckRead()
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(chat_policy, "time", clock),
    ):
        assert (await _policy_after_refresh()).row_id == good.pk
        clock.now += chat_policy.POLICY_CACHE_SECONDS
        with patch.object(chat_policy, "newest_policy_row", side_effect=stuck):
            try:
                policies = await asyncio.gather(
                    *(aget_chat_policy() for _ in range(20))
                )
                assert await asyncio.to_thread(stuck.entered.wait, 5)
                policies += await asyncio.gather(
                    *(aget_chat_policy() for _ in range(20))
                )
            finally:
                stuck.release.set()
            assert chat_policy.wait_for_chat_policy_refresh(5)

    # One read for all forty turns, and every one of them had the cached
    # policy while it ran.
    assert stuck.calls == 1
    assert {p.row_id for p in policies} == {good.pk}


@pytest.mark.parametrize(
    "error", [RuntimeError("db down"), OperationalError("statement timeout")]
)
@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_good_cached_policy(error):
    good = await _store()
    clock = _Clock()
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(chat_policy, "time", clock),
    ):
        assert (await _policy_after_refresh()).row_id == good.pk
        clock.now += chat_policy.POLICY_CACHE_SECONDS
        with patch.object(chat_policy, "newest_policy_row", side_effect=error) as read:
            kept = await _policy_after_refresh()
            assert read.call_count == 1
            # Nor is the failing read tried again on the next turn.
            assert (await aget_chat_policy()).row_id == good.pk
            assert chat_policy.wait_for_chat_policy_refresh(5)
            assert read.call_count == 1
    assert kept.row_id == good.pk


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_refresh_that_finds_a_bad_newest_row_stops_following_the_cached_one():
    good = await _store(minutes_ago=5)
    clock = _Clock()
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(chat_policy, "time", clock),
    ):
        assert (await _policy_after_refresh()).row_id == good.pk
        await _store(schema_version=2)
        clock.now += chat_policy.POLICY_CACHE_SECONDS
        assert await _policy_after_refresh() is DEFAULT_POLICY


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_the_row_is_read_once_per_cache_period():
    await _store()
    clock = _Clock()
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(chat_policy, "time", clock),
        _counting_reads() as read,
    ):
        first = await _policy_after_refresh()
        clock.now += chat_policy.POLICY_CACHE_SECONDS - 1
        second = await aget_chat_policy()
        assert chat_policy.wait_for_chat_policy_refresh(5)
        assert first == second
        assert read.call_count == 1
        clock.now += 1
        await _policy_after_refresh()
        assert read.call_count == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_failed_read_is_not_retried_on_every_turn():
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(
            chat_policy, "newest_policy_row", side_effect=RuntimeError("db down")
        ) as read,
    ):
        assert await _policy_after_refresh() is DEFAULT_POLICY
        assert await aget_chat_policy() is DEFAULT_POLICY
        assert chat_policy.wait_for_chat_policy_refresh(5)
    assert read.call_count == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_read_started_before_a_reset_cannot_write_its_result():
    await _store()
    stuck = _StuckRead()
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(chat_policy, "_read_newest_isolated", side_effect=stuck),
    ):
        await aget_chat_policy()
        assert await asyncio.to_thread(stuck.entered.wait, 5)
        old = chat_policy._policy_cache.reset()
        stuck.release.set()
        old.join(5)
        assert not old.is_alive()
        # Had the old read written its result, the cache would count as
        # read and this call would start no read of its own.
        await aget_chat_policy()
        assert chat_policy.wait_for_chat_policy_refresh(5)
    assert stuck.calls == 2


class _StuckReads:
    """Stands in for the row read. The first ``stuck`` reads block until
    released, like reads on a connection that stopped answering; the ones
    after them read the row. Build it before patching the reader."""

    def __init__(self, stuck):
        self.stuck = stuck
        self.read = chat_policy._read_newest_isolated
        self.release = threading.Event()
        self.first_entered = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
            call = self.calls
        if call <= self.stuck:
            self.first_entered.set()
            self.release.wait(10)
            return None
        return self.read()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_read_stuck_past_the_limit_is_given_up_and_a_new_one_lands():
    row = await _store()
    clock = _Clock()
    reads = _StuckReads(stuck=1)
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(chat_policy, "time", clock),
        patch.object(chat_policy, "_read_newest_isolated", side_effect=reads),
    ):
        assert await aget_chat_policy() is DEFAULT_POLICY
        assert await asyncio.to_thread(reads.first_entered.wait, 5)
        stuck = chat_policy._policy_cache._thread
        clock.now += chat_policy.POLICY_READ_STUCK_SECONDS - 1
        assert await aget_chat_policy() is DEFAULT_POLICY
        assert reads.calls == 1
        clock.now += 1
        await aget_chat_policy()
        assert chat_policy.wait_for_chat_policy_refresh(5)
        assert reads.calls == 2
        assert (await aget_chat_policy()).row_id == row.pk
        # The given-up read answers late (with no row); the cache keeps
        # the newer read's value.
        reads.release.set()
        stuck.join(5)
        assert not stuck.is_alive()
        assert (await aget_chat_policy()).row_id == row.pk


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_no_more_than_max_stuck_reads_are_left_waiting():
    await _store()
    clock = _Clock()
    reads = _StuckReads(stuck=100)
    started = []
    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(chat_policy, "time", clock),
        patch.object(chat_policy, "_read_newest_isolated", side_effect=reads),
    ):
        for _ in range(chat_policy.MAX_STUCK_READS + 3):
            assert await aget_chat_policy() is DEFAULT_POLICY
            thread = chat_policy._policy_cache._thread
            if thread not in started:
                started.append(thread)
            clock.now += chat_policy.POLICY_READ_STUCK_SECONDS
        # The first read plus one new read for each given up, and no more.
        assert len(started) == chat_policy.MAX_STUCK_READS + 1
        reads.release.set()
        for thread in started:
            thread.join(5)
            assert not thread.is_alive()
        # Once they have ended, reads start again when the next is due.
        clock.now += chat_policy.POLICY_CACHE_SECONDS
        await aget_chat_policy()
        assert chat_policy.wait_for_chat_policy_refresh(5)
        assert reads.calls == len(started) + 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_read_that_cannot_start_gives_the_default_and_waits_its_turn():
    await _store()

    class _Unstartable(threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    with (
        override_settings(FHI_CHAT_POLICY_APPLY=True),
        patch.object(
            chat_policy, "threading", types.SimpleNamespace(Thread=_Unstartable)
        ),
        _counting_reads() as read,
    ):
        assert await aget_chat_policy() is DEFAULT_POLICY
        # Not stuck "reading" forever: nothing is running, and the next
        # read waits for the cache time like any failed one.
        assert chat_policy._policy_cache._thread is None
        assert await aget_chat_policy() is DEFAULT_POLICY
    read.assert_not_called()
    assert chat_policy._policy_cache._refresh_due > time.monotonic()


def test_policy_from_row_reads_every_field():
    row = ChatRoutingPolicy(
        pk=3,
        source="manual",
        window_minutes=60,
        turns_considered=80,
        external_excluded=["deepseek"],
        external_delay_seconds=5.5,
        outside_order=["claude-opus", "deepseek"],
        order_scores={"claude-opus": [0.05, 200]},
        internal_usable_rate=0.9,
        internal_ttu_p75_ms=5500,
        reason="ok",
        created_at=timezone.now(),
    )
    policy = policy_from_row(row)
    assert policy is not None
    assert policy.row_id == 3
    assert policy.outside_order == ("claude-opus", "deepseek")
    assert policy.order_scores == {"claude-opus": (0.05, 200)}
    assert policy.internal_ttu_p75_ms == 5500
    assert not policy.narrows_nothing

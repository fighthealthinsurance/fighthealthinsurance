"""ml/spend.py: monthly budgets for paid providers, counted as calls happen.

TypeSafe's month is shared, with a reserve kept for letters that chat may
never spend. Chat's shares (TypeSafe and DeepInfra) are spread by day. A
provider that refuses for credit or quota is paused for every use for the
rest of the UTC day. The rules read memory only; these tests load the ledger
directly.
"""

import datetime
from unittest.mock import patch

import pytest
from django.test import override_settings

from fighthealthinsurance.ml import spend, typesafe

# The tenth of a 30-day month: 21 days left, today included.
TODAY = datetime.date(2026, 9, 10)
EARLIER = datetime.date(2026, 9, 3)
M = spend.MICRO


@pytest.fixture(autouse=True)
def _pinned_today():
    with patch.object(spend, "_today", return_value=TODAY):
        yield


def _load(**by_day):
    """_load(**{"typesafe:chat": {TODAY: 1000}}) sets the ledger."""
    rows = {}
    for name, days in by_day.items():
        for day, amount in days.items():
            rows[(name, day)] = amount
    spend._ledger.load_for_tests(rows)


def _k(provider, use):
    return spend.counter(provider, use)


class TestCosts:
    def test_typesafe_bills_input_tokens_at_the_listed_rate(self):
        assert spend.typesafe_cost_micro(1_000_000) == 42_000
        assert spend.typesafe_cost_micro(800) == 34
        for bad in (None, "x", -5):
            assert spend.typesafe_cost_micro(bad) == 0

    def test_deepinfra_prefers_the_answers_own_estimate(self):
        usage = {"prompt_tokens": 1000, "completion_tokens": 100, "estimated_cost": 0.0003}
        assert spend.deepinfra_cost_micro("zai-org/GLM-5.3-Flash", usage) == 300

    def test_deepinfra_falls_back_to_list_price(self):
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}
        # Mistral-Small: $0.075 in and $0.2 out per million.
        assert spend.deepinfra_cost_micro(
            "mistralai/Mistral-Small-3.2-24B-Instruct-2506", usage
        ) == 275_000

    @pytest.mark.parametrize(
        "estimated", [float("inf"), float("nan"), 1e308, -0.5, True, "0.1"]
    )
    def test_an_estimate_we_cannot_trust_falls_back_to_list_price(self, estimated):
        usage = {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 1_000_000,
            "estimated_cost": estimated,
        }
        assert spend.deepinfra_cost_micro(
            "mistralai/Mistral-Small-3.2-24B-Instruct-2506", usage
        ) == 275_000

    def test_an_unpriced_model_is_charged_high(self):
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
        assert spend.deepinfra_cost_micro("someone/new-model", usage) == 3 * M

    def test_no_usage_costs_nothing(self):
        assert spend.deepinfra_cost_micro("x", None) == 0


class TestTypeSafeBudget:
    def test_letters_may_use_the_whole_month(self):
        _load(**{_k("typesafe", "letters"): {EARLIER: int(4.99 * M)}})
        assert spend.allows(spend.TYPESAFE, spend.LETTERS)
        _load(**{_k("typesafe", "letters"): {EARLIER: 5 * M}})
        assert not spend.allows(spend.TYPESAFE, spend.LETTERS)
        assert not spend.allows(spend.TYPESAFE, spend.TRIAGE)

    def test_research_spend_counts_toward_the_month(self):
        _load(**{_k("typesafe", "research"): {EARLIER: 5 * M}})
        assert not spend.allows(spend.TYPESAFE, spend.RESEARCH)
        assert not spend.allows(spend.TYPESAFE, spend.LETTERS)
        _load(**{_k("typesafe", "research"): {EARLIER: int(4.99 * M)}})
        assert spend.allows(spend.TYPESAFE, spend.RESEARCH)

    def test_chat_never_spends_the_letters_reserve(self):
        # Chat has spent little, but letters and chat together reached $3.
        _load(
            **{
                _k("typesafe", "letters"): {EARLIER: int(2.9 * M)},
                _k("typesafe", "chat"): {EARLIER: int(0.1 * M)},
            }
        )
        assert not spend.allows(spend.TYPESAFE, spend.CHAT)
        # Letters still may.
        assert spend.allows(spend.TYPESAFE, spend.LETTERS)

    def test_chat_stops_at_its_monthly_share(self):
        _load(**{_k("typesafe", "chat"): {EARLIER: 3 * M}})
        assert not spend.allows(spend.TYPESAFE, spend.CHAT)

    def test_chat_spreads_its_month_by_day(self):
        # $3 left this morning over 21 days: about $0.142857 today.
        _load(**{_k("typesafe", "chat"): {TODAY: 142_000}})
        assert spend.allows(spend.TYPESAFE, spend.CHAT)
        _load(**{_k("typesafe", "chat"): {TODAY: 143_000}})
        assert not spend.allows(spend.TYPESAFE, spend.CHAT)

    def test_the_budgets_come_from_settings(self):
        _load(**{_k("typesafe", "letters"): {EARLIER: 6 * M}})
        with override_settings(FHI_SPEND_TYPESAFE_MONTHLY_USD=10.0):
            assert spend.allows(spend.TYPESAFE, spend.LETTERS)

    def test_an_unread_ledger_refuses_typesafe_but_not_outside_models(self):
        with override_settings(FHI_SPEND_BACKGROUND=True), patch.object(
            spend._Ledger, "_ensure_worker"
        ):
            spend._ledger.reset_for_tests()
            assert not spend.allows(spend.TYPESAFE, spend.LETTERS)
            assert spend.allows(spend.DEEPINFRA, spend.CHAT)

    def test_an_unread_ledger_holds_back_no_unbudgeted_provider(self):
        """Anthropic, Claude on Azure and Perplexity have no budget to
        judge, so an assistant appeal is not kept from them while the
        ledger is unread, as it is from DeepInfra's capped share."""
        with override_settings(FHI_SPEND_BACKGROUND=True), patch.object(
            spend._Ledger, "_ensure_worker"
        ):
            spend._ledger.reset_for_tests()
            assert not spend.allows(spend.DEEPINFRA, spend.ASSISTANT)
            for provider in spend.UNBUDGETED:
                assert spend.allows(provider, spend.ASSISTANT), provider

    def test_a_copy_that_stopped_refreshing_counts_as_unread(self):
        """Another pod may have spent the month or paused TypeSafe: a copy
        the worker has not refreshed for STALE_SECONDS refuses TypeSafe and
        lets outside models through, as an unread one does."""
        with override_settings(FHI_SPEND_BACKGROUND=True), patch.object(
            spend._Ledger, "_ensure_worker"
        ):
            _load()
            assert spend.allows(spend.TYPESAFE, spend.LETTERS)
            spend._ledger._refreshed_at -= spend.STALE_SECONDS + 1
            assert not spend.allows(spend.TYPESAFE, spend.LETTERS)
            assert spend.allows(spend.DEEPINFRA, spend.CHAT)

    def test_a_copy_that_stopped_refreshing_still_holds_deepinfra_chat_to_its_share(
        self,
    ):
        """Stale is not unread: the counts it last read (and this process's
        own) are a floor on the real spend, so a spent share stays spent."""
        with override_settings(FHI_SPEND_BACKGROUND=True), patch.object(
            spend._Ledger, "_ensure_worker"
        ):
            _load(**{_k("deepinfra", "chat"): {TODAY: 953_000}})
            spend._ledger._refreshed_at -= spend.STALE_SECONDS + 1
            assert not spend.allows(spend.DEEPINFRA, spend.CHAT)

    def test_a_stale_copy_from_last_month_does_not_hold_back_deepinfra_chat(self):
        """At the month's turn a stale copy holds the old month's counts,
        which say nothing about this month's budget."""
        with override_settings(FHI_SPEND_BACKGROUND=True), patch.object(
            spend._Ledger, "_ensure_worker"
        ):
            _load(**{_k("deepinfra", "chat"): {TODAY: 25 * M}})
            spend._ledger._refreshed_at -= spend.STALE_SECONDS + 1
            with patch.object(
                spend, "_today", return_value=datetime.date(2026, 10, 1)
            ):
                assert spend.allows(spend.DEEPINFRA, spend.CHAT)

    def test_what_this_process_records_counts_at_once(self):
        _load(**{_k("typesafe", "chat"): {TODAY: 140_000}})
        assert spend.allows(spend.TYPESAFE, spend.CHAT)
        spend.record(spend.TYPESAFE, spend.CHAT, 5_000)
        assert not spend.allows(spend.TYPESAFE, spend.CHAT)


class TestOutsideBudgets:
    def test_deepinfra_chat_spreads_twenty_dollars_by_day(self):
        # $20 over 21 days: about $0.952 today.
        _load(**{_k("deepinfra", "chat"): {TODAY: 950_000}})
        assert spend.allows(spend.DEEPINFRA, spend.CHAT)
        _load(**{_k("deepinfra", "chat"): {TODAY: 953_000}})
        assert not spend.allows(spend.DEEPINFRA, spend.CHAT)

    def test_deepinfra_outside_chat_is_counted_not_capped(self):
        _load(**{_k("deepinfra", "other"): {EARLIER: 500 * M}})
        assert spend.allows(spend.DEEPINFRA, spend.OTHER)
        assert spend.allows(spend.DEEPINFRA, spend.CHAT)

    def test_azure_has_no_cap_unless_one_is_set(self):
        _load(**{_k("azure", "chat"): {TODAY: 5000}})
        assert spend.allows(spend.AZURE, spend.CHAT)
        with override_settings(FHI_SPEND_AZURE_CHAT_DAILY_CALLS=5000):
            assert not spend.allows(spend.AZURE, spend.CHAT)
        with override_settings(FHI_SPEND_AZURE_CHAT_DAILY_CALLS=5001):
            assert spend.allows(spend.AZURE, spend.CHAT)


class TestPauses:
    def test_a_pause_for_one_use_leaves_the_others(self):
        _load()
        spend.pause(spend.DEEPINFRA, spend.CHAT)
        assert not spend.allows(spend.DEEPINFRA, spend.CHAT)
        assert spend.allows(spend.DEEPINFRA, spend.OTHER)

    def test_pausing_every_use(self):
        _load()
        spend.pause(spend.TYPESAFE)
        for use in (spend.CHAT, spend.LETTERS, spend.TRIAGE):
            assert not spend.allows(spend.TYPESAFE, use)

    def test_another_pods_pause_is_read_from_the_ledger(self):
        _load(**{spend.counter(spend.PAUSED, "typesafe:*"): {TODAY: 1}})
        assert not spend.allows(spend.TYPESAFE, spend.LETTERS)

    def test_yesterdays_pause_is_over(self):
        _load(**{spend.counter(spend.PAUSED, "typesafe:*"): {EARLIER: 1}})
        assert spend.allows(spend.TYPESAFE, spend.LETTERS)

    def test_active_pauses_names_todays_pauses_from_here_and_other_pods(self):
        _load(
            **{
                spend.counter(spend.PAUSED, "typesafe:*"): {TODAY: 1},
                spend.counter(spend.PAUSED, "azure:chat"): {EARLIER: 1},
            }
        )
        spend.pause(spend.DEEPINFRA)
        assert spend.active_pauses() == ["deepinfra:*", "typesafe:*"]

    @pytest.mark.parametrize(
        "status,body,refused",
        [
            (402, "", True),
            (429, '{"error": {"code": "insufficient_quota"}}', True),
            (429, "Your credit balance is too low", True),
            (429, "Too many requests, slow down", False),
            # Azure OpenAI's ordinary per-minute limit links to a quota page.
            (
                429,
                '{"error": {"code": "429", "message": "Requests to the '
                "ChatCompletions_Create Operation under Azure OpenAI API have "
                "exceeded token rate limit of your current OpenAI S0 pricing "
                "tier. Please retry after 6 seconds. Please go here: "
                'https://aka.ms/oai/quotaincrease if you would like to further '
                'increase the default rate limit."}}',
                False,
            ),
            (429, "You exceeded your current quota, please check your plan", True),
            (429, "Your balance is updated nightly; retry in 5s", False),
            (500, "quota", False),
            # Anthropic answers an empty balance with a 400, Perplexity with a
            # 401 insufficient_quota.
            (400, "Your credit balance is too low to access the Anthropic API", True),
            (401, '{"error":{"type":"insufficient_quota"}}', True),
            (403, '{"error": {"code": "insufficient_quota"}}', True),
            # Generic wording on a 400/401/403 may be the request quoted
            # back (a denial says "insufficient funds"); only 402/429 read it.
            (403, "insufficient balance", False),
            (400, "Denied: insufficient funds in the HSA", False),
            (429, "insufficient balance", True),
            (400, "You have reached your specified API usage limits", True),
            (400, "Bad request", False),
            (401, "Invalid API key", False),
        ],
    )
    def test_what_counts_as_a_quota_refusal(self, status, body, refused):
        assert spend.quota_refusal(status, body) is refused


class TestUnpause:
    """unpause lifts a credit pause before the UTC day ends: here at once,
    and on other pods once the worker stores its count back at 0."""

    def test_unpause_says_it_lifted_a_pause_made_here(self):
        _load()
        spend.pause(spend.DEEPINFRA)
        assert spend.unpause(spend.DEEPINFRA) is True

    def test_a_provider_paused_here_is_asked_again_after_unpause(self):
        _load()
        spend.pause(spend.DEEPINFRA)
        spend.unpause(spend.DEEPINFRA)
        assert spend.allows(spend.DEEPINFRA, spend.CHAT)

    def test_unpause_lifts_another_pods_pause_read_from_the_ledger(self):
        _load(**{spend.counter(spend.PAUSED, "anthropic:*"): {TODAY: 1}})
        spend.unpause(spend.ANTHROPIC)
        assert spend.allows(spend.ANTHROPIC, spend.CHAT)

    @pytest.mark.parametrize(
        "rows",
        [{}, {spend.counter(spend.PAUSED, "anthropic:*"): {EARLIER: 1}}],
        ids=["never-paused", "paused-on-an-earlier-day"],
    )
    def test_unpause_of_a_provider_not_paused_today_returns_false(self, rows):
        _load(**rows)
        assert spend.unpause(spend.ANTHROPIC) is False

    def test_unpause_lifts_only_the_use_it_names(self):
        _load()
        spend.pause(spend.DEEPINFRA, spend.CHAT)
        spend.unpause(spend.DEEPINFRA)
        assert not spend.allows(spend.DEEPINFRA, spend.CHAT)

    def test_pausing_again_after_an_unpause_pauses(self):
        _load()
        spend.pause(spend.DEEPINFRA)
        spend.unpause(spend.DEEPINFRA)
        spend.pause(spend.DEEPINFRA)
        assert not spend.allows(spend.DEEPINFRA, spend.CHAT)

    def test_active_pauses_no_longer_lists_a_lifted_pause(self):
        _load(**{spend.counter(spend.PAUSED, "typesafe:*"): {TODAY: 1}})
        spend.pause(spend.DEEPINFRA)
        spend.unpause(spend.TYPESAFE)
        spend.unpause(spend.DEEPINFRA)
        assert spend.active_pauses() == []

    def test_unpause_queues_taking_back_the_pause_count_it_read(self):
        # Not the whole row: a pause stored after this read is not its to take.
        _load(**{spend.counter(spend.PAUSED, "anthropic:*"): {TODAY: 2}})
        spend.unpause(spend.ANTHROPIC)
        assert spend._ledger._clears == {("paused:anthropic:*", TODAY): 2}

    def test_unpause_of_a_pause_never_stored_queues_nothing_to_take_back(self):
        _load()
        spend.pause(spend.ANTHROPIC)
        spend.unpause(spend.ANTHROPIC)
        assert (spend._ledger._clears, spend._ledger._pending) == ({}, {})

    def test_a_pause_after_an_unpause_is_stored_on_top_of_the_queued_lift(self):
        # The lift takes back only the count before it; the later pause's
        # count is still stored, so the shared count stays set.
        key = ("paused:anthropic:*", TODAY)
        _load(**{spend.counter(spend.PAUSED, "anthropic:*"): {TODAY: 1}})
        spend.unpause(spend.ANTHROPIC)
        spend.pause(spend.ANTHROPIC)
        assert (spend._ledger._clears, spend._ledger._pending) == ({key: 1}, {key: 1})

    def test_unpause_logs_one_warning_with_its_reason(self, log_capture):
        _load()
        spend.pause(spend.ANTHROPIC)
        with log_capture() as cap:
            spend.unpause(spend.ANTHROPIC, reason="it answered again")
        assert cap.messages("WARNING") == [
            "Lifted the pause on anthropic:* before the UTC day ended "
            "(it answered again)"
        ]

    def test_unpause_never_raises(self):
        _load()
        with patch.object(spend._ledger, "unpause", side_effect=RuntimeError("broken")):
            assert spend.unpause(spend.ANTHROPIC) is False


class TestTypeSafeRequests:
    """ml/typesafe.ask applies the budget before sending and counts after."""

    SETTINGS = dict(
        TYPESAFE_API_KEY="test-key",
        TYPESAFE_API_URL="https://typesafe.invalid/v1/systemone",
    )

    @pytest.fixture(autouse=True)
    def _no_cooldown(self):
        # A 401 also starts typesafe's process-wide cooldown.
        typesafe.reset_cooldown_for_tests()
        yield
        typesafe.reset_cooldown_for_tests()

    class _Response:
        def __init__(self, status, payload, body=""):
            self.status = status
            self.payload = payload
            self.body = body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def json(self):
            return self.payload

        async def text(self, **kwargs):
            return self.body

    class _Session:
        def __init__(self, status=200, payload=None, body=""):
            self.status = status
            self.payload = payload or {"answers": {}, "usage": {"input_tokens": 10_000}}
            self.body = body
            self.posts = 0

        def __call__(self, *args, **kwargs):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, *args, **kwargs):
            self.posts += 1
            return TestTypeSafeRequests._Response(self.status, self.payload, self.body)

    @pytest.mark.asyncio
    async def test_a_spent_budget_is_refused_before_anything_is_sent(self):
        _load(**{_k("typesafe", "chat"): {EARLIER: 3 * M}})
        session = self._Session()
        with override_settings(**self.SETTINGS), patch.object(
            typesafe.aiohttp, "ClientSession", session
        ):
            with pytest.raises(typesafe.TypeSafeBudgetSpent):
                await typesafe.ask("state", {}, timeout_seconds=1, use=spend.CHAT)
        assert session.posts == 0

    @pytest.mark.asyncio
    async def test_the_reported_input_tokens_are_counted_against_the_use(self):
        _load()
        session = self._Session()
        with override_settings(**self.SETTINGS), patch.object(
            typesafe.aiohttp, "ClientSession", session
        ):
            await typesafe.ask("state", {}, timeout_seconds=1, use=spend.LETTERS)
        view = spend._ledger.snapshot()
        assert view.day_total(_k("typesafe", "letters"), TODAY) == 420
        assert view.day_total(_k("typesafe", "chat"), TODAY) == 0

    @pytest.mark.asyncio
    async def test_a_402_pauses_typesafe_for_every_use(self):
        _load()
        with override_settings(**self.SETTINGS), patch.object(
            typesafe.aiohttp, "ClientSession", self._Session(status=402)
        ):
            with pytest.raises(typesafe.TypeSafeError):
                await typesafe.ask("state", {}, timeout_seconds=1, use=spend.LETTERS)
        assert not spend.allows(spend.TYPESAFE, spend.LETTERS)
        assert not spend.allows(spend.TYPESAFE, spend.CHAT)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status,body",
        [
            (400, "Your credit balance is too low"),
            (401, '{"error": {"type": "insufficient_quota"}}'),
            (429, '{"error": {"code": "insufficient_quota"}}'),
        ],
    )
    async def test_a_quota_refusal_under_another_status_pauses_typesafe_for_every_use(
        self, status, body
    ):
        _load()
        with override_settings(**self.SETTINGS), patch.object(
            typesafe.aiohttp, "ClientSession", self._Session(status=status, body=body)
        ):
            with pytest.raises(typesafe.TypeSafeError):
                await typesafe.ask("state", {}, timeout_seconds=1, use=spend.CHAT)
        assert spend.paused(spend.TYPESAFE, "*")

    @pytest.mark.asyncio
    async def test_a_passing_rate_limit_pauses_nothing(self):
        _load()
        with override_settings(**self.SETTINGS), patch.object(
            typesafe.aiohttp,
            "ClientSession",
            self._Session(status=429, body="Too many requests, slow down"),
        ):
            with pytest.raises(typesafe.TypeSafeError):
                await typesafe.ask("state", {}, timeout_seconds=1, use=spend.CHAT)
        assert spend.allows(spend.TYPESAFE, spend.LETTERS)

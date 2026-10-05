"""ml/spend.py: appeals that come through an AI assistant spend under their
own use, with their own budgets, and the channel reaches the model threads.
Reservations touch the database and are tested in tests/sync.
"""

import datetime
from unittest.mock import patch

import pytest
from django.test import override_settings

from fighthealthinsurance import exec as fhi_exec
from fighthealthinsurance.ml import spend
from fighthealthinsurance.ml.ml_metrics import ml_call_purpose

TODAY = datetime.date(2026, 9, 10)
EARLIER = datetime.date(2026, 9, 3)
M = spend.MICRO


@pytest.fixture(autouse=True)
def _pinned_today():
    with patch.object(spend, "_today", return_value=TODAY):
        yield


def _load(**by_day):
    rows = {}
    for name, days in by_day.items():
        for day, amount in days.items():
            rows[(name, day)] = amount
    spend._ledger.load_for_tests(rows)


class _Denial:
    denial_text = "letter"

    def __init__(self, channel):
        self.channel = channel


class TestTheChannel:
    def test_site_work_spends_as_other_and_assistant_work_as_assistant(self):
        assert spend.current_use() == spend.OTHER
        with spend.for_channel(spend.CHANNEL_ASSISTANT):
            assert spend.current_use() == spend.ASSISTANT
        assert spend.current_use() == spend.OTHER

    def test_chat_stays_chat_whatever_the_channel(self):
        with spend.for_channel(spend.CHANNEL_ASSISTANT), ml_call_purpose("chat"):
            assert spend.current_use() == spend.CHAT

    def test_the_channel_comes_from_the_denial_and_unknown_values_mean_site(self):
        assert spend.channel_of(_Denial("assistant")) == spend.CHANNEL_ASSISTANT
        assert spend.channel_of(_Denial("site")) == spend.CHANNEL_SITE
        assert spend.channel_of(_Denial("anything")) == spend.CHANNEL_SITE
        assert spend.channel_of(object()) == spend.CHANNEL_SITE

    def test_a_mark_set_inside_a_scope_is_undone_at_its_end(self):
        with spend.channel_scope():
            spend.set_channel_of(_Denial("assistant"))
            assert spend.current_use() == spend.ASSISTANT
        assert spend.current_use() == spend.OTHER

    def test_typesafe_letters_and_triage_become_assistant_for_assistant_work(self):
        assert spend.typesafe_use(spend.LETTERS) == spend.LETTERS
        with spend.for_channel(spend.CHANNEL_ASSISTANT):
            assert spend.typesafe_use(spend.LETTERS) == spend.ASSISTANT
            assert spend.typesafe_use(spend.TRIAGE) == spend.ASSISTANT

    def test_the_channel_survives_a_hop_through_every_executor(self):
        pools = (
            fhi_exec.executor,
            fhi_exec.background_executor,
            fhi_exec.bridge_executor,
            fhi_exec.cleaner_executor,
        )
        with spend.for_channel(spend.CHANNEL_ASSISTANT):
            seen = [pool.submit(spend.current_use).result(timeout=10) for pool in pools]
        assert seen == [spend.ASSISTANT] * len(pools)
        assert (
            fhi_exec.executor.submit(spend.current_use).result(timeout=10)
            == spend.OTHER
        )


class TestTheDecorator:
    @pytest.mark.asyncio
    async def test_a_helper_given_the_denial_spends_for_its_channel(self):
        seen = []

        @spend.for_denial_channel
        async def helper(denial, speculative):
            seen.append(spend.current_use())
            return "done"

        assert await helper(_Denial("assistant"), True) == "done"
        assert await helper(denial=_Denial("site"), speculative=False) == "done"
        assert seen == [spend.ASSISTANT, spend.OTHER]
        assert spend.current_use() == spend.OTHER

    @pytest.mark.asyncio
    async def test_a_helper_called_without_the_denial_is_refused(self):
        @spend.for_denial_channel
        async def helper(cls, denial):
            return "never"

        with pytest.raises(TypeError):
            await helper(object(), None)


class TestAssistantBudgets:
    def test_assistant_typesafe_work_counts_in_the_month_and_never_spends_the_letters_reserve(
        self,
    ):
        # $3.50 spent by assistant appeals: letters still fit in the $5 month,
        # but assistant work has reached the $2 reserve line.
        _load(
            **{spend.counter(spend.TYPESAFE, spend.ASSISTANT): {EARLIER: int(3.5 * M)}}
        )
        assert spend.allows(spend.TYPESAFE, spend.LETTERS)
        assert not spend.allows(spend.TYPESAFE, spend.ASSISTANT)
        assert not spend.allows(spend.TYPESAFE, spend.CHAT)

    def test_deepinfra_assistant_work_defaults_to_five_dollars_a_month(self):
        _load(**{spend.counter(spend.DEEPINFRA, spend.ASSISTANT): {EARLIER: 5 * M}})
        assert not spend.allows(spend.DEEPINFRA, spend.ASSISTANT)
        with override_settings(FHI_SPEND_DEEPINFRA_ASSISTANT_MONTHLY_USD=0.0):
            assert spend.allows(spend.DEEPINFRA, spend.ASSISTANT)

    def test_deepinfra_assistant_work_spreads_its_month_by_day(self):
        # $21 a month, nothing spent before today, 21 days left: $1 today.
        with override_settings(FHI_SPEND_DEEPINFRA_ASSISTANT_MONTHLY_USD=21.0):
            _load(
                **{
                    spend.counter(spend.DEEPINFRA, spend.ASSISTANT): {
                        TODAY: int(0.99 * M)
                    }
                }
            )
            assert spend.allows(spend.DEEPINFRA, spend.ASSISTANT)
            _load(**{spend.counter(spend.DEEPINFRA, spend.ASSISTANT): {TODAY: 1 * M}})
            assert not spend.allows(spend.DEEPINFRA, spend.ASSISTANT)
            assert spend.allows(spend.DEEPINFRA, spend.CHAT)

    def test_an_unread_ledger_refuses_assistant_work_on_every_provider_but_not_chat(
        self,
    ):
        spend._ledger.reset_for_tests()
        with override_settings(FHI_SPEND_BACKGROUND=True):
            for provider in (spend.DEEPINFRA, spend.TYPESAFE, spend.AZURE):
                assert not spend.allows(provider, spend.ASSISTANT), provider
            assert spend.allows(spend.DEEPINFRA, spend.CHAT)
            assert spend.allows(spend.AZURE, spend.CHAT)

    def test_a_failing_check_refuses_assistant_work_and_lets_chat_through(self):
        with patch.object(spend._ledger, "snapshot", side_effect=RuntimeError("boom")):
            assert not spend.allows(spend.DEEPINFRA, spend.ASSISTANT)
            assert not spend.allows(spend.AZURE, spend.ASSISTANT)
            assert spend.allows(spend.DEEPINFRA, spend.CHAT)
            assert not spend.allows(spend.TYPESAFE, spend.LETTERS)

    def test_the_summary_shows_generations_as_counts_and_deepinfra_as_dollars(self):
        _load(
            **{
                spend.counter(spend.FHI, spend.ASSISTANT): {TODAY: 3},
                spend.counter(spend.DEEPINFRA, spend.ASSISTANT): {TODAY: int(0.25 * M)},
            }
        )
        summary = spend.month_summary()
        assert summary["fhi:assistant"] == 3.0
        assert summary["deepinfra:assistant"] == 0.25
        assert spend.is_count("fhi:assistant") and spend.is_count("azure:chat")
        assert not spend.is_count("deepinfra:assistant")

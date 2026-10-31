"""The experiments registry behind the staff Experiments page.

A flag reads ON only when it and everything it needs are set (and the
code's own live check agrees), INERT when it is set but something it needs
is not, OFF when it is unset, and UNKNOWN when its live check raises.
"""

from unittest import mock

import pytest
from cryptography.fernet import Fernet
from django.conf import settings
from django.test import override_settings
from django.utils.module_loading import import_string

from fighthealthinsurance import experiments
from fighthealthinsurance.experiments import EXPERIMENTS, INERT, OFF, ON, UNKNOWN

_WITH_LIVE_CHECK = [e for e in EXPERIMENTS if e.live_check]
# Every setting the registry names, as a flag or as a need.
_KNOWN = sorted({e.setting for e in EXPERIMENTS} | {n for e in EXPERIMENTS for n in e.needs})


def _row(setting):
    rows = experiments.experiment_states()
    return next(r for r in rows if r["experiment"].setting == setting)


def _truthy(name):
    # The key settings are checked for shape, not only presence; a fresh key
    # each call keeps the research key apart from the default one.
    return Fernet.generate_key().decode() if name.endswith("_KEY") else True


def _falsy(name):
    return "" if name.endswith("_KEY") else False


def test_each_setting_is_listed_once():
    settings_named = [e.setting for e in EXPERIMENTS]
    assert len(settings_named) == len(set(settings_named))


@pytest.mark.parametrize("name", _KNOWN, ids=str)
def test_every_named_setting_exists(name):
    assert hasattr(settings, name)


@pytest.mark.parametrize("experiment", _WITH_LIVE_CHECK, ids=lambda e: e.setting)
def test_every_live_check_is_an_importable_callable(experiment):
    assert callable(import_string(experiment.live_check))


@pytest.mark.parametrize("experiment", _WITH_LIVE_CHECK, ids=lambda e: e.setting)
def test_needs_list_everything_the_live_check_requires(experiment):
    """From every known setting off, setting just the flag and its needs is
    enough for the code's own check to say on: a requirement the check
    gained and needs lacks would leave the page saying "not in effect"
    without naming what is missing. Starting from off matters, since the
    test configuration inherits Dev, which turns some MCP flags on."""
    overrides = {name: _falsy(name) for name in _KNOWN}
    overrides.update({name: _truthy(name) for name in experiment.needs})
    overrides[experiment.setting] = True
    with override_settings(**overrides):
        assert _row(experiment.setting)["state"] == ON


@override_settings(TYPESAFE_LETTER_RANKING_ENABLED=False, TYPESAFE_API_KEY="k")
def test_an_unset_flag_reads_off():
    assert _row("TYPESAFE_LETTER_RANKING_ENABLED")["state"] == OFF


@override_settings(TYPESAFE_LETTER_RANKING_ENABLED=True, TYPESAFE_API_KEY=None)
def test_a_flag_without_its_key_reads_inert_and_names_the_key():
    row = _row("TYPESAFE_LETTER_RANKING_ENABLED")
    assert (row["state"], row["missing"]) == (INERT, ["TYPESAFE_API_KEY"])


@override_settings(TYPESAFE_LETTER_RANKING_ENABLED=True, TYPESAFE_API_KEY="k")
def test_a_flag_with_its_key_reads_on():
    assert _row("TYPESAFE_LETTER_RANKING_ENABLED")["state"] == ON


@override_settings(
    TEMPORAL_ENABLED=True,
    TEMPORAL_PLAN_DOCUMENTS_ENABLED=True,
    TEMPORAL_RESEARCH_PAYLOAD_KEY="not-a-fernet-key",
)
def test_a_live_check_saying_off_reads_inert_with_nothing_missing():
    """Every need is set, but the key is malformed, which only
    temporal_client.plan_documents_enabled knows."""
    row = _row("TEMPORAL_PLAN_DOCUMENTS_ENABLED")
    assert (row["state"], row["missing"]) == (INERT, [])


@override_settings(TYPESAFE_LETTER_RANKING_ENABLED=True, TYPESAFE_API_KEY="k")
def test_a_live_check_that_raises_reads_unknown_with_the_exception_name():
    with mock.patch(
        "fighthealthinsurance.ml.letter_quality.enabled",
        side_effect=RuntimeError("boom"),
    ):
        row = _row("TYPESAFE_LETTER_RANKING_ENABLED")
    assert (row["state"], row["error"]) == (UNKNOWN, "RuntimeError")


def test_grouped_keeps_every_row_in_registry_order():
    rows = experiments.experiment_states()
    flattened = [r for g in experiments.grouped(rows) for r in g["rows"]]
    assert [r["experiment"] for r in flattened] == list(EXPERIMENTS)


def test_summary_counts_every_row_once():
    rows = experiments.experiment_states()
    assert sum(experiments.summary(rows).values()) == len(EXPERIMENTS)

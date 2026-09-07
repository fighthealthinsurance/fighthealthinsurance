"""The Stripe reachability probe must never run for a test that is not a Stripe test.

The root conftest used to probe api.stripe.com at import whenever
STRIPE_TEST_SECRET_KEY was in the environment, so running any test from a
shell holding the key made a real HTTPS request before a single test ran.
The decision is now taken lazily by a setup hook, once per process, only when
a test carrying the ``stripe_e2e`` marker is about to run.
"""

import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from tests import conftest as root_conftest


class _Item:
    """The two attributes of a pytest item the hook reads."""

    def __init__(self, marked: bool):
        self._marked = marked

    def get_closest_marker(self, name):
        return object() if self._marked and name == root_conftest.STRIPE_E2E_MARKER else None


@pytest.fixture(autouse=True)
def _fresh_decision():
    root_conftest._stripe_e2e_decided = False
    root_conftest._stripe_e2e_skip_reason = None
    yield
    root_conftest._stripe_e2e_decided = False
    root_conftest._stripe_e2e_skip_reason = None


def test_the_probe_is_never_called_at_import():
    """Every call site of the probe sits inside a function, none at module level."""
    src = Path(root_conftest.__file__).read_text()
    assert "_skip_stripe_ssl = (" not in src, "the import-time probe is back"
    # Call sites only; the definition line itself starts at column 0.
    for m in re.finditer(r"(?<!def )_has_ssl_intercepting_proxy\(\)", src):
        line_start = src.rfind("\n", 0, m.start()) + 1
        assert src[line_start:m.start()].startswith(" "), (
            "the probe is called at module level again"
        )


def test_an_unmarked_test_never_triggers_the_probe():
    with patch.object(root_conftest, "_has_ssl_intercepting_proxy") as probe, patch.dict(
        os.environ, {"STRIPE_TEST_SECRET_KEY": "sk_test_not_real"}
    ):
        root_conftest.pytest_runtest_setup(_Item(marked=False))
        probe.assert_not_called()


def test_a_marked_test_without_a_key_skips_without_probing():
    with patch.object(root_conftest, "_has_ssl_intercepting_proxy") as probe, patch.dict(
        os.environ
    ):
        os.environ.pop("STRIPE_TEST_SECRET_KEY", None)
        with pytest.raises(pytest.skip.Exception, match="not configured"):
            root_conftest.pytest_runtest_setup(_Item(marked=True))
        probe.assert_not_called()


def test_a_marked_test_with_a_key_probes_once_and_caches():
    with patch.object(
        root_conftest, "_has_ssl_intercepting_proxy", return_value=False
    ) as probe, patch.dict(os.environ, {"STRIPE_TEST_SECRET_KEY": "sk_test_not_real"}):
        root_conftest.pytest_runtest_setup(_Item(marked=True))
        root_conftest.pytest_runtest_setup(_Item(marked=True))
        assert probe.call_count == 1


def test_a_blocked_probe_skips_with_the_proxy_reason():
    with patch.object(
        root_conftest, "_has_ssl_intercepting_proxy", return_value=True
    ), patch.dict(os.environ, {"STRIPE_TEST_SECRET_KEY": "sk_test_not_real"}):
        with pytest.raises(pytest.skip.Exception, match="proxy"):
            root_conftest.pytest_runtest_setup(_Item(marked=True))

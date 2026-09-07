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


def test_importing_conftest_with_a_key_makes_no_network_call():
    """Behavioural, not textual: load the conftest source fresh, with a key in
    the environment and every socket connect patched to raise, and require
    the import to complete. A guarded probe at module scope (review's
    counterexample) would connect, raise, and fail the import.
    """
    import importlib.util
    import socket
    import urllib.request

    def _no_network(*args, **kwargs):
        raise AssertionError("network access during conftest import")

    path = Path(root_conftest.__file__)
    with patch.dict(os.environ, {"STRIPE_TEST_SECRET_KEY": "sk_test_not_real"}), patch.object(
        socket, "create_connection", _no_network
    ), patch.object(socket.socket, "connect", _no_network), patch.object(
        urllib.request, "build_opener", _no_network
    ):
        spec = importlib.util.spec_from_file_location("_root_conftest_isolated", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # raises if anything reached the network
    assert callable(module.pytest_runtest_setup)


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

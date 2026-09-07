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

    def _no_network(*args, **kwargs):
        raise AssertionError("network access during conftest import")

    # Only real network operations are guarded. Building an opener constructs
    # handlers and does not connect; a refactor that builds one at module
    # scope must not fail this (review).
    path = Path(root_conftest.__file__)
    with patch.dict(os.environ, {"STRIPE_TEST_SECRET_KEY": "sk_test_not_real"}), patch.object(
        socket, "create_connection", _no_network
    ), patch.object(socket.socket, "connect", _no_network), patch.object(
        socket.socket, "connect_ex", _no_network
    ):
        # Loaded under the `tests` package, as pytest loads it, so a relative
        # import inside the conftest (a helper extracted to tests/x.py)
        # resolves here too (review).
        spec = importlib.util.spec_from_file_location("tests._root_conftest_isolated", path)
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "tests"
        # Registered while it executes, as a real import would be: dataclass
        # processing under `from __future__ import annotations` looks the
        # module up in sys.modules (review).
        import sys

        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)  # raises if anything reached the network
        finally:
            sys.modules.pop(spec.name, None)
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


REPO_ROOT = Path(root_conftest.__file__).resolve().parents[1]
_INNER_FLAG = "_STRIPE_LAZY_INNER_RUN"
_DENY_FLAG = "_STRIPE_LAZY_DENY_NETWORK"
_ATTEMPT_LOG_FLAG = "_STRIPE_LAZY_ATTEMPT_LOG"

# Installed as sitecustomize.py on PYTHONPATH, so it runs at interpreter start
# in the pytest process AND in every xdist worker (workers are separate
# interpreters that inherit the environment, not the parent's monkeypatches).
# A probe hidden in any hook of the lifecycle, in the controller or a worker,
# would connect, raise, and surface in the output (review). It is gated on an
# environment flag so it can never affect anything else.
_SITECUSTOMIZE = """
import os
if os.environ.get("%s") == "1":
    import socket
    _real_create_connection = socket.create_connection
    _real_connect = socket.socket.connect
    _real_connect_ex = socket.socket.connect_ex
    _log = os.environ.get("%s")

    def _is_local(address):
        # Loopback and Unix sockets stay open: xdist's own plumbing and
        # pytest-rerunfailures' shared-state server connect to localhost at
        # configure time. api.stripe.com is never loopback.
        if isinstance(address, (str, bytes)):
            return True
        host = address[0] if isinstance(address, tuple) and address else address
        return host in ("", "localhost", "127.0.0.1", "::1", "0.0.0.0")

    def _attempt(address):
        # Recorded BEFORE raising, so a probe that swallows the error is
        # still on the record (review).
        if _log:
            with open(_log, "a") as fh:
                fh.write(repr(address) + "\\n")
        # OSError, not RuntimeError: the conftest's probe classifies OSError
        # as "blocked" and skips, so the positive control below can prove the
        # real skip path rather than a setup error (second reviewer).
        raise OSError("NETWORK ACCESS DURING PYTEST LIFECYCLE: %%r" %% (address,))

    def _guard_method(real):
        # socket.socket.connect / connect_ex, called as (self, address, ...).
        def wrapped(self, address, *args, **kwargs):
            if not _is_local(address):
                _attempt(address)
            return real(self, address, *args, **kwargs)
        return wrapped

    def _guard_create_connection(real):
        # socket.create_connection(address, timeout=..., ...): address first.
        def wrapped(address, *args, **kwargs):
            if not _is_local(address):
                _attempt(address)
            return real(address, *args, **kwargs)
        return wrapped

    socket.create_connection = _guard_create_connection(_real_create_connection)
    socket.socket.connect = _guard_method(_real_connect)
    socket.socket.connect_ex = _guard_method(_real_connect_ex)
""" % (_DENY_FLAG, _ATTEMPT_LOG_FLAG)


def _inner_pytest(tmp_path, *args):
    import subprocess
    import sys

    site_dir = tmp_path / "site"
    site_dir.mkdir(exist_ok=True)
    (site_dir / "sitecustomize.py").write_text(_SITECUSTOMIZE)
    attempts = tmp_path / "attempts.log"
    env = dict(
        os.environ,
        STRIPE_TEST_SECRET_KEY="sk_test_not_real",
        PYTHONPATH=str(site_dir) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        **{_INNER_FLAG: "1", _DENY_FLAG: "1", _ATTEMPT_LOG_FLAG: str(attempts)},
    )
    # The child runs with the outer environment minus two things. No proxy:
    # behind an HTTPS proxy the probe's first connect is to the proxy, not to
    # Stripe, and the attempt log would name the proxy. And no inherited
    # pytest selection: a PYTEST_ADDOPTS of `-m "not stripe_e2e"` on the
    # outer run would deselect the positive control's own marked test
    # (review). Anything else the outer environment carries is inherited
    # as is; that is the documented boundary of these two tests.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "PYTEST_ADDOPTS"):
        env.pop(name, None)
        env.pop(name.lower(), None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly", "-n", "2", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


@pytest.mark.skipif(os.environ.get(_INNER_FLAG) == "1", reason="inner run")
def test_a_real_pytest_session_over_unmarked_tests_never_reaches_the_network(tmp_path):
    """The whole lifecycle, with a key in the environment: still no connect."""
    result = _inner_pytest(tmp_path, "tests/async-unit/test_worker_assets.py")
    out = result.stdout + result.stderr
    attempts = tmp_path / "attempts.log"
    # The record, not the exception: a probe that swallows the error is
    # still an attempt (review).
    assert not attempts.exists() or attempts.read_text() == "", attempts.read_text()
    assert "NETWORK ACCESS" not in out, out
    assert result.returncode == 0, out
    # The guard was live in the workers too: xdist actually ran. Under -q
    # its only trace is the "bringing up nodes" line.
    assert "bringing up nodes" in out or "[gw0]" in out or "2 workers" in out, out


@pytest.mark.skipif(os.environ.get(_INNER_FLAG) == "1", reason="inner run")
def test_a_marked_test_is_what_triggers_the_probe(tmp_path):
    """Positive control: the guard is real, and the probe runs for a marked
    test, at setup, not before."""
    marked = tmp_path / "test_marked_probe.py"
    marked.write_text(
        "import pytest\n"
        "@pytest.mark.stripe_e2e\n"
        "def test_marked():\n"
        "    pass\n"
    )
    result = _inner_pytest(tmp_path, "-p", "tests.conftest", "-rs", str(marked))
    out = result.stdout + result.stderr
    attempts = tmp_path / "attempts.log"
    # The probe ran, inside a worker (a non-loopback attempt is on the
    # record) and, because the connect "failed", the marked test was
    # SKIPPED with the proxy reason: a clean session, not a setup error.
    # The hostname is not asserted: what matters is that an outbound
    # attempt happened at all.
    assert attempts.exists() and attempts.read_text().strip(), out
    assert "SSL-intercepting proxy" in out, out
    assert result.returncode == 0, out

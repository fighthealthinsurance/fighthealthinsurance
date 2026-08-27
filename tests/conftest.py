"""
Pytest configuration for the test suite.

Sets up environment variables needed for tests to match tox configuration.
"""

import os
import shutil
import ssl
import urllib.error
import urllib.request

import pytest

# Set TESTING=True to match tox.ini configuration
# This is needed for SessionRequiredMixin and other test-aware code
os.environ["TESTING"] = "True"


def _has_ssl_intercepting_proxy() -> bool:
    """Detect environments where api.stripe.com can't be reached with a valid
    certificate.

    Covers env-var-configured proxies, transparent SSL-intercepting proxies
    (which set no proxy env vars), and fully firewalled sandboxes. The E2E
    Stripe tests can't pass in any of those, so they should skip rather than
    fail on connect.

    The probe verifies against the Stripe SDK's bundled CA file — the same
    bundle the SDK uses at request time. A sandbox that injects its MITM CA
    into the *system* trust store would otherwise pass a default-context
    probe while every real SDK call still fails certificate verification.
    """
    try:
        import stripe

        ssl_context = ssl.create_default_context(cafile=stripe.ca_bundle_path)
    except Exception:
        ssl_context = ssl.create_default_context()
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(),
            urllib.request.HTTPSHandler(context=ssl_context),
        )
        opener.open("https://api.stripe.com", timeout=5)
        return False
    except urllib.error.HTTPError:
        # An HTTP error status still means we reached Stripe with a valid
        # TLS handshake; only transport/verification failures mean blocked.
        return False
    except (ssl.SSLError, ssl.SSLCertVerificationError, urllib.error.URLError, OSError):
        return True


# Only probe when Stripe is actually configured. The E2E tests gated on this
# need STRIPE_TEST_SECRET_KEY and can't run without it, so when it's unset we
# skip them without paying ~5s for a network probe to api.stripe.com whose
# result can't change the outcome (pure waste in firewalled CI that never
# configures Stripe).
_skip_stripe_ssl = (
    _has_ssl_intercepting_proxy() if os.environ.get("STRIPE_TEST_SECRET_KEY") else True
)

skip_if_stripe_ssl_blocked = pytest.mark.skipif(
    _skip_stripe_ssl,
    reason="Stripe not configured (STRIPE_TEST_SECRET_KEY unset) or SSL-intercepting proxy blocks api.stripe.com",
)

skip_if_no_pandoc = pytest.mark.skipif(
    shutil.which("pandoc") is None,
    reason="pandoc is not installed",
)

skip_if_no_nice_api_key = pytest.mark.skipif(
    not os.environ.get("NICE_API_KEY"),
    reason="NICE_API_KEY environment variable is not set",
)


@pytest.fixture(autouse=True)
def _clear_pa_resolver_cache():
    """Reset the PA-requirement regex-resolver cache between tests.

    ``fighthealthinsurance.pa_requirements._regex_candidates`` is an
    ``lru_cache``d helper keyed on a 5-minute time bucket. Without this
    fixture, candidates that were registered as ``InsuranceCompany`` rows
    by test A leak into test B (because both run inside the same bucket),
    causing flaky resolver hits. The import is local so test runs that
    never touch the PA module don't pay an import cost.
    """
    try:
        from fighthealthinsurance.pa_requirements import _regex_candidates
    except Exception:
        yield
        return
    _regex_candidates.cache_clear()
    yield
    _regex_candidates.cache_clear()


@pytest.fixture(autouse=True)
def _stub_denied_items_analysis_dispatch():
    """Stub the denied-items analysis Ray dispatch for the whole suite.

    ``OngoingChatConsumer.disconnect`` enqueues analysis on a detached Ray
    actor. In tests there is no Ray cluster, so the first unpatched chat
    disconnect would lazily start an in-process Ray instance whose detached
    actor crash-loops against the test database and eventually hard-kills
    the pytest process mid-suite. Tests that assert on dispatch behavior
    patch the same attribute themselves; their innermost patch wins.
    """
    from unittest.mock import patch as _patch

    try:
        with _patch(
            "fighthealthinsurance.denied_items_analysis_actor_ref.denied_items_analysis_actor_ref"
        ) as mock_ref:
            mock_ref.get.run_analysis.remote.return_value = None
            yield
    except (ImportError, AttributeError):
        yield


def _rollback_leaked_transactions() -> None:
    """Roll back any transaction left open on the calling thread's connections.

    Runs inside the thread that owns the connections -- a sqlite connection may
    only be rolled back from its own thread.
    """
    from django.db import connections

    for conn in connections.all(initialized_only=True):
        try:
            raw = conn.connection
            # in_transaction is sqlite's; other backends just fall through.
            if raw is None or not raw.in_transaction:
                continue
            # Reset Django's own transaction bookkeeping first: a block that
            # never unwound leaves in_atomic_block set, and rollback() refuses
            # to run inside one.
            conn.in_atomic_block = False
            conn.savepoint_ids = []
            conn.atomic_blocks = []
            conn.needs_rollback = False
            conn.rollback()
            conn.set_autocommit(True)
        except Exception:
            # Best effort: a connection we can't reset is no worse than the
            # one we started with.
            pass


@pytest.fixture(autouse=True)
def _settle_thread_sensitive_db_work():
    """Drain asgiref's shared thread-sensitive executor at the end of each test.

    In a plain ``async def`` test (nothing wraps it in ``async_to_sync``), the
    native async ORM and ``database_sync_to_async`` both run their sync body on
    asgiref's process-wide ``SyncToAsync.single_thread_executor``. Two things
    leak out of a test that way:

    * a call whose awaiting task was abandoned when the test's event loop closed
      keeps running there afterwards, so its queries can still be in flight
      during the next test, and
    * that thread's connection can be left mid-transaction -- an ``atomic``
      whose COMMIT failed against a lock the main thread held, say. Django never
      closes an in-memory sqlite connection (closing would destroy the
      database), so ``close_old_connections`` doesn't drop the broken one the
      way it would in production, and nothing else clears it.

    Either one holds a sqlite table lock, and shared-cache sqlite answers a
    conflicting lock with an immediate "database table is locked" rather than
    honouring the busy timeout. The next ``transaction=True`` test then dies in
    its teardown flush ("Database ... couldn't be flushed") and the rows that
    flush failed to delete leak into every test after it.

    The executor is a single-worker FIFO queue, so waiting on one task of our
    own is exactly a barrier: every call queued ahead of it has finished by the
    time it runs. The task then rolls back whatever was left open.

    Defined ahead of ``_drain_fire_and_forget_threads`` deliberately: teardown
    runs in reverse order, so those threads are joined first and the DB work
    they queued onto this same executor is drained here.
    """
    yield
    try:
        from asgiref.sync import SyncToAsync
    except Exception:
        return
    executor = getattr(SyncToAsync, "single_thread_executor", None)
    if executor is None:
        return
    try:
        executor.submit(_rollback_leaked_transactions).result(timeout=10.0)
    except Exception:
        # Draining is best effort: if the queued work is itself blocked on a
        # lock this test still holds, timing out leaves us no worse off.
        pass


@pytest.fixture(autouse=True)
def _drain_fire_and_forget_threads():
    """Drain fire-and-forget background threads at the end of each test.

    ``fire_and_forget_in_new_threadpool`` runs coroutines -- often sqlite writes,
    e.g. chooser task prefill or chat context summaries -- in detached daemon
    threads. If one outlives the test that started it, its DB work can hold a
    table lock during the next test's fixture load, surfacing as a flaky
    "database table is locked" in the parallel async suite. Joining outstanding
    threads at teardown (which runs before the next class's setUpClass) keeps
    that work inside the test that started it. The per-thread timeout is a safety
    cap; leaked threads normally finish in milliseconds.
    """
    yield
    try:
        from fighthealthinsurance.utils import join_fire_and_forget_threads
    except Exception:
        return
    join_fire_and_forget_threads(timeout=10.0)

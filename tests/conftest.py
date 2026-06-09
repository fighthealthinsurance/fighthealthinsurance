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


_skip_stripe_ssl = _has_ssl_intercepting_proxy()

skip_if_stripe_ssl_blocked = pytest.mark.skipif(
    _skip_stripe_ssl,
    reason="SSL-intercepting proxy blocks Stripe API connections",
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

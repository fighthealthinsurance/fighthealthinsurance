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
    """Detect if an SSL-intercepting proxy prevents connecting to api.stripe.com.

    Returns True when the environment has an HTTPS proxy AND SSL verification
    to api.stripe.com fails (e.g. self-signed cert from a MITM proxy).
    Returns False when there is no proxy or when SSL verification succeeds.
    """
    if not (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")):
        return False
    try:
        handler = urllib.request.ProxyHandler()
        opener = urllib.request.build_opener(handler)
        opener.open("https://api.stripe.com", timeout=5)
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

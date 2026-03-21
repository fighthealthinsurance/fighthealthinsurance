"""
Pytest configuration for the test suite.

Sets up environment variables needed for tests to match tox configuration.
"""

import os
import ssl
import socket

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
        ctx = ssl.create_default_context()
        with socket.create_connection(("api.stripe.com", 443), timeout=5) as sock:
            with ctx.wrap_socket(sock, server_hostname="api.stripe.com"):
                pass
        return False
    except (ssl.SSLError, ssl.SSLCertVerificationError, OSError):
        return True


_skip_stripe_ssl = _has_ssl_intercepting_proxy()

skip_if_stripe_ssl_blocked = pytest.mark.skipif(
    _skip_stripe_ssl,
    reason="SSL-intercepting proxy blocks Stripe API connections",
)

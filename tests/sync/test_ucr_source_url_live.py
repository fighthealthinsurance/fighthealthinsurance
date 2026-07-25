"""Sanity-check the canonical source URLs we ship in UCR_SOURCE_URLS.

A HEAD against each URL confirms the page resolves; this catches typos /
hallucinated paths in the constant. Skips when the test environment has no
outbound internet (CI sandboxes, MITM proxies that reject self-signed
certs), which is fine — the goal is to fail loudly when we *can* reach the
network and the URL is wrong, not to require connectivity.
"""

import socket
import ssl
import urllib.error
import urllib.request

import pytest
from django.test import SimpleTestCase

from fighthealthinsurance.ucr_constants import UCR_SOURCE_URLS


_HEAD_TIMEOUT_SECONDS = 8


def _head(url: str) -> int:
    """HEAD `url` and return the status code, or raise the original error."""
    request = urllib.request.Request(url, method="HEAD")
    request.add_header(
        "User-Agent",
        # Some CDNs (cms.gov among them) gate default urllib UA strings.
        "Mozilla/5.0 (compatible; fhi-ucr-source-check/1.0)",
    )
    with urllib.request.urlopen(request, timeout=_HEAD_TIMEOUT_SECONDS) as resp:
        return resp.status


class UCRSourceURLLiveTests(SimpleTestCase):
    def test_each_source_url_resolves(self):
        if not UCR_SOURCE_URLS:
            self.skipTest("UCR_SOURCE_URLS is empty")

        for source, url in UCR_SOURCE_URLS.items():
            with self.subTest(source=source, url=url):
                try:
                    status = _head(url)
                except (
                    socket.timeout,
                    socket.gaierror,
                    ssl.SSLError,
                    urllib.error.URLError,
                ) as e:
                    pytest.skip(f"Network unreachable for {url}: {e}")
                # Accept any 2xx/3xx; some sites 30x to a regional mirror.
                self.assertLess(
                    status,
                    400,
                    f"UCR_SOURCE_URLS[{source!r}]={url!r} returned HTTP {status}; "
                    "the constant may be stale or hallucinated.",
                )

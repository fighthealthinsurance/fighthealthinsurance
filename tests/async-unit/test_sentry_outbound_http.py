"""Outbound request details stay out of Sentry.

Sentry's httpx, aiohttp and stdlib integrations record each outbound request
as an "http.client" span and an "httplib" breadcrumb, with the query string
and fragment as separate fields. The filters in sentry_filters remove those
fields, clean the URL (and the path, for hosts not known to use fixed routes),
and the propagation targets keep trace headers on our own hosts.
"""

import re

import httpx
import pytest
import sentry_sdk
from sentry_sdk.transport import Transport

from fighthealthinsurance.sentry_filters import (
    FHI_TRACE_PROPAGATION_TARGETS,
    FILTERED,
    PATH_REMOVED,
    before_breadcrumb_filter,
    before_send_filter,
    before_send_transaction_filter,
    clean_outbound_url,
    scrub_outbound_http,
)

QUERY_KEYS = ("http.query", "http.fragment", "url.query", "url.fragment")


class TestCleanOutboundUrl:
    def test_query_fragment_and_credentials_go(self):
        url = "https://user:pw@eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?term=x&api_key=k#top"
        assert (
            clean_outbound_url(url)
            == "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        )

    def test_unknown_host_keeps_only_scheme_and_host(self):
        assert (
            clean_outbound_url("https://portal.example.com/members/12345/claims?id=9")
            == "https://portal.example.com" + PATH_REMOVED
        )

    def test_subdomain_of_a_kept_host_keeps_its_path(self):
        assert (
            clean_outbound_url("https://myres.openai.azure.com/openai/deployments/x")
            == "https://myres.openai.azure.com/openai/deployments/x"
        )

    def test_lookalike_host_is_not_kept(self):
        assert clean_outbound_url("https://evil-doi.org/10.1/x").endswith(
            PATH_REMOVED
        )

    def test_port_is_kept(self):
        assert (
            clean_outbound_url("http://localhost:8001/search?q=x")
            == "http://localhost:8001" + PATH_REMOVED
        )

    @pytest.mark.parametrize("value", ["not a url", "/relative/path?q=1"])
    def test_unparseable_becomes_filtered(self, value):
        assert clean_outbound_url(value) == FILTERED

    @pytest.mark.parametrize("value", [None, 3, {"a": 1}])
    def test_non_text_passes_through(self, value):
        assert clean_outbound_url(value) == value


def _http_span(url_with_query: str) -> dict:
    base, _, query = url_with_query.partition("?")
    return {
        "op": "http.client",
        "description": f"GET {base}",
        "data": {
            "url": base,
            "http.query": query,
            "http.fragment": "frag",
            "http.method": "GET",
        },
    }


class TestEventScrub:
    def test_transaction_spans_lose_query(self):
        event = {
            "type": "transaction",
            "transaction": "/appeal/",
            "transaction_info": {"source": "route"},
            "spans": [
                _http_span("https://api.example.org/x/y?term=headache"),
                {"op": "db", "description": "SELECT 1", "data": {"x": 1}},
            ],
        }
        out = before_send_transaction_filter(event, {})
        span = out["spans"][0]
        assert not any(key in span["data"] for key in QUERY_KEYS)
        assert span["data"]["url"] == "https://api.example.org" + PATH_REMOVED
        assert span["description"] == "GET https://api.example.org" + PATH_REMOVED
        assert span["data"]["http.method"] == "GET"
        assert out["spans"][1] == {"op": "db", "description": "SELECT 1", "data": {"x": 1}}

    def test_error_event_breadcrumbs_lose_query(self):
        event = {
            "exception": {"values": [{"type": "KeyError", "value": "x"}]},
            "breadcrumbs": {
                "values": [
                    {
                        "type": "http",
                        "category": "httplib",
                        "data": {
                            "url": "https://rxnav.nlm.nih.gov/REST/drugs.json",
                            "http.query": "name=metformin",
                            "method": "GET",
                        },
                    },
                    {"category": "query", "message": "SELECT 1"},
                ]
            },
        }
        out = before_send_filter(event, {})
        crumb = out["breadcrumbs"]["values"][0]
        assert "http.query" not in crumb["data"]
        assert crumb["data"]["url"] == "https://rxnav.nlm.nih.gov/REST/drugs.json"
        assert out["breadcrumbs"]["values"][1] == {
            "category": "query",
            "message": "SELECT 1",
        }

    def test_existing_filters_still_drop(self):
        # The new scrub runs after the existing drops, so they still apply.
        unrouted = {
            "type": "transaction",
            "transaction": "/.env",
            "transaction_info": {"source": "url"},
            "spans": [_http_span("https://api.example.org/x?y=1")],
        }
        assert before_send_transaction_filter(unrouted, {}) is None

    @pytest.mark.parametrize(
        "event",
        [
            None,
            "text",
            {"spans": None},
            {"spans": [None, 3, {"op": None}]},
            {"spans": [{"op": "http.client", "data": None, "description": 5}]},
            {"breadcrumbs": [None, {"category": "httplib", "data": "x"}]},
            {"breadcrumbs": {"values": None}},
        ],
    )
    def test_never_raises(self, event):
        scrub_outbound_http(event)
        before_breadcrumb_filter(event, {})


class TestPropagationTargets:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.fighthealthinsurance.com/v0/x",
            "https://api.fightpaperwork.com/",
            "https://fightinsurance.ai",
            "http://phoenix.fhi-eval.svc:6006/v1/projects",
            "http://temporal-frontend.default.svc.cluster.local:7233/",
            "http://localhost:8001/x",
        ],
    )
    def test_our_hosts_match(self, url):
        assert any(re.search(t, url) for t in FHI_TRACE_PROPAGATION_TARGETS)

    @pytest.mark.parametrize(
        "url",
        [
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            "https://api.deepinfra.com/v1/openai/chat/completions",
            "https://evil.com/?next=https://www.fighthealthinsurance.com/",
            "https://fighthealthinsurance.com.evil.com/",
            "https://notfighthealthinsurance.com/",
        ],
    )
    def test_outside_hosts_do_not_match(self, url):
        assert not any(re.search(t, url) for t in FHI_TRACE_PROPAGATION_TARGETS)


class _Capture(Transport):
    def __init__(self, options=None):
        super().__init__(options)
        self.events: list = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            payload = item.payload.json
            if payload is not None:
                self.events.append(payload)


@pytest.fixture
def sentry_capture():
    transport = _Capture()
    sentry_sdk.init(
        dsn="https://public@sentry.invalid/1",
        transport=transport,
        traces_sample_rate=1.0,
        before_send=before_send_filter,
        before_send_transaction=before_send_transaction_filter,
        before_breadcrumb=before_breadcrumb_filter,
        trace_propagation_targets=FHI_TRACE_PROPAGATION_TARGETS,
        default_integrations=False,
        auto_enabling_integrations=False,
        integrations=[
            __import__(
                "sentry_sdk.integrations.httpx", fromlist=["HttpxIntegration"]
            ).HttpxIntegration()
        ],
    )
    try:
        yield transport
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.init()  # back to a disabled client for later tests


def test_real_httpx_request_is_scrubbed_and_untagged(sentry_capture):
    seen_headers: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers[request.url.host] = dict(request.headers)
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with sentry_sdk.start_transaction(name="/appeal/", source="route"):
        client.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={"term": "lumbar fusion back pain", "api_key": "secret-key"},
        )
        client.get("https://www.fighthealthinsurance.com/healthz")
        sentry_sdk.capture_message("after the calls")
    sentry_sdk.flush()

    outside = seen_headers["eutils.ncbi.nlm.nih.gov"]
    assert "sentry-trace" not in outside and "baggage" not in outside
    assert "sentry-trace" in seen_headers["www.fighthealthinsurance.com"]

    blob = repr(sentry_capture.events)
    assert "lumbar" not in blob
    assert "secret-key" not in blob
    assert "esearch.fcgi" in blob  # the fixed API path is kept

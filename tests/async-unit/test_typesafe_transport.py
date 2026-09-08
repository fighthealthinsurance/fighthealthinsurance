"""ml/typesafe.py: the shared TypeSafe transport.

The request carries the API key and the redacted document, so it only ever
goes over https; the body of a failed response is never read, because an
error body could quote the document back.
"""

import asyncio
from unittest.mock import patch

import pytest
from django.test import override_settings

from fighthealthinsurance.ml import typesafe

SETTINGS = dict(
    TYPESAFE_API_KEY="test-key", TYPESAFE_API_URL="https://typesafe.invalid/v1/systemone"
)


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return {"answers": {}}


class _FakeSession:
    """Stands in for aiohttp.ClientSession: the constructor and the context."""

    def __init__(self, status=200):
        self.response = _FakeResponse(status)
        self.posted = []
        self.opened = 0

    def __call__(self, *args, **kwargs):
        self.opened += 1
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None):
        self.posted.append((url, json, headers))
        return self.response


def _ask(session, **overrides):
    conf = {**SETTINGS, **overrides}
    with override_settings(**conf), patch.object(typesafe.aiohttp, "ClientSession", session):
        return asyncio.run(typesafe.ask("doc", {"q": {"type": "int"}}, timeout_seconds=1))


@pytest.mark.parametrize(
    "url",
    [
        "http://typesafe.invalid/v1/systemone",
        "ftp://typesafe.invalid/v1/systemone",
        "typesafe.invalid/v1/systemone",
        "",
        None,
    ],
)
def test_anything_but_https_is_refused_before_a_session_exists(url):
    session = _FakeSession()
    with pytest.raises(typesafe.TypeSafeError, match="https"):
        _ask(session, TYPESAFE_API_URL=url)
    assert session.opened == 0
    assert session.posted == []


def test_https_in_any_case_is_accepted_and_the_json_comes_back():
    session = _FakeSession()
    assert _ask(session, TYPESAFE_API_URL="HTTPS://typesafe.invalid/v1/systemone") == {"answers": {}}
    url, body, headers = session.posted[0]
    assert url == "HTTPS://typesafe.invalid/v1/systemone"
    assert headers["Authorization"] == "Bearer test-key"
    assert body["document"] == "doc"


def test_a_non_200_raises():
    with pytest.raises(typesafe.TypeSafeError, match="HTTP 503"):
        _ask(_FakeSession(503))

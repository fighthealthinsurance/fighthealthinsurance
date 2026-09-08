"""ml/typesafe.py: the shared TypeSafe transport.

The request carries the API key and the redacted document, so it only ever
goes over https and never follows a redirect. The one thing a caller may
learn from a failed call is the HTTP status: the body is never read, because
an error body could quote the document back.
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
        self.json_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        self.json_calls += 1
        return {"answers": {}}


class _FakeSession:
    """Stands in for aiohttp.ClientSession: the constructor and the context."""

    def __init__(self, status=200):
        self.response = _FakeResponse(status)
        self.posted = []
        self.opened = 0
        self.post_kwargs = {}

    def __call__(self, *args, **kwargs):
        self.opened += 1
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None, **kwargs):
        self.posted.append((url, json, headers))
        self.post_kwargs = kwargs
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


def test_a_non_200_raises_with_the_status_attached():
    with pytest.raises(typesafe.TypeSafeError) as info:
        _ask(_FakeSession(402))
    assert info.value.status == 402
    assert str(info.value) == "HTTP 402"


def test_the_body_is_never_read_on_a_failure():
    session = _FakeSession(503)
    with pytest.raises(typesafe.TypeSafeError):
        _ask(session)
    assert session.response.json_calls == 0


def test_redirects_are_never_followed():
    """An https endpoint answering 307 toward http would otherwise make the
    client resend the document in the clear (review)."""
    session = _FakeSession(307)
    with pytest.raises(typesafe.TypeSafeError, match="HTTP 307"):
        _ask(session)
    assert session.post_kwargs.get("allow_redirects") is False


def test_the_status_is_optional_on_the_error():
    assert typesafe.TypeSafeError("bad answer").status is None

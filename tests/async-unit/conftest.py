"""Shared fixtures for async-unit tests of the ML backend transport layer."""

import os

import aiohttp
import pytest

# Unit tests must compose the same MLRouter everywhere. The ML backend
# classes read provider credentials and backend hosts from the environment,
# so on a machine where any of these are set a unit-suite MLRouter() picks
# up live backends -- routing tests then assert against a different model
# pool than CI sees, and a selected live backend means a unit test making
# real network calls to a provider. Scrub them before any test module
# imports app code (this runs at collection start, ahead of both the lazy
# router singleton and routers built in setUp). Keep the list in sync with
# the os.getenv / *_ENV usage in fighthealthinsurance/ml/ml_models.py;
# test_ml_router.py::TestRouterHermeticity fails if a new one slips in.
_AMBIENT_BACKEND_ENV_VARS = (
    "ALPHA_HEALTH_BACKEND_HOST",
    "ALPHA_HEALTH_BACKEND_PORT",
    "ALPHA_HEALTH_BACKUP_BACKEND_HOST",
    "ALPHA_HEALTH_BACKUP_BACKEND_PORT",
    "ANTHROPIC_API_KEY",
    "AZURE_ANTHROPIC_API_KEY",
    "AZURE_ANTHROPIC_ENDPOINT",
    "AZURE_ANTHROPIC_MODELS",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_MODELS",
    "DEEPINFRA_API",
    "HEALTH_BACKEND_HOST",
    "HEALTH_BACKEND_PORT",
    "HEALTH_BACKUP_BACKEND_HOST",
    "HEALTH_BACKUP_BACKEND_MODEL",
    "HEALTH_BACKUP_BACKEND_PORT",
    "NEW_HEALTH_BACKEND_HOST",
    "NEW_HEALTH_BACKEND_PORT",
    "PERPLEXITY_API",
    "SECONDARY_NEW_HEALTH_BACKEND_HOST",
    "SECONDARY_NEW_HEALTH_BACKEND_PORT",
)
for _name in _AMBIENT_BACKEND_ENV_VARS:
    os.environ.pop(_name, None)
from loguru import logger
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL


class LogCapture:
    """Context manager capturing loguru records at DEBUG and above."""

    def __enter__(self):
        self.records = []
        self._sink_id = logger.add(
            lambda msg: self.records.append(msg.record), level="DEBUG"
        )
        return self

    def __exit__(self, *exc):
        logger.remove(self._sink_id)

    @property
    def levels(self):
        return [r["level"].name for r in self.records]

    def messages(self, level):
        return [r["message"] for r in self.records if r["level"].name == level]


class FakeModelResponse:
    """Canned response for ``aiohttp.ClientSession.post`` stand-ins."""

    def __init__(self, status: int, body: str = "", json_data=None):
        self.status = status
        self._body = body
        self._json = json_data

    async def text(self):
        return self._body

    async def json(self):
        return self._json

    @property
    def request_info(self):
        # A real RequestInfo so any str()/repr() of an error built from this
        # response (loguru, pytest tracebacks) can dereference real_url.
        url = URL("http://fake-backend.example/v1/chat/completions")
        return aiohttp.RequestInfo(url, "POST", CIMultiDictProxy(CIMultiDict()), url)

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=self.request_info,
                history=(),
                status=self.status,
                message="Not Found" if self.status == 404 else "Error",
            )


class FakeModelPost:
    """Stands in for ClientSession.post: an async context manager yielding
    the canned response, counting calls so tests can assert an endpoint was
    (or was not) re-hit."""

    def __init__(self, response: FakeModelResponse):
        self._response = response
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def log_capture():
    """The LogCapture class; use as ``with log_capture() as cap:``."""
    return LogCapture


@pytest.fixture
def make_fake_model_post():
    """Factory for a ClientSession.post stand-in serving one canned response."""

    def _make(status: int, body: str = "", json_data=None) -> FakeModelPost:
        return FakeModelPost(FakeModelResponse(status, body, json_data))

    return _make

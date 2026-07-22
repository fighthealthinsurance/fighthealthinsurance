"""Shared fixtures for async-unit tests of the ML backend transport layer."""

import aiohttp
import pytest
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

"""Cooldown for backends that reject our credentials.

A 401/402/403 is a standing account state -- a rotated key, an unpaid
balance, a deployment the key can't reach -- not a per-request blip. Every
inference used to re-hit such a backend and re-print the same "check
quota/billing/API key" warning. Instead the (api_base, model) pair is flagged
for a cooldown: one WARNING per window, later calls skip it without a round
trip, the router stops selecting the instance, and the startup probe still
goes live. Rate limiting (429) keeps its own short back-off and must NOT be
cooled down this way.

Shared fakes (log_capture, make_fake_model_post) live in conftest.py.
"""

import time
from typing import Optional, Tuple, List

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from fighthealthinsurance.ml.ml_models import (
    AUTH_HTTP_STATUS_CODES,
    RateLimitedRemoteOpenLike,
    RemoteFullOpenLike,
    _http_status_is_auth_failure,
)

UNAUTHORIZED_BODY = (
    '{"error":{"code":"PermissionDenied","message":"Access denied due to '
    'invalid subscription key or wrong API endpoint."}}'
)


def _model() -> RemoteFullOpenLike:
    return RemoteFullOpenLike("https://paid.test/v1", "bad-token", "some-model")


def _rate_limited_model() -> RateLimitedRemoteOpenLike:
    model = RateLimitedRemoteOpenLike("https://paid.test/v1", "bad-token", "premium")
    RateLimitedRemoteOpenLike._ensure_rate_limiter("premium")
    return model


def _client_response_error(status: int) -> aiohttp.ClientResponseError:
    url = URL("https://paid.test/v1/chat/completions")
    return aiohttp.ClientResponseError(
        request_info=aiohttp.RequestInfo(
            url, "POST", CIMultiDictProxy(CIMultiDict()), url
        ),
        history=(),
        status=status,
        message="PermissionDenied",
    )


class TestAuthStatusClassification:
    def test_key_quota_and_forbidden_codes_are_auth_failures(self):
        for status in (401, 402, 403):
            assert _http_status_is_auth_failure(status)
            assert status in AUTH_HTTP_STATUS_CODES

    def test_rate_limit_and_server_errors_are_not_auth_failures(self):
        for status in (429, 404, 500, None):
            assert not _http_status_is_auth_failure(status)


class TestCooldownAccounting:
    def test_one_failure_starts_the_cooldown(self):
        m = _model()
        m._note_auth_failure(m.api_base, m.model, 401, "PermissionDenied")
        assert m._auth_cooling(m.api_base, m.model) is True

    def test_cooldown_expires_and_probes_again(self):
        m = _model()
        m._note_auth_failure(m.api_base, m.model, 401, "PermissionDenied")
        m._auth_failures[(m.api_base, m.model)] = time.monotonic() - 1
        assert m._auth_cooling(m.api_base, m.model) is False

    def test_cooldown_is_keyed_per_endpoint(self):
        m = _model()
        m._note_auth_failure("https://other.test/v1", "other", 403, "Forbidden")
        assert m._auth_cooling(m.api_base, m.model) is False

    def test_backup_leg_is_flagged_from_the_failing_url(self):
        m = RemoteFullOpenLike(
            "https://primary.test/v1",
            "tok",
            "some-model",
            backup_api_base="https://paid.test/v1",
            backup_model="backup-model",
        )
        base, model = m._auth_pair_for_error(_client_response_error(401))
        assert (base, model) == ("https://paid.test/v1", "backup-model")

    def test_router_stops_selecting_a_backend_with_no_working_key(self):
        m = _model()
        m._note_auth_failure(m.api_base, m.model, 401, "PermissionDenied")
        assert m.is_available() is False


class TestInferBehavior:
    @pytest.mark.asyncio
    async def test_401_warns_once_and_skips_the_next_call(
        self, monkeypatch, make_fake_model_post, log_capture
    ):
        model = _model()
        fake_post = make_fake_model_post(401, UNAUTHORIZED_BODY)
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

        with log_capture() as cap:
            first = await model._infer(system_prompts=["sys"], prompt="hi")
            second = await model._infer(system_prompts=["sys"], prompt="hi")

        assert first is None and second is None
        # One HTTP round-trip total: the second call skipped the endpoint.
        assert fake_post.calls == 1
        warnings = cap.messages("WARNING")
        assert len(warnings) == 1
        assert "401" in warnings[0]
        assert "quota" in warnings[0].lower()
        assert cap.messages("ERROR") == []
        assert any("auth-failure cooldown" in m for m in cap.messages("DEBUG"))

    @pytest.mark.asyncio
    async def test_probe_bypasses_the_cooldown_and_raises(
        self, monkeypatch, make_fake_model_post
    ):
        model = _model()
        fake_post = make_fake_model_post(401, UNAUTHORIZED_BODY)
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

        await model._infer(system_prompts=["sys"], prompt="hi")
        assert model._auth_cooling(model.api_base, model.model)

        with pytest.raises(aiohttp.ClientResponseError):
            await model._infer(
                system_prompts=["sys"], prompt="hi", raise_http_errors=True
            )
        assert fake_post.calls == 2

    @pytest.mark.asyncio
    async def test_429_does_not_cool_the_backend_down(
        self, monkeypatch, make_fake_model_post
    ):
        """Rate limiting clears on its own; it must keep its own short
        back-off rather than sidelining the backend for the auth window."""
        model = _model()
        fake_post = make_fake_model_post(429, '{"error":"slow down"}')
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

        await model._infer(system_prompts=["sys"], prompt="hi")

        assert model._auth_cooling(model.api_base, model.model) is False
        assert model.is_available() is True


class TestCustomTransportBackends:
    """Providers whose wire format isn't OpenAI-compatible (e.g. Claude on
    Azure AI Foundry) never reach RemoteOpenLike.__infer, so the gate has to
    live in the rate-limited _infer too -- that path is what logged the
    repeated 'skipping backend -- ... HTTP 401 (PermissionDenied)' lines."""

    @pytest.mark.asyncio
    async def test_second_call_never_reaches_the_transport(self, log_capture):
        calls: List[int] = []

        class FakeMessagesBackend(RateLimitedRemoteOpenLike):
            _rate_limiters: dict = {}
            _rate_limiter_lock = RateLimitedRemoteOpenLike._rate_limiter_lock

            async def _do_infer(
                self, *args, **kwargs
            ) -> Optional[Tuple[Optional[str], Optional[List[str]]]]:
                calls.append(1)
                raise _client_response_error(401)

        model = FakeMessagesBackend("https://paid.test/v1", "bad-token", "premium")
        FakeMessagesBackend._ensure_rate_limiter("premium")

        with log_capture() as cap:
            assert await model._infer(system_prompts=["sys"], prompt="hi") is None
            assert await model._infer(system_prompts=["sys"], prompt="hi") is None

        assert len(calls) == 1
        warnings = cap.messages("WARNING")
        assert len(warnings) == 1
        assert "401" in warnings[0]

    @pytest.mark.asyncio
    async def test_rate_limited_openai_path_flags_once(
        self, monkeypatch, make_fake_model_post, log_capture
    ):
        model = _rate_limited_model()
        fake_post = make_fake_model_post(403, UNAUTHORIZED_BODY)
        monkeypatch.setattr(aiohttp.ClientSession, "post", fake_post)

        with log_capture() as cap:
            await model._infer(system_prompts=["sys"], prompt="hi")
            await model._infer(system_prompts=["sys"], prompt="hi")

        assert fake_post.calls == 1
        assert len(cap.messages("WARNING")) == 1

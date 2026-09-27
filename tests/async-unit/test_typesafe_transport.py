"""ml/typesafe.py: the shared TypeSafe transport.

The request is the documented System One shape (docs.typesafe.ai/api): a
``state``, a ``model`` and the ``questions``, with the key as a Bearer token.
It carries the API key and the redacted text, so it only ever goes over https
and never follows a redirect. The one thing a caller may learn from a failed
call is the HTTP status: the body is never read, because an error body could
quote the text back.
"""

import asyncio
import datetime
from unittest.mock import patch

import pytest
from django.conf import settings
from django.test import override_settings

from fighthealthinsurance.ml import denial_triage, letter_quality, typesafe

SETTINGS = dict(
    TYPESAFE_API_KEY="test-key", TYPESAFE_API_URL="https://typesafe.invalid/v1/systemone"
)


class _FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self.json_calls = 0
        self.payload = {"answers": {}} if payload is None else payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        self.json_calls += 1
        return self.payload


class _FakeSession:
    """Stands in for aiohttp.ClientSession: the constructor and the context."""

    def __init__(self, status=200, payload=None):
        self.response = _FakeResponse(status, payload)
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


QUESTIONS = {"is_urgent": {"type": "noul", "instructions": "Does this convey urgency?"}}


def _ask(session, state="doc", **overrides):
    conf = {**SETTINGS, **overrides}
    with override_settings(**conf), patch.object(typesafe.aiohttp, "ClientSession", session):
        return asyncio.run(typesafe.ask(state, QUESTIONS, timeout_seconds=1))


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
    assert body["state"] == "doc"


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


def test_the_body_is_the_documented_state_model_and_questions():
    session = _FakeSession()
    _ask(session)
    _, body, headers = session.posted[0]
    assert body == {"state": "doc", "model": "jev-1.13.0", "questions": QUESTIONS}
    assert headers == {"Authorization": "Bearer test-key", "Content-Type": "application/json"}


def test_the_state_is_capped():
    session = _FakeSession()
    _ask(session, state="s" * (typesafe.STATE_CHAR_CAP + 500))
    assert session.posted[0][1]["state"] == "s" * typesafe.STATE_CHAR_CAP
    assert typesafe.STATE_CHAR_CAP == 24_000


def test_the_model_comes_from_the_setting():
    session = _FakeSession()
    _ask(session, TYPESAFE_MODEL="jev-latest")
    assert session.posted[0][1]["model"] == "jev-latest"


@pytest.mark.parametrize("value", ["", "   ", None])
def test_an_empty_model_setting_means_the_pinned_release(value):
    session = _FakeSession()
    _ask(session, TYPESAFE_MODEL=value)
    assert session.posted[0][1]["model"] == typesafe.DEFAULT_MODEL == "jev-1.13.0"


def test_the_pinned_release_is_the_default_and_the_test_pin():
    """A pinned version, not an alias TypeSafe can repoint, and the test
    configurations pin the same one."""
    assert typesafe.DEFAULT_MODEL == "jev-1.13.0"
    assert settings.TYPESAFE_MODEL == typesafe.DEFAULT_MODEL
    assert typesafe.model_name() == typesafe.DEFAULT_MODEL


@pytest.mark.parametrize("value", ["jev latest", "typesafe/jev", "jev\nlatest", "x" * 49])
def test_a_model_setting_that_is_not_a_model_name_is_refused_before_a_session(value):
    session = _FakeSession()
    with pytest.raises(typesafe.TypeSafeError, match="TYPESAFE_MODEL") as info:
        _ask(session, TYPESAFE_MODEL=value)
    assert info.value.status is None
    assert session.opened == 0 and session.posted == []


@pytest.mark.parametrize("status", [401, 422, 429, 529])
def test_each_documented_error_carries_its_status_and_nothing_else(status):
    session = _FakeSession(status, payload={"detail": "state: the text quoted back"})
    with pytest.raises(typesafe.TypeSafeError) as info:
        _ask(session, state="Jane Q. Doe was denied")
    assert info.value.status == status
    assert str(info.value) == f"HTTP {status}"
    assert info.value.args == (f"HTTP {status}",)
    assert session.response.json_calls == 0


class TestReportedModel:
    def test_the_versioned_id_the_response_names_is_recorded(self):
        with override_settings(TYPESAFE_MODEL="jev-latest"):
            assert typesafe.reported_model({"model": "jev-1.13.0"}) == "jev-1.13.0"

    def test_no_name_in_the_response_records_the_name_the_request_sent(self):
        with override_settings(TYPESAFE_MODEL="jev-latest"):
            assert typesafe.reported_model({"answers": {}}) == "jev-latest"
            assert typesafe.reported_model({"model": ""}) == "jev-latest"
            assert typesafe.reported_model(None) == "jev-latest"

    @pytest.mark.parametrize(
        "odd", ["weird value!", "a/b", "x" * 200, ["jev"], 123, 1.5, True]
    )
    def test_an_odd_name_in_the_response_cannot_forge_provenance(self, odd):
        assert typesafe.reported_model({"model": odd}) == typesafe.DEFAULT_MODEL


# The features go through ask() unchanged: the same fake session, the real
# _post seams, the documented request and a documented response.

_ENABLED = dict(
    SETTINGS,
    TYPESAFE_LETTER_RANKING_ENABLED=True,
    TYPESAFE_DENIAL_TRIAGE_ENABLED=True,
)


def _letter_payload(model):
    answers = {
        q: {"type": "score", "score": 2.0, "confidence": 0.9}
        for q in letter_quality.SCORE_QUESTIONS
    }
    return {"model": model, "answers": answers, "usage": {"input_tokens": 900, "output_tokens": 20}}


def _score(session, **overrides):
    with override_settings(**{**_ENABLED, **overrides}), patch.object(
        typesafe.aiohttp, "ClientSession", session
    ):
        return asyncio.run(letter_quality.score_letter("The denial.", "Dear reviewer, the letter."))


class TestLetterScoringThroughAsk:
    def test_the_draft_goes_out_as_state_with_the_rubric_and_the_model(self):
        session = _FakeSession(payload=_letter_payload("jev-1.13.0"))
        score = _score(session)
        assert score is not None and score.quality == 1.0
        _, body, _ = session.posted[0]
        assert set(body) == {"state", "model", "questions"}
        assert body["state"].startswith("THE DENIAL:\nThe denial.")
        assert body["model"] == "jev-1.13.0"
        assert body["questions"] == letter_quality.QUESTIONS

    def test_the_scorer_records_the_model_the_response_names(self):
        score = _score(_FakeSession(payload=_letter_payload("jev-1.13.0")))
        assert score.scorer == f"typesafe/jev-1.13.0/rubric-{letter_quality.RUBRIC_VERSION}"

    def test_a_model_change_starts_a_new_series(self):
        """A repointed alias answers under a new versioned id, so its scores
        get their own scorer string and are never averaged into the old one."""
        old = _score(_FakeSession(payload=_letter_payload("jev-1.13.0")), TYPESAFE_MODEL="jev-latest")
        new = _score(_FakeSession(payload=_letter_payload("jev-1.14.0")), TYPESAFE_MODEL="jev-latest")
        assert old.scorer != new.scorer
        assert new.scorer == f"typesafe/jev-1.14.0/rubric-{letter_quality.RUBRIC_VERSION}"
        assert letter_quality.same_rubric(old.scorer) and letter_quality.same_rubric(new.scorer)

    def test_a_422_is_no_score_and_its_status_reaches_the_failure_hook(self):
        calls = []

        async def hook(summary):
            calls.append(summary)

        with override_settings(**_ENABLED), patch.object(
            typesafe.aiohttp, "ClientSession", _FakeSession(422)
        ):
            assert asyncio.run(letter_quality.score_letter("d", "letter", on_failure=hook)) is None
        assert calls == ["HTTP 422"]


class TestDenialTriageThroughAsk:
    LETTER = (
        "Your request for an MRI was denied as not medically necessary. "
        "Your written appeal must be received within 180 days of this notice."
    )

    def _payload(self, model):
        return {
            "model": model,
            "answers": {
                "category": {"type": "choice", "choice": "medical_necessity", "confidence": 0.85},
                "regulation": {"type": "choice", "choice": "employer_plan", "confidence": 0.6},
                "pre_service": {"type": "noul", "noul": 0.92},
                "urgent": {"type": "noul", "noul": 0.05},
                "deadline": {"type": "choice", "choice": "180 days from notice", "confidence": 0.9},
            },
            "usage": {"input_tokens": 400, "output_tokens": 30},
        }

    def test_the_letter_goes_out_as_state_and_the_source_names_the_answering_model(self):
        session = _FakeSession(payload=self._payload("jev-1.13.0"))
        with override_settings(**_ENABLED), patch.object(typesafe.aiohttp, "ClientSession", session):
            result = asyncio.run(denial_triage.triage(self.LETTER, datetime.date(2026, 9, 2)))
        assert result is not None and result.category == "medical_necessity"
        assert result.source == f"typesafe/jev-1.13.0/rubric-{denial_triage.RUBRIC_VERSION}"
        _, body, _ = session.posted[0]
        assert set(body) == {"state", "model", "questions"}
        assert body["state"] == self.LETTER
        assert body["model"] == "jev-1.13.0"
        assert {"category", "regulation", "pre_service", "urgent", "deadline"} <= set(body["questions"])

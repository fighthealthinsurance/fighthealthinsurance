"""ml/typesafe.py: the shared TypeSafe transport.

The request is the documented System One shape (docs.typesafe.ai/api): a
``state``, a ``model`` and the ``questions``, with the key as a Bearer token.
It carries the API key and the redacted text, so it only ever goes over https
and never follows a redirect. The one thing a caller may learn from a failed
call is the HTTP status: the body is never surfaced, because an error body
could quote the text back (it is read only to tell a credit or quota refusal).
A refused key, an unknown model, an unreachable endpoint or a streak of
connect timeouts holds every use off the wire for a cooldown. A spent budget
is a WARNING once per use and day.
"""

import asyncio
import datetime
import os
import socket
import ssl
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest
from django.conf import settings
from django.test import override_settings

from fighthealthinsurance.ml import denial_triage, letter_quality, spend, typesafe

SETTINGS = dict(
    TYPESAFE_API_KEY="test-key",
    TYPESAFE_API_URL="https://typesafe.invalid/v1/systemone",
    # Pinned, so a developer's environment cannot change the cooldown tests.
    FHI_TYPESAFE_COOLDOWN_SECONDS=900,
)


class _FakeResponse:
    def __init__(self, status, payload=None, body=""):
        self.status = status
        self.json_calls = 0
        self.text_calls = 0
        self.payload = {"answers": {}} if payload is None else payload
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        self.json_calls += 1
        return self.payload

    async def text(self, **kwargs):
        self.text_calls += 1
        return self.body


class _FakeSession:
    """Stands in for aiohttp.ClientSession: the constructor and the context.
    With ``error``, the post fails the way an unreachable endpoint does."""

    def __init__(self, status=200, payload=None, body="", error=None):
        self.response = _FakeResponse(status, payload, body)
        self.error = error
        self.posted = []
        self.opened = 0
        self.post_kwargs = {}
        self.session_kwargs = {}

    def __call__(self, *args, **kwargs):
        self.opened += 1
        self.session_kwargs = kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None, **kwargs):
        if self.error is not None:
            raise self.error
        self.posted.append((url, json, headers))
        self.post_kwargs = kwargs
        return self.response


QUESTIONS = {"is_urgent": {"type": "noul", "instructions": "Does this convey urgency?"}}


@pytest.fixture(autouse=True)
def _no_cooldown():
    """The cooldown is process-wide: no test inherits or leaves one."""
    typesafe.reset_cooldown_for_tests()
    yield
    typesafe.reset_cooldown_for_tests()


def _ask(session, state="doc", timeout_seconds=1, **overrides):
    conf = {**SETTINGS, **overrides}
    with override_settings(**conf), patch.object(typesafe.aiohttp, "ClientSession", session):
        return asyncio.run(typesafe.ask(state, QUESTIONS, timeout_seconds=timeout_seconds))


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
    assert session.response.text_calls == 0


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
    quoted = "state: the text quoted back"
    session = _FakeSession(status, payload={"detail": quoted}, body=quoted)
    with pytest.raises(typesafe.TypeSafeError) as info:
        _ask(session, state="Jane Q. Doe was denied")
    assert info.value.status == status
    assert str(info.value) == f"HTTP {status}"
    assert info.value.args == (f"HTTP {status}",)
    assert session.response.json_calls == 0


def _connector_error(error_class, os_error):
    """An aiohttp connect-phase error; the key only needs what its str() reads."""
    key = SimpleNamespace(host="typesafe.invalid", port=443, is_ssl=True, ssl=True)
    return error_class(key, os_error)


# The chat gate's budget after a slow identifier lookup ate most of it: a
# connect bound of 0.225s, too short to say anything about the host.
SQUEEZED_TIMEOUT_SECONDS = 0.3


def _connect_timeout(timeout_seconds=20):
    """One request whose connect times out (a host dropping our packets).
    By default under the letter and triage callers' 20s timeout, whose
    connect bound counts toward a streak; the fake fails at once, so nothing
    waits that long."""
    error = aiohttp.ConnectionTimeoutError("Connection timeout to host")
    with pytest.raises(aiohttp.ConnectionTimeoutError):
        _ask(_FakeSession(error=error), timeout_seconds=timeout_seconds)


class _Clock:
    """Stands in for the time module the cooldown reads."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


class TestCooldown:
    """A refused key, an unknown or retired model, or an endpoint that cannot
    be reached fails every request the same way, so every use is held off
    the wire for FHI_TYPESAFE_COOLDOWN_SECONDS."""

    @pytest.mark.parametrize("status", [401, 403, 404, 410])
    def test_a_refused_key_or_unknown_model_holds_the_next_request_back(self, status):
        with pytest.raises(typesafe.TypeSafeError):
            _ask(_FakeSession(status))
        session = _FakeSession()
        with pytest.raises(typesafe.TypeSafeCoolingDown):
            _ask(session)
        assert session.opened == 0 and session.posted == []

    @pytest.mark.parametrize(
        "error",
        [
            _connector_error(aiohttp.ClientConnectorError, ConnectionRefusedError(111, "refused")),
            _connector_error(aiohttp.ClientConnectorDNSError, socket.gaierror(-2, "not known")),
            _connector_error(aiohttp.ClientConnectorSSLError, ssl.SSLError(1, "handshake")),
        ],
        ids=["refused", "dns", "tls"],
    )
    def test_an_unreachable_endpoint_holds_the_next_request_back(self, error):
        with pytest.raises(aiohttp.ClientConnectorError):
            _ask(_FakeSession(error=error))
        session = _FakeSession()
        with pytest.raises(typesafe.TypeSafeCoolingDown):
            _ask(session)
        assert session.opened == 0 and session.posted == []

    def test_the_skip_carries_the_status_that_started_it(self):
        """So the status page keeps naming the cause during the cooldown."""
        with pytest.raises(typesafe.TypeSafeError):
            _ask(_FakeSession(401))
        with pytest.raises(typesafe.TypeSafeCoolingDown) as info:
            _ask(_FakeSession())
        assert letter_quality.failure_summary(info.value) == "HTTP 401"

    def test_the_skip_after_an_unreachable_endpoint_carries_no_status(self):
        error = _connector_error(aiohttp.ClientConnectorError, ConnectionRefusedError(111, "x"))
        with pytest.raises(aiohttp.ClientConnectorError):
            _ask(_FakeSession(error=error))
        with pytest.raises(typesafe.TypeSafeCoolingDown) as info:
            _ask(_FakeSession())
        assert info.value.status is None

    @pytest.mark.parametrize("status", [400, 422, 429, 503])
    def test_an_error_one_request_can_earn_starts_no_cooldown(self, status):
        with pytest.raises(typesafe.TypeSafeError):
            _ask(_FakeSession(status))
        session = _FakeSession()
        _ask(session)
        assert len(session.posted) == 1

    def test_it_ends_after_the_configured_window(self):
        clock = _Clock()
        window = dict(FHI_TYPESAFE_COOLDOWN_SECONDS=60)
        with patch.object(typesafe, "time", clock):
            with pytest.raises(typesafe.TypeSafeError):
                _ask(_FakeSession(404), **window)
            clock.now += 59
            with pytest.raises(typesafe.TypeSafeCoolingDown):
                _ask(_FakeSession(), **window)
            clock.now += 2
            session = _FakeSession()
            _ask(session, **window)
        assert len(session.posted) == 1

    def test_an_unreachable_endpoint_cools_for_less_than_a_refusal(self):
        """A failed connect is often a passing blip: it holds requests back
        for CONNECT_COOLDOWN_SECONDS, not the refusal window."""
        clock = _Clock()
        error = _connector_error(aiohttp.ClientConnectorError, ConnectionRefusedError(111, "x"))
        with patch.object(typesafe, "time", clock):
            with pytest.raises(aiohttp.ClientConnectorError):
                _ask(_FakeSession(error=error))
            clock.now += typesafe.CONNECT_COOLDOWN_SECONDS + 1
            session = _FakeSession()
            _ask(session)
        assert len(session.posted) == 1

    def test_one_connect_timeout_starts_no_cooldown(self):
        """Not definitive like a refusal: one lost packet can cause it."""
        _connect_timeout()
        session = _FakeSession()
        _ask(session)
        assert len(session.posted) == 1

    def test_a_second_connect_timeout_in_a_row_holds_the_next_request_back(self):
        """A host that drops our packets never completes the connect: nothing
        reached TypeSafe, as with a refused connection."""
        _connect_timeout()
        _connect_timeout()
        session = _FakeSession()
        with pytest.raises(typesafe.TypeSafeCoolingDown):
            _ask(session)
        assert session.opened == 0 and session.posted == []

    def test_an_answer_between_connect_timeouts_starts_the_streak_over(self):
        """Any HTTP status shows the connect works."""
        _connect_timeout()
        with pytest.raises(typesafe.TypeSafeError):
            _ask(_FakeSession(503))
        _connect_timeout()
        session = _FakeSession()
        _ask(session)
        assert len(session.posted) == 1

    def test_connect_timeouts_under_a_short_bound_start_no_cooldown(self):
        """A caller's leftover budget can be too short for a healthy host's
        handshake, so even two in a row say nothing about reaching it."""
        for _ in range(typesafe.CONNECT_TIMEOUT_STREAK):
            _connect_timeout(timeout_seconds=SQUEEZED_TIMEOUT_SECONDS)
        session = _FakeSession()
        _ask(session)
        assert len(session.posted) == 1

    def test_a_connect_timeout_under_a_short_bound_starts_no_streak(self):
        """The counted connect timeout after it is still the first of one."""
        _connect_timeout(timeout_seconds=SQUEEZED_TIMEOUT_SECONDS)
        _connect_timeout()
        session = _FakeSession()
        _ask(session)
        assert len(session.posted) == 1

    def test_connect_timeouts_further_apart_than_the_window_start_no_cooldown(self):
        clock = _Clock()
        with patch.object(typesafe, "time", clock):
            _connect_timeout()
            clock.now += typesafe.CONNECT_TIMEOUT_WINDOW_SECONDS + 1
            _connect_timeout()
            session = _FakeSession()
            _ask(session)
        assert len(session.posted) == 1

    def test_a_connect_timeout_streak_cools_for_the_connect_window(self):
        clock = _Clock()
        with patch.object(typesafe, "time", clock):
            _connect_timeout()
            _connect_timeout()
            clock.now += typesafe.CONNECT_COOLDOWN_SECONDS + 1
            session = _FakeSession()
            _ask(session)
        assert len(session.posted) == 1

    @pytest.mark.parametrize(
        "error",
        [
            asyncio.TimeoutError(),
            aiohttp.SocketTimeoutError("Timeout on reading data from socket"),
        ],
        ids=["request timeout", "read timeout"],
    )
    def test_a_timeout_after_connecting_starts_no_cooldown(self, error):
        """A slow answer from a host we reached is no reason to stop asking."""
        with pytest.raises(type(error)):
            _ask(_FakeSession(error=error))
        session = _FakeSession()
        _ask(session)
        assert len(session.posted) == 1

    def test_an_answer_ends_the_cooldown(self):
        """A request sent before the cooldown began that then gets a 200
        shows TypeSafe is back: the next request is sent."""
        with pytest.raises(typesafe.TypeSafeError):
            _ask(_FakeSession(401))
        # Sent before the cooldown began, so not held back; answered 200.
        with patch.object(typesafe, "_refuse_while_cooling"):
            _ask(_FakeSession())
        session = _FakeSession()
        _ask(session)
        assert len(session.posted) == 1

    def test_the_start_is_one_warning_and_each_skip_a_debug_line(self, log_capture):
        with log_capture() as cap:
            with pytest.raises(typesafe.TypeSafeError):
                _ask(_FakeSession(401))
            for _ in range(3):
                with pytest.raises(typesafe.TypeSafeCoolingDown):
                    _ask(_FakeSession())
        assert len([m for m in cap.messages("WARNING") if "TypeSafe" in m]) == 1
        assert len([m for m in cap.messages("DEBUG") if "not asked" in m]) == 3

    def test_the_window_comes_from_the_environment_when_not_set(self):
        with patch.dict(os.environ, {"FHI_TYPESAFE_COOLDOWN_SECONDS": "60"}), override_settings(
            FHI_TYPESAFE_COOLDOWN_SECONDS=None
        ):
            assert typesafe.cooldown_seconds() == 60.0

    @pytest.mark.parametrize("value", [None, "", "soon", "-5", "nan", "inf"])
    def test_an_unset_or_unusable_window_is_fifteen_minutes(self, value):
        with patch.dict(os.environ, {"FHI_TYPESAFE_COOLDOWN_SECONDS": ""}), override_settings(
            FHI_TYPESAFE_COOLDOWN_SECONDS=value
        ):
            assert typesafe.cooldown_seconds() == 900.0


class TestConnectBound:
    """The connect phase has its own bound inside the request's timeout, so a
    host that never completes the connect fails as a connect timeout (and is
    cooled down) before the request's timeout, or a caller's own wait_for of
    the same length, cuts the request off."""

    def _client_timeout(self, seconds):
        session = _FakeSession()
        _ask(session, timeout_seconds=seconds)
        return session.session_kwargs["timeout"]

    def test_a_short_request_timeout_leaves_room_after_the_connect(self):
        # The chat gate's default: it waits the same 1.5s itself.
        timeout = self._client_timeout(1.5)
        assert 0 < timeout.sock_connect < timeout.total == 1.5

    def test_a_long_request_timeout_caps_the_connect(self):
        assert self._client_timeout(20).sock_connect == typesafe.CONNECT_TIMEOUT_SECONDS

    @pytest.mark.parametrize("seconds", [1.5, 20])
    def test_the_whole_connect_has_the_same_bound_as_each_address(self, seconds):
        timeout = self._client_timeout(seconds)
        assert timeout.connect == timeout.sock_connect

    def test_a_host_that_never_completes_the_connect_is_cooled_down(self):
        """Through the real client: only the socket connect is replaced, by
        one that never completes. The URL is an address literal, so nothing
        is looked up, and were the patch to miss, the local refusal would
        fail this test rather than reach out. Two in a row cool. The counted
        floor is lowered so a 0.3s connect bound counts and nothing waits a
        full second."""

        async def never_connects(*args, **kwargs):
            await asyncio.Event().wait()

        conf = {**SETTINGS, "TYPESAFE_API_URL": "https://127.0.0.1:9/v1/systemone"}
        never = patch("aiohappyeyeballs.start_connection", never_connects)
        floor = patch.object(typesafe, "MIN_COUNTED_CONNECT_SECONDS", 0.1)
        with override_settings(**conf), never, floor:
            for _ in range(typesafe.CONNECT_TIMEOUT_STREAK):
                with pytest.raises(aiohttp.ConnectionTimeoutError):
                    asyncio.run(typesafe.ask("doc", QUESTIONS, timeout_seconds=0.4))
        assert typesafe.cooling_down() is True

    def test_a_host_with_two_addresses_that_never_connect_is_cooled_down(self):
        """api.typesafe.ai has two addresses, and aiohttp tries them in turn,
        each with its own socket connect bound: without a bound on the whole
        connect, the second try runs past the request's timeout, which never
        counts toward the cooldown. The lookup is replaced by two loopback
        addresses and the socket connect by one that never completes, so
        nothing is looked up or sent."""

        async def two_addresses(self, host, port, traces=None):
            return [
                {
                    "hostname": host,
                    "host": address,
                    "port": port,
                    "family": socket.AF_INET,
                    "proto": 0,
                    "flags": 0,
                }
                for address in ("127.0.0.1", "127.0.0.2")
            ]

        async def never_connects(*args, **kwargs):
            await asyncio.Event().wait()

        conf = {
            **SETTINGS,
            "TYPESAFE_API_URL": "https://typesafe.invalid:9/v1/systemone",
        }
        lookup = patch.object(aiohttp.TCPConnector, "_resolve_host", two_addresses)
        never = patch("aiohappyeyeballs.start_connection", never_connects)
        floor = patch.object(typesafe, "MIN_COUNTED_CONNECT_SECONDS", 0.1)
        with override_settings(**conf), lookup, never, floor:
            for _ in range(typesafe.CONNECT_TIMEOUT_STREAK):
                with pytest.raises(aiohttp.ConnectionTimeoutError):
                    asyncio.run(typesafe.ask("doc", QUESTIONS, timeout_seconds=0.4))
        assert typesafe.cooling_down() is True


class TestCoolingDownPredicate:
    """typesafe.cooling_down(): whether ask() would refuse before sending, for
    callers with work of their own to skip (the chat gate, shadow scoring)."""

    def test_false_with_no_cooldown(self):
        assert typesafe.cooling_down() is False

    def test_true_after_a_refusal(self):
        with pytest.raises(typesafe.TypeSafeError):
            _ask(_FakeSession(401))
        assert typesafe.cooling_down() is True

    def test_false_once_the_window_ends(self):
        clock = _Clock()
        with patch.object(typesafe, "time", clock):
            with pytest.raises(typesafe.TypeSafeError):
                _ask(_FakeSession(401), FHI_TYPESAFE_COOLDOWN_SECONDS=60)
            clock.now += 61
            assert typesafe.cooling_down() is False


class TestBudgetSpentLog:
    """A spent or paused budget refuses every request for the rest of the day
    or month: a WARNING once per use and UTC day, then debug lines."""

    def _refuse(self, use, times):
        with override_settings(**SETTINGS), patch.object(spend, "allows", return_value=False):
            for _ in range(times):
                with pytest.raises(typesafe.TypeSafeBudgetSpent):
                    asyncio.run(typesafe.ask("doc", QUESTIONS, timeout_seconds=1, use=use))

    @staticmethod
    def _warnings(cap):
        return [m for m in cap.messages("WARNING") if "TypeSafe not asked" in m]

    def test_the_first_refusal_for_a_use_is_the_one_warning(self, log_capture):
        with log_capture() as cap:
            self._refuse(spend.LETTERS, times=3)
        assert len(self._warnings(cap)) == 1

    def test_later_refusals_that_day_are_debug_lines(self, log_capture):
        with log_capture() as cap:
            self._refuse(spend.LETTERS, times=3)
        assert len([m for m in cap.messages("DEBUG") if "TypeSafe not asked" in m]) == 2

    def test_each_use_is_announced_on_its_own(self, log_capture):
        with log_capture() as cap:
            self._refuse(spend.LETTERS, times=2)
            self._refuse(spend.TRIAGE, times=2)
        assert len(self._warnings(cap)) == 2

    def test_a_use_last_announced_yesterday_is_announced_again(self, log_capture):
        today = datetime.datetime.now(datetime.timezone.utc).date()
        typesafe._budget_spent_logged[spend.LETTERS] = today - datetime.timedelta(days=1)
        with log_capture() as cap:
            self._refuse(spend.LETTERS, times=1)
        assert len(self._warnings(cap)) == 1


class TestAnnounced:
    """typesafe.announced(): the refusals whose cause was logged once when it
    began, which every feature then logs at debug rather than WARNING."""

    @pytest.mark.parametrize(
        "error",
        [
            typesafe.TypeSafeCoolingDown("cooling down", status=401),
            typesafe.TypeSafeBudgetSpent("budget spent"),
        ],
    )
    def test_a_cooldown_or_a_spent_budget_was_announced(self, error):
        assert typesafe.announced(error) is True

    @pytest.mark.parametrize(
        "error",
        [
            typesafe.TypeSafeError("HTTP 503", status=503),
            aiohttp.ConnectionTimeoutError("Connection timeout to host"),
            asyncio.TimeoutError(),
        ],
    )
    def test_any_other_failure_was_not(self, error):
        assert typesafe.announced(error) is False


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

"""Sentry's before_send hooks decide what reaches production error tracking.

Two properties matter and neither is obvious from reading the filters:

1. They must never raise. sentry-sdk wraps ``before_send`` in
   ``capture_internal_exceptions()``, so an exception inside a filter does not
   surface anywhere -- it silently DISCARDS the event. A malformed payload
   would therefore stop real errors from reaching Sentry with no signal at all,
   which is why the shape tests below feed in the nulls and non-dicts that
   ``event.get("exception", {}).get("values", [])`` used to trip over.
2. ``before_send_transaction_filter`` must keep routed traffic. It drops
   unrouted-path transactions (the scanner-probe noise), and if its check ever
   over-matched it would silently disable tracing for the whole site.
"""

import pytest

from fighthealthinsurance.sentry_filters import (
    UNROUTED_TRANSACTION_SOURCE,
    before_send_filter,
    before_send_transaction_filter,
    exception_values,
)


def _exc_event(exc_type: str, value: str) -> dict:
    return {"exception": {"values": [{"type": exc_type, "value": value}]}}


def _transaction(source, name="/appeal/"):
    return {
        "type": "transaction",
        "transaction": name,
        "transaction_info": {"source": source},
    }


class TestRayNoiseIsDropped:
    @pytest.mark.parametrize(
        "message",
        [
            "Logstream proxy failed to connect",
            "ray: Unrecoverable error in data channel, shutting down",
        ],
    )
    def test_ray_message_is_dropped(self, message):
        assert before_send_filter({"message": message}, {}) is None

    def test_ray_grpc_logstream_exception_is_dropped(self):
        event = _exc_event(
            "ConnectionError", "Logstream proxy failed to connect after 5 retries"
        )
        assert before_send_filter(event, {}) is None

    def test_ray_grpc_channel_exception_is_dropped(self):
        event = _exc_event(
            "RpcError", "grpc_status:5 ... Channel for client abc123 not found"
        )
        assert before_send_filter(event, {}) is None

    def test_grpc_status_alone_is_kept(self):
        """Both halves are required -- grpc_status:5 shows up elsewhere."""
        event = _exc_event("RpcError", "grpc_status:5 while calling the ML backend")
        assert before_send_filter(event, {}) is event


class TestUnroutedWebsocketIsDropped:
    """Channels raises ValueError for a websocket path matching no route; it
    escapes the ASGI app and uvicorn logs it at ERROR, so it becomes an event
    fingerprinted by the client-supplied path -- one new issue per probe."""

    def test_unrouted_websocket_exception_is_dropped(self):
        event = _exc_event("ValueError", "No route found for path 'ws/admin'.")
        assert before_send_filter(event, {}) is None

    def test_unrouted_websocket_log_message_is_dropped(self):
        event = {"message": "No route found for path 'ws/wp-login.php'."}
        assert before_send_filter(event, {}) is None

    def test_other_value_errors_are_kept(self):
        """Narrow on purpose: a ValueError from inside a consumer is a bug."""
        event = _exc_event("ValueError", "invalid literal for int() with base 10")
        assert before_send_filter(event, {}) is event

    def test_matching_text_under_another_type_is_kept(self):
        event = _exc_event("RuntimeError", "No route found for path 'ws/x'.")
        assert before_send_filter(event, {}) is event


class TestOrdinaryErrorsAreKept:
    def test_application_exception_is_kept(self):
        event = _exc_event("OperationalError", "could not connect to server")
        assert before_send_filter(event, {}) is event

    def test_message_only_event_is_kept(self):
        event = {"message": "appeal generation produced no text"}
        assert before_send_filter(event, {}) is event

    def test_empty_event_is_kept(self):
        event = {}
        assert before_send_filter(event, {}) is event


class TestMalformedPayloadsDoNotDropEvents:
    """A raise inside before_send is swallowed by sentry-sdk and the event is
    discarded, so every one of these must return the event, not blow up."""

    @pytest.mark.parametrize(
        "event",
        [
            {"exception": None},
            {"exception": {"values": None}},
            {"exception": {}},
            {"exception": {"values": []}},
            {"exception": {"values": [None]}},
            {"exception": {"values": ["[Filtered]"]}},
            {"exception": {"values": [{"type": None, "value": None}]}},
            {"message": None},
        ],
    )
    def test_malformed_event_is_kept(self, event):
        assert before_send_filter(event, {}) is event

    def test_noise_is_still_found_beside_a_malformed_entry(self):
        event = {
            "exception": {
                "values": [
                    "[Filtered]",
                    {
                        "type": "ConnectionError",
                        "value": "Logstream proxy failed to connect",
                    },
                ]
            }
        }
        assert before_send_filter(event, {}) is None


class TestExceptionValues:
    def test_missing_key_yields_empty_list(self):
        assert exception_values({}) == []

    def test_null_exception_yields_empty_list(self):
        assert exception_values({"exception": None}) == []

    def test_null_values_yields_empty_list(self):
        assert exception_values({"exception": {"values": None}}) == []

    def test_non_dict_entries_are_dropped(self):
        event = {"exception": {"values": [None, "[Filtered]", {"type": "ValueError"}]}}
        assert exception_values(event) == [{"type": "ValueError"}]


class TestTransactionFilter:
    def test_unrouted_transaction_is_dropped(self):
        event = _transaction(UNROUTED_TRANSACTION_SOURCE, name="/.env")
        assert before_send_transaction_filter(event, {}) is None

    @pytest.mark.parametrize("source", ["route", "view", "component", "custom", "task"])
    def test_routed_transaction_is_kept(self, source):
        """The guard against silently disabling tracing site-wide: anything
        that matched a route must survive."""
        event = _transaction(source)
        assert before_send_transaction_filter(event, {}) is event

    @pytest.mark.parametrize(
        "event",
        [
            {"type": "transaction", "transaction": "/appeal/"},
            {"type": "transaction", "transaction_info": None},
            {"type": "transaction", "transaction_info": {}},
            {},
        ],
    )
    def test_transaction_without_a_source_is_kept(self, event):
        assert before_send_transaction_filter(event, {}) is event

    def test_unrouted_source_constant_matches_sentry(self):
        """sentry-sdk labels the raw-path fallback TransactionSource.URL."""
        assert UNROUTED_TRANSACTION_SOURCE == "url"

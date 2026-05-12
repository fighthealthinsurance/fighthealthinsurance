"""Tests for the optional Microsoft Azure Log Analytics integration."""

import base64
import binascii
import json
import logging
from unittest import mock

from django.test import TestCase, override_settings

from fighthealthinsurance import log_analytics

# A valid base64-encoded fake key (decodes to 32 bytes); the workspace ID is
# a syntactically valid GUID. Neither is a real Azure credential.
FAKE_WORKSPACE_ID = "11111111-2222-3333-4444-555555555555"
FAKE_WORKSPACE_KEY = base64.b64encode(b"x" * 32).decode("ascii")


def _make_record(
    name: str = "t",
    level: int = logging.INFO,
    msg: str = "hello",
    args=None,
    exc_info=None,
    lineno: int = 1,
) -> logging.LogRecord:
    """Minimal LogRecord factory for handler tests."""
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=lineno,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )


def _drain():
    """Block until the module-level send queue has been fully processed."""
    log_analytics._send_queue.join()


class LogAnalyticsDisabledByDefaultTest(TestCase):
    """The integration must default off so an unconfigured deploy ships nothing."""

    @override_settings(LOG_ANALYTICS_WORKSPACE_ID="", LOG_ANALYTICS_WORKSPACE_KEY="")
    def test_disabled_when_settings_blank(self):
        self.assertFalse(log_analytics.is_log_analytics_enabled())

    @override_settings(
        LOG_ANALYTICS_WORKSPACE_ID=FAKE_WORKSPACE_ID,
        LOG_ANALYTICS_WORKSPACE_KEY="",
    )
    def test_disabled_when_only_workspace_id_set(self):
        self.assertFalse(log_analytics.is_log_analytics_enabled())

    @override_settings(
        LOG_ANALYTICS_WORKSPACE_ID="",
        LOG_ANALYTICS_WORKSPACE_KEY=FAKE_WORKSPACE_KEY,
    )
    def test_disabled_when_only_key_set(self):
        self.assertFalse(log_analytics.is_log_analytics_enabled())

    @override_settings(LOG_ANALYTICS_WORKSPACE_ID="", LOG_ANALYTICS_WORKSPACE_KEY="")
    def test_post_log_no_op_when_disabled(self):
        with mock.patch.object(log_analytics, "_get_session") as gs:
            sent = log_analytics.post_log({"message": "hi"})
        self.assertFalse(sent)
        gs.assert_not_called()

    @override_settings(LOG_ANALYTICS_WORKSPACE_ID="", LOG_ANALYTICS_WORKSPACE_KEY="")
    def test_handler_is_silent_when_disabled(self):
        handler = log_analytics.LogAnalyticsHandler()
        with mock.patch.object(log_analytics, "_post_log_sync") as p:
            handler.emit(_make_record())
            _drain()
        p.assert_not_called()


@override_settings(
    LOG_ANALYTICS_WORKSPACE_ID=FAKE_WORKSPACE_ID,
    LOG_ANALYTICS_WORKSPACE_KEY=FAKE_WORKSPACE_KEY,
    LOG_ANALYTICS_LOG_TYPE="TestTable",
)
class LogAnalyticsEnabledTest(TestCase):
    """When configured, helpers must produce a valid HTTP Data Collector call."""

    def setUp(self):
        # Make sure no items leak in from earlier tests.
        _drain()

    def test_is_enabled(self):
        self.assertTrue(log_analytics.is_log_analytics_enabled())

    def test_signature_is_deterministic_for_fixed_inputs(self):
        sig_a = log_analytics._build_signature(
            FAKE_WORKSPACE_ID,
            FAKE_WORKSPACE_KEY,
            "Mon, 01 Jan 2024 00:00:00 GMT",
            42,
        )
        sig_b = log_analytics._build_signature(
            FAKE_WORKSPACE_ID,
            FAKE_WORKSPACE_KEY,
            "Mon, 01 Jan 2024 00:00:00 GMT",
            42,
        )
        self.assertEqual(sig_a, sig_b)
        self.assertTrue(sig_a.startswith(f"SharedKey {FAKE_WORKSPACE_ID}:"))

    def test_signature_rejects_invalid_base64_strictly(self):
        with self.assertRaises(binascii.Error):
            log_analytics._build_signature(
                FAKE_WORKSPACE_ID, "!!!not-base64!!!", "x", 0
            )

    def test_post_log_sends_expected_payload(self):
        fake_response = mock.Mock(status_code=200)
        fake_session = mock.Mock()
        fake_session.post.return_value = fake_response
        with mock.patch.object(
            log_analytics, "_get_session", return_value=fake_session
        ):
            ok = log_analytics.post_log([{"message": "hello", "level": "INFO"}])

        self.assertTrue(ok)
        fake_session.post.assert_called_once()
        call = fake_session.post.call_args
        url = call.args[0] if call.args else call.kwargs["url"]
        self.assertIn(FAKE_WORKSPACE_ID, url)
        self.assertIn("ods.opinsights.azure.com", url)

        headers = call.kwargs["headers"]
        self.assertEqual(headers["Log-Type"], "TestTable")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertTrue(headers["Authorization"].startswith("SharedKey "))
        self.assertIn("x-ms-date", headers)

        body = json.loads(call.kwargs["data"])
        self.assertEqual(body[0]["message"], "hello")

    def test_post_log_returns_false_on_http_error(self):
        fake_response = mock.Mock(status_code=403)
        fake_session = mock.Mock()
        fake_session.post.return_value = fake_response
        with mock.patch.object(
            log_analytics, "_get_session", return_value=fake_session
        ):
            ok = log_analytics.post_log([{"message": "x"}])
        self.assertFalse(ok)

    def test_post_log_returns_false_on_request_exception(self):
        fake_session = mock.Mock()
        fake_session.post.side_effect = (
            log_analytics.requests.exceptions.ConnectTimeout()
        )
        with mock.patch.object(
            log_analytics, "_get_session", return_value=fake_session
        ):
            ok = log_analytics.post_log([{"message": "x"}])
        self.assertFalse(ok)

    def test_post_log_returns_false_when_key_invalid_base64(self):
        with override_settings(LOG_ANALYTICS_WORKSPACE_KEY="!!!not-base64!!!"):
            with mock.patch.object(log_analytics, "_get_session") as gs:
                ok = log_analytics.post_log([{"message": "x"}])
        self.assertFalse(ok)
        gs.assert_not_called()

    def test_handler_emits_record_to_workspace(self):
        handler = log_analytics.LogAnalyticsHandler()
        record = _make_record(
            name="myapp",
            level=logging.WARNING,
            msg="something broke: %s",
            args=("oops",),
            lineno=42,
        )
        with mock.patch.object(
            log_analytics, "_post_log_sync", return_value=True
        ) as ship:
            handler.emit(record)
            _drain()

        ship.assert_called_once()
        body = ship.call_args.args[0]
        self.assertEqual(len(body), 1)
        entry = body[0]
        self.assertEqual(entry["level"], "WARNING")
        self.assertEqual(entry["logger"], "myapp")
        self.assertEqual(entry["message"], "something broke: oops")
        self.assertEqual(entry["line_number"], 42)

    def test_handler_includes_exception_info(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            exc_info = sys.exc_info()
        handler = log_analytics.LogAnalyticsHandler()
        record = _make_record(level=logging.ERROR, msg="failed", exc_info=exc_info)

        with mock.patch.object(
            log_analytics, "_post_log_sync", return_value=True
        ) as ship:
            handler.emit(record)
            _drain()

        body = ship.call_args.args[0]
        self.assertIn("exception", body[0])
        self.assertIn("ValueError", body[0]["exception"])

    def test_handler_swallows_post_failures(self):
        handler = log_analytics.LogAnalyticsHandler()
        with mock.patch.object(
            log_analytics, "_post_log_sync", side_effect=RuntimeError("network down")
        ):
            # Must not raise -- failures inside the worker must never crash callers.
            handler.emit(_make_record())
            _drain()

    def test_handler_emit_does_not_block_on_network(self):
        """emit() must enqueue and return immediately; the network call runs
        on the worker thread."""
        import threading
        import time

        slow = threading.Event()

        def _slow_ship(*_args, **_kwargs):
            # Hold the worker so we can verify emit() didn't wait on this.
            slow.wait(timeout=2.0)
            return True

        handler = log_analytics.LogAnalyticsHandler()
        with mock.patch.object(log_analytics, "_post_log_sync", side_effect=_slow_ship):
            start = time.monotonic()
            handler.emit(_make_record())
            elapsed = time.monotonic() - start
            # emit() should return promptly -- well under the worker hold time.
            self.assertLess(elapsed, 0.2)
            slow.set()
            _drain()

    def test_handler_via_loguru_sink_ships_exactly_once(self):
        """Installing the handler as a loguru sink must not double-ship a
        single record (regression guard for the asgi.py wiring)."""
        from loguru import logger as loguru_logger

        handler = log_analytics.LogAnalyticsHandler(level=logging.INFO)
        sink_id = loguru_logger.add(handler, level="INFO")
        try:
            with mock.patch.object(
                log_analytics, "_post_log_sync", return_value=True
            ) as ship:
                loguru_logger.bind(test=True).info("hello via loguru")
                _drain()
            self.assertEqual(ship.call_count, 1)
        finally:
            loguru_logger.remove(sink_id)

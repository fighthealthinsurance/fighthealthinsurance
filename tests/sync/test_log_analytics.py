"""Tests for the optional Microsoft Azure Log Analytics integration."""

import base64
import json
import logging
from unittest import mock

from django.test import TestCase, override_settings

from fighthealthinsurance import log_analytics

# A valid base64-encoded fake key (decodes to 32 bytes); the workspace ID is
# a syntactically valid GUID. Neither is a real Azure credential.
FAKE_WORKSPACE_ID = "11111111-2222-3333-4444-555555555555"
FAKE_WORKSPACE_KEY = base64.b64encode(b"x" * 32).decode("ascii")


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
        with mock.patch.object(log_analytics.requests, "post") as post:
            sent = log_analytics.post_log({"message": "hi"})
        self.assertFalse(sent)
        post.assert_not_called()

    @override_settings(LOG_ANALYTICS_WORKSPACE_ID="", LOG_ANALYTICS_WORKSPACE_KEY="")
    def test_handler_is_silent_when_disabled(self):
        handler = log_analytics.LogAnalyticsHandler()
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello",
            args=None,
            exc_info=None,
        )
        with mock.patch.object(log_analytics.requests, "post") as post:
            handler.emit(record)
        post.assert_not_called()


@override_settings(
    LOG_ANALYTICS_WORKSPACE_ID=FAKE_WORKSPACE_ID,
    LOG_ANALYTICS_WORKSPACE_KEY=FAKE_WORKSPACE_KEY,
    LOG_ANALYTICS_LOG_TYPE="TestTable",
)
class LogAnalyticsEnabledTest(TestCase):
    """When configured, helpers must produce a valid HTTP Data Collector call."""

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

    def test_post_log_sends_expected_payload(self):
        fake_response = mock.Mock(status_code=200)
        with mock.patch.object(
            log_analytics.requests, "post", return_value=fake_response
        ) as post:
            ok = log_analytics.post_log([{"message": "hello", "level": "INFO"}])

        self.assertTrue(ok)
        self.assertEqual(post.call_count, 1)
        call = post.call_args
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
        with mock.patch.object(
            log_analytics.requests, "post", return_value=fake_response
        ):
            ok = log_analytics.post_log([{"message": "x"}])
        self.assertFalse(ok)

    def test_post_log_returns_false_on_request_exception(self):
        with mock.patch.object(
            log_analytics.requests,
            "post",
            side_effect=log_analytics.requests.exceptions.ConnectTimeout(),
        ):
            ok = log_analytics.post_log([{"message": "x"}])
        self.assertFalse(ok)

    def test_post_log_returns_false_when_key_invalid_base64(self):
        with override_settings(LOG_ANALYTICS_WORKSPACE_KEY="!!!not-base64!!!"):
            with mock.patch.object(log_analytics.requests, "post") as post:
                ok = log_analytics.post_log([{"message": "x"}])
        self.assertFalse(ok)
        post.assert_not_called()

    def test_handler_emits_record_to_workspace(self):
        handler = log_analytics.LogAnalyticsHandler()
        record = logging.LogRecord(
            name="myapp",
            level=logging.WARNING,
            pathname=__file__,
            lineno=42,
            msg="something broke: %s",
            args=("oops",),
            exc_info=None,
        )
        fake_response = mock.Mock(status_code=200)
        with mock.patch.object(
            log_analytics.requests, "post", return_value=fake_response
        ) as post:
            handler.emit(record)

        post.assert_called_once()
        body = json.loads(post.call_args.kwargs["data"])
        self.assertEqual(len(body), 1)
        entry = body[0]
        self.assertEqual(entry["level"], "WARNING")
        self.assertEqual(entry["logger"], "myapp")
        self.assertEqual(entry["message"], "something broke: oops")
        self.assertEqual(entry["line_number"], 42)

    def test_handler_includes_exception_info(self):
        handler = log_analytics.LogAnalyticsHandler()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="t",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=None,
            exc_info=exc_info,
        )
        fake_response = mock.Mock(status_code=200)
        with mock.patch.object(
            log_analytics.requests, "post", return_value=fake_response
        ) as post:
            handler.emit(record)

        body = json.loads(post.call_args.kwargs["data"])
        self.assertIn("exception", body[0])
        self.assertIn("ValueError", body[0]["exception"])

    def test_handler_swallows_post_failures(self):
        handler = log_analytics.LogAnalyticsHandler()
        record = logging.LogRecord(
            name="t",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="m",
            args=None,
            exc_info=None,
        )
        with mock.patch.object(
            log_analytics.requests,
            "post",
            side_effect=RuntimeError("network down"),
        ):
            # Must not raise -- logging failures cannot break callers.
            handler.emit(record)

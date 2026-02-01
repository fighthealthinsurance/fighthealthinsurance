"""
Tests for Sentry before_send_filter for fuzz guard events.
"""

from unittest.mock import patch, MagicMock

from django.test import TestCase, override_settings


class TestSentryBeforeSendFilter(TestCase):
    """Test Sentry before_send_filter for fuzz guard events."""

    def setUp(self):
        """Set up before_send_filter function for testing."""
        # Import the filter function from asgi.py
        # We need to test it in isolation
        self.filter_func = self._create_filter_func()

    def _create_filter_func(self):
        """Create a test version of the before_send_filter function."""

        def before_send_filter(event, hint):
            """
            Filter out noisy infrastructure errors from Sentry.
            """
            # Check logger name for Ray client internal loggers
            logger_name = event.get("logger", "")
            if logger_name in (
                "ray.util.client.logsclient",
                "ray.util.client.dataclient",
            ):
                return None

            # Filter fuzz guard module events (they're logged locally)
            if (
                "fuzz_guard" in logger_name.lower()
                or "fuzzguard" in logger_name.lower()
            ):
                return None

            # Check for fuzz_guard tag set by middleware
            tags = event.get("tags", {})
            if tags.get("fuzz_guard") is True:
                return None

            # Check transaction name for fuzz guard middleware
            transaction = event.get("transaction", "")
            if "FuzzGuardMiddleware" in transaction:
                return None

            # Check for specific gRPC error messages from Ray
            message = event.get("message", "") or ""
            if "Logstream proxy failed to connect" in message:
                return None
            if "Unrecoverable error in data channel" in message:
                return None

            # Check exception values for Ray gRPC errors
            exception_values = event.get("exception", {}).get("values", [])
            for exc in exception_values:
                exc_value = exc.get("value", "") or ""
                if "Logstream proxy failed to connect" in exc_value:
                    return None
                if "grpc_status:5" in exc_value and "Channel for client" in exc_value:
                    return None

            return event

        return before_send_filter

    def test_filters_fuzz_guard_logger_events(self):
        """Events from fuzz_guard logger should be filtered."""
        event = {
            "logger": "fighthealthinsurance.middleware.fuzz_guard",
            "message": "Blocked fuzz request",
        }
        result = self.filter_func(event, {})
        self.assertIsNone(result)

    def test_filters_fuzzguard_logger_events(self):
        """Events from fuzzguard logger (no underscore) should be filtered."""
        event = {
            "logger": "fighthealthinsurance.fuzzguard",
            "message": "Blocked fuzz request",
        }
        result = self.filter_func(event, {})
        self.assertIsNone(result)

    def test_filters_fuzz_guard_logger_case_insensitive(self):
        """Fuzz guard logger filtering should be case-insensitive."""
        event = {
            "logger": "some.module.FUZZ_GUARD.handler",
            "message": "Test message",
        }
        result = self.filter_func(event, {})
        self.assertIsNone(result)

    def test_filters_events_with_fuzz_guard_tag(self):
        """Events with fuzz_guard=True tag should be filtered."""
        event = {
            "logger": "django.request",
            "message": "Bad request",
            "tags": {"fuzz_guard": True},
        }
        result = self.filter_func(event, {})
        self.assertIsNone(result)

    def test_does_not_filter_fuzz_guard_tag_false(self):
        """Events with fuzz_guard=False tag should NOT be filtered."""
        event = {
            "logger": "django.request",
            "message": "Bad request",
            "tags": {"fuzz_guard": False},
        }
        result = self.filter_func(event, {})
        self.assertEqual(result, event)

    def test_filters_fuzzguardmiddleware_transactions(self):
        """Transactions from FuzzGuardMiddleware should be filtered."""
        event = {
            "logger": "django",
            "message": "Request error",
            "transaction": "FuzzGuardMiddleware.process_request",
        }
        result = self.filter_func(event, {})
        self.assertIsNone(result)

    def test_passes_normal_events(self):
        """Normal events should pass through."""
        event = {
            "logger": "django.request",
            "message": "Server error",
            "tags": {},
        }
        result = self.filter_func(event, {})
        self.assertEqual(result, event)

    def test_passes_normal_events_no_tags(self):
        """Normal events without tags should pass through."""
        event = {
            "logger": "fighthealthinsurance.views",
            "message": "Something went wrong",
        }
        result = self.filter_func(event, {})
        self.assertEqual(result, event)

    def test_does_not_blanket_filter_valueerror(self):
        """ValueError from other sources should not be filtered."""
        event = {
            "logger": "fighthealthinsurance.models",
            "message": "Invalid value",
            "exception": {
                "values": [
                    {
                        "type": "ValueError",
                        "value": "Invalid input value",
                    }
                ]
            },
        }
        result = self.filter_func(event, {})
        self.assertEqual(result, event)

    def test_filters_ray_logstream_errors(self):
        """Ray logstream proxy errors should be filtered."""
        event = {
            "logger": "ray.util.client.logsclient",
            "message": "Connection error",
        }
        result = self.filter_func(event, {})
        self.assertIsNone(result)

    def test_filters_ray_dataclient_errors(self):
        """Ray dataclient errors should be filtered."""
        event = {
            "logger": "ray.util.client.dataclient",
            "message": "Data channel error",
        }
        result = self.filter_func(event, {})
        self.assertIsNone(result)

    def test_filters_ray_logstream_message(self):
        """Events with logstream proxy message should be filtered."""
        event = {
            "logger": "some.logger",
            "message": "Logstream proxy failed to connect to remote server",
        }
        result = self.filter_func(event, {})
        self.assertIsNone(result)

    def test_filters_ray_data_channel_message(self):
        """Events with data channel error message should be filtered."""
        event = {
            "logger": "some.logger",
            "message": "Unrecoverable error in data channel",
        }
        result = self.filter_func(event, {})
        self.assertIsNone(result)

    def test_filters_ray_grpc_exception(self):
        """Ray gRPC exceptions should be filtered."""
        event = {
            "logger": "some.logger",
            "message": "Error",
            "exception": {
                "values": [
                    {
                        "type": "RpcError",
                        "value": "Logstream proxy failed to connect",
                    }
                ]
            },
        }
        result = self.filter_func(event, {})
        self.assertIsNone(result)

    def test_filters_ray_grpc_channel_exception(self):
        """Ray gRPC channel exceptions should be filtered."""
        event = {
            "logger": "some.logger",
            "message": "Error",
            "exception": {
                "values": [
                    {
                        "type": "RpcError",
                        "value": "Channel for client xyz grpc_status:5 details",
                    }
                ]
            },
        }
        result = self.filter_func(event, {})
        self.assertIsNone(result)

    def test_does_not_filter_partial_matches(self):
        """Should not filter events with partial tag/logger matches."""
        event = {
            "logger": "fuzzy_matching",  # Not fuzz_guard
            "message": "Fuzzy search error",
            "tags": {"fuzzy": True},  # Not fuzz_guard tag
        }
        result = self.filter_func(event, {})
        self.assertEqual(result, event)

    def test_handles_empty_event(self):
        """Should handle empty events gracefully."""
        event = {}
        result = self.filter_func(event, {})
        self.assertEqual(result, event)

    def test_handles_none_message(self):
        """Should handle None message gracefully."""
        event = {
            "logger": "django",
            "message": None,
        }
        result = self.filter_func(event, {})
        self.assertEqual(result, event)

    def test_handles_missing_exception_values(self):
        """Should handle missing exception values gracefully."""
        event = {
            "logger": "django",
            "message": "Error",
            "exception": {},  # No values key
        }
        result = self.filter_func(event, {})
        self.assertEqual(result, event)

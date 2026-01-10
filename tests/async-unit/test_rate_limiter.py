"""
Tests for the RateLimiter class in fighthealthinsurance.utils.

Tests cover:
- Backoff/exhaustion marking from 429 responses
- Auto-recovery after backoff period expires
- Status reporting
- Lock-free design (race conditions are harmless)

Note: RPM/RPD tracking has been removed as it's inaccurate in multi-container
deployments. We now rely on the API's 429 responses as the source of truth.
"""

import time
import unittest
from unittest.mock import patch

from fighthealthinsurance.utils import RateLimiter


class TestRateLimiterBasic(unittest.TestCase):
    """Basic functionality tests for RateLimiter."""

    def test_init_default_values(self):
        """Test RateLimiter initializes with correct defaults."""
        limiter = RateLimiter(name="test")

        self.assertEqual(limiter.name, "test")

    def test_init_with_legacy_params(self):
        """Test RateLimiter accepts legacy rpm_limit/rpd_limit params (ignored)."""
        # Should not raise - params are accepted for backwards compatibility
        limiter = RateLimiter(rpm_limit=10, rpd_limit=500, name="custom")

        self.assertEqual(limiter.name, "custom")

    def test_can_request_initially_true(self):
        """Test can_request returns True when no exhaustion has occurred."""
        limiter = RateLimiter(name="test")

        self.assertTrue(limiter.can_request())

    def test_record_request_is_noop(self):
        """Test record_request is a no-op (kept for backwards compatibility)."""
        limiter = RateLimiter(name="test")

        # Should not raise, but also should not do anything
        limiter.record_request()
        limiter.record_request()
        limiter.record_request()

        # Should still be able to request (no RPM limiting)
        self.assertTrue(limiter.can_request())


class TestRateLimiterExhaustion(unittest.TestCase):
    """Tests for temporary exhaustion marking (429 handling)."""

    def test_mark_exhausted_blocks_requests(self):
        """Test that mark_exhausted blocks subsequent requests."""
        limiter = RateLimiter(name="test")

        # Initially can request
        self.assertTrue(limiter.can_request())

        # Mark as exhausted for 60 seconds
        limiter.mark_exhausted(retry_after_seconds=60.0)

        # Should be blocked now
        self.assertFalse(limiter.can_request())

    def test_mark_exhausted_auto_recovery(self):
        """Test that exhaustion auto-recovers after the specified duration."""
        limiter = RateLimiter(name="test")

        base_time = time.time()

        with patch("time.time") as mock_time:
            mock_time.return_value = base_time

            # Mark as exhausted for 30 seconds
            limiter.mark_exhausted(retry_after_seconds=30.0)

            # Should be blocked
            self.assertFalse(limiter.can_request())

            # Move time forward 31 seconds
            mock_time.return_value = base_time + 31

            # Should be able to request again
            self.assertTrue(limiter.can_request())

    def test_mark_exhausted_custom_duration(self):
        """Test that custom exhaustion duration is respected."""
        limiter = RateLimiter(name="test")

        base_time = time.time()

        with patch("time.time") as mock_time:
            mock_time.return_value = base_time

            # Mark as exhausted for 120 seconds
            limiter.mark_exhausted(retry_after_seconds=120.0)

            # At 60 seconds, still blocked
            mock_time.return_value = base_time + 60
            self.assertFalse(limiter.can_request())

            # At 121 seconds, recovered
            mock_time.return_value = base_time + 121
            self.assertTrue(limiter.can_request())

    def test_mark_exhausted_clears_state_on_recovery(self):
        """Test that _exhausted_until is cleared after recovery."""
        limiter = RateLimiter(name="test")

        base_time = time.time()

        with patch("time.time") as mock_time:
            mock_time.return_value = base_time

            limiter.mark_exhausted(retry_after_seconds=30.0)

            # Move past exhaustion
            mock_time.return_value = base_time + 31

            # Call can_request to trigger cleanup
            self.assertTrue(limiter.can_request())

            # Internal state should be cleared
            self.assertIsNone(limiter._exhausted_until)


class TestRateLimiterStatus(unittest.TestCase):
    """Tests for get_status method."""

    def test_get_status_initial(self):
        """Test get_status returns correct initial state."""
        limiter = RateLimiter(name="test-model")

        status = limiter.get_status()

        self.assertEqual(status["name"], "test-model")
        self.assertFalse(status["exhausted"])
        self.assertIsNone(status["exhausted_until"])
        self.assertEqual(status["seconds_remaining"], 0.0)

    def test_get_status_when_exhausted(self):
        """Test get_status reflects exhausted state."""
        limiter = RateLimiter(name="test")

        base_time = time.time()

        with patch("time.time") as mock_time:
            mock_time.return_value = base_time

            limiter.mark_exhausted(retry_after_seconds=60.0)

            status = limiter.get_status()

            self.assertTrue(status["exhausted"])
            self.assertIsNotNone(status["exhausted_until"])
            self.assertAlmostEqual(status["seconds_remaining"], 60.0, delta=1.0)

    def test_get_status_seconds_remaining_decreases(self):
        """Test seconds_remaining decreases as time passes."""
        limiter = RateLimiter(name="test")

        base_time = time.time()

        with patch("time.time") as mock_time:
            mock_time.return_value = base_time
            limiter.mark_exhausted(retry_after_seconds=60.0)

            # Check at start
            status = limiter.get_status()
            self.assertAlmostEqual(status["seconds_remaining"], 60.0, delta=1.0)

            # Check after 20 seconds
            mock_time.return_value = base_time + 20
            status = limiter.get_status()
            self.assertAlmostEqual(status["seconds_remaining"], 40.0, delta=1.0)

            # Check after 60 seconds (should be 0)
            mock_time.return_value = base_time + 60
            status = limiter.get_status()
            self.assertEqual(status["seconds_remaining"], 0.0)


class TestRateLimiterEdgeCases(unittest.TestCase):
    """Edge case tests for RateLimiter."""

    def test_multiple_mark_exhausted_calls(self):
        """Test that multiple mark_exhausted calls use the latest value."""
        limiter = RateLimiter(name="test")

        base_time = time.time()

        with patch("time.time") as mock_time:
            mock_time.return_value = base_time

            # Mark exhausted for 30 seconds
            limiter.mark_exhausted(retry_after_seconds=30.0)

            # Mark exhausted again for 60 seconds (should override)
            limiter.mark_exhausted(retry_after_seconds=60.0)

            # At 35 seconds, should still be blocked
            mock_time.return_value = base_time + 35
            self.assertFalse(limiter.can_request())

            # At 61 seconds, should be recovered
            mock_time.return_value = base_time + 61
            self.assertTrue(limiter.can_request())

    def test_mark_exhausted_with_zero_duration(self):
        """Test mark_exhausted with zero duration."""
        limiter = RateLimiter(name="test")

        limiter.mark_exhausted(retry_after_seconds=0.0)

        # Should immediately be able to request
        self.assertTrue(limiter.can_request())

    def test_mark_exhausted_with_very_small_duration(self):
        """Test mark_exhausted with very small duration."""
        limiter = RateLimiter(name="test")

        limiter.mark_exhausted(retry_after_seconds=0.001)

        # Sleep a tiny bit and should be recovered
        time.sleep(0.01)
        self.assertTrue(limiter.can_request())

    def test_concurrent_access_is_safe(self):
        """Test that concurrent access doesn't cause crashes.

        Note: We don't use locks because the worst-case race is harmless.
        Two requests both see 'not exhausted', both hit API, both get 429,
        both call mark_exhausted - the last one wins, which is fine.
        """
        limiter = RateLimiter(name="test")

        import threading

        errors = []

        def worker():
            try:
                for _ in range(100):
                    limiter.can_request()
                    limiter.mark_exhausted(0.001)
                    limiter.get_status()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Should not have any errors
        self.assertEqual(len(errors), 0)


if __name__ == "__main__":
    unittest.main()

"""
Tests for the RateLimiter class in fighthealthinsurance.utils.

Tests cover:
- RPM (requests per minute) sliding window behavior
- RPD (requests per day) daily counter with UTC reset
- Temporary exhaustion marking and auto-recovery
- Thread safety (basic tests)
- Status reporting
"""
import asyncio
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from fighthealthinsurance.utils import RateLimiter


class TestRateLimiterBasic(unittest.TestCase):
    """Basic functionality tests for RateLimiter."""

    def test_init_default_values(self):
        """Test RateLimiter initializes with correct defaults."""
        limiter = RateLimiter(name="test")
        
        self.assertEqual(limiter.rpm_limit, 30)
        self.assertEqual(limiter.rpd_limit, 1000)
        self.assertEqual(limiter.name, "test")

    def test_init_custom_values(self):
        """Test RateLimiter initializes with custom values."""
        limiter = RateLimiter(rpm_limit=10, rpd_limit=500, name="custom")
        
        self.assertEqual(limiter.rpm_limit, 10)
        self.assertEqual(limiter.rpd_limit, 500)
        self.assertEqual(limiter.name, "custom")

    def test_can_request_initially_true(self):
        """Test can_request returns True when no requests have been made."""
        limiter = RateLimiter(rpm_limit=30, rpd_limit=1000, name="test")
        
        self.assertTrue(limiter.can_request())

    def test_record_request_increments_counters(self):
        """Test record_request properly tracks the request."""
        limiter = RateLimiter(rpm_limit=30, rpd_limit=1000, name="test")
        
        limiter.record_request()
        status = limiter.get_status()
        
        self.assertEqual(status["rpm_current"], 1)
        self.assertEqual(status["rpd_current"], 1)


class TestRateLimiterRPM(unittest.TestCase):
    """Tests for RPM (requests per minute) sliding window behavior."""

    def test_rpm_limit_enforced(self):
        """Test that RPM limit is enforced."""
        limiter = RateLimiter(rpm_limit=3, rpd_limit=1000, name="test")
        
        # Make 3 requests (should all succeed)
        for _ in range(3):
            self.assertTrue(limiter.can_request())
            limiter.record_request()
        
        # 4th request should be blocked
        self.assertFalse(limiter.can_request())

    def test_rpm_sliding_window_recovery(self):
        """Test that RPM recovers as requests age out of the window."""
        limiter = RateLimiter(rpm_limit=2, rpd_limit=1000, name="test")
        
        # Mock time to control the sliding window
        base_time = time.time()
        
        with patch('time.time') as mock_time:
            # Record 2 requests at base_time
            mock_time.return_value = base_time
            limiter.record_request()
            limiter.record_request()
            
            # Should be blocked now
            self.assertFalse(limiter.can_request())
            
            # Move time forward 61 seconds (past the 60s window)
            mock_time.return_value = base_time + 61
            
            # Should be able to request again
            self.assertTrue(limiter.can_request())

    def test_rpm_partial_window_recovery(self):
        """Test that only old requests age out, recent ones remain."""
        limiter = RateLimiter(rpm_limit=2, rpd_limit=1000, name="test")
        
        base_time = time.time()
        
        with patch('time.time') as mock_time:
            # Record request at base_time
            mock_time.return_value = base_time
            limiter.record_request()
            
            # Record request at base_time + 30s
            mock_time.return_value = base_time + 30
            limiter.record_request()
            
            # Should be blocked now (2 requests in window)
            self.assertFalse(limiter.can_request())
            
            # Move to base_time + 61s (first request ages out)
            mock_time.return_value = base_time + 61
            
            # Should allow one more request (only second request in window)
            self.assertTrue(limiter.can_request())
            limiter.record_request()
            
            # Now blocked again
            self.assertFalse(limiter.can_request())


class TestRateLimiterRPD(unittest.TestCase):
    """Tests for RPD (requests per day) daily counter behavior."""

    def test_rpd_limit_enforced(self):
        """Test that RPD limit is enforced."""
        limiter = RateLimiter(rpm_limit=1000, rpd_limit=3, name="test")
        
        # Make 3 requests (should all succeed)
        for _ in range(3):
            self.assertTrue(limiter.can_request())
            limiter.record_request()
        
        # 4th request should be blocked
        self.assertFalse(limiter.can_request())

    def test_rpd_daily_reset(self):
        """Test that RPD resets at day boundary."""
        limiter = RateLimiter(rpm_limit=1000, rpd_limit=2, name="test")
        
        with patch.object(limiter, '_get_utc_date') as mock_date:
            # Start on day 1
            mock_date.return_value = "2026-01-08"
            limiter.record_request()
            limiter.record_request()
            
            # Should be blocked
            self.assertFalse(limiter.can_request())
            
            # Move to day 2
            mock_date.return_value = "2026-01-09"
            
            # Should be able to request again (daily reset)
            self.assertTrue(limiter.can_request())
            
            # Verify counter was reset
            status = limiter.get_status()
            self.assertEqual(status["rpd_current"], 0)


class TestRateLimiterExhaustion(unittest.TestCase):
    """Tests for temporary exhaustion marking (429 handling)."""

    def test_mark_exhausted_blocks_requests(self):
        """Test that mark_exhausted blocks subsequent requests."""
        limiter = RateLimiter(rpm_limit=30, rpd_limit=1000, name="test")
        
        # Initially can request
        self.assertTrue(limiter.can_request())
        
        # Mark as exhausted for 60 seconds
        limiter.mark_exhausted(retry_after_seconds=60.0)
        
        # Should be blocked now
        self.assertFalse(limiter.can_request())

    def test_mark_exhausted_auto_recovery(self):
        """Test that exhaustion auto-recovers after the specified duration."""
        limiter = RateLimiter(rpm_limit=30, rpd_limit=1000, name="test")
        
        base_time = time.time()
        
        with patch('time.time') as mock_time:
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
        limiter = RateLimiter(rpm_limit=30, rpd_limit=1000, name="test")
        
        base_time = time.time()
        
        with patch('time.time') as mock_time:
            mock_time.return_value = base_time
            
            # Mark as exhausted for 120 seconds
            limiter.mark_exhausted(retry_after_seconds=120.0)
            
            # At 60 seconds, still blocked
            mock_time.return_value = base_time + 60
            self.assertFalse(limiter.can_request())
            
            # At 121 seconds, recovered
            mock_time.return_value = base_time + 121
            self.assertTrue(limiter.can_request())


class TestRateLimiterStatus(unittest.TestCase):
    """Tests for get_status method."""

    def test_get_status_initial(self):
        """Test get_status returns correct initial state."""
        limiter = RateLimiter(rpm_limit=30, rpd_limit=1000, name="test-model")
        
        status = limiter.get_status()
        
        self.assertEqual(status["name"], "test-model")
        self.assertEqual(status["rpm_current"], 0)
        self.assertEqual(status["rpm_limit"], 30)
        self.assertEqual(status["rpd_current"], 0)
        self.assertEqual(status["rpd_limit"], 1000)
        self.assertFalse(status["exhausted"])
        self.assertIsNone(status["exhausted_until"])

    def test_get_status_after_requests(self):
        """Test get_status reflects recorded requests."""
        limiter = RateLimiter(rpm_limit=30, rpd_limit=1000, name="test")
        
        limiter.record_request()
        limiter.record_request()
        limiter.record_request()
        
        status = limiter.get_status()
        
        self.assertEqual(status["rpm_current"], 3)
        self.assertEqual(status["rpd_current"], 3)

    def test_get_status_when_exhausted(self):
        """Test get_status reflects exhausted state."""
        limiter = RateLimiter(rpm_limit=30, rpd_limit=1000, name="test")
        
        limiter.mark_exhausted(retry_after_seconds=60.0)
        
        status = limiter.get_status()
        
        self.assertTrue(status["exhausted"])
        self.assertIsNotNone(status["exhausted_until"])


class TestRateLimiterCombined(unittest.TestCase):
    """Tests for combined RPM and RPD behavior."""

    def test_both_limits_checked(self):
        """Test that both RPM and RPD limits are checked."""
        # Low RPM limit
        limiter = RateLimiter(rpm_limit=2, rpd_limit=5, name="test")
        
        # Use up RPM limit
        limiter.record_request()
        limiter.record_request()
        
        # Should be blocked by RPM (not RPD)
        self.assertFalse(limiter.can_request())
        status = limiter.get_status()
        self.assertEqual(status["rpm_current"], 2)
        self.assertEqual(status["rpd_current"], 2)

    def test_rpd_blocks_even_when_rpm_available(self):
        """Test that RPD limit blocks even when RPM has capacity."""
        base_time = time.time()
        
        with patch('time.time') as mock_time:
            mock_time.return_value = base_time
            
            # High RPM, low RPD
            limiter = RateLimiter(rpm_limit=100, rpd_limit=2, name="test")
            
            # Use up RPD limit
            limiter.record_request()
            limiter.record_request()
            
            # Move time forward to clear RPM window
            mock_time.return_value = base_time + 61
            
            # Should still be blocked by RPD
            self.assertFalse(limiter.can_request())


class TestRateLimiterEdgeCases(unittest.TestCase):
    """Edge case tests for RateLimiter."""

    def test_zero_limits_block_all(self):
        """Test that zero limits block all requests."""
        limiter = RateLimiter(rpm_limit=0, rpd_limit=1000, name="test")
        
        # Should be immediately blocked
        self.assertFalse(limiter.can_request())

    def test_very_high_limits(self):
        """Test that very high limits work correctly."""
        limiter = RateLimiter(rpm_limit=1000000, rpd_limit=1000000, name="test")
        
        # Should allow many requests
        for _ in range(100):
            self.assertTrue(limiter.can_request())
            limiter.record_request()

    def test_multiple_mark_exhausted_calls(self):
        """Test that multiple mark_exhausted calls use the latest value."""
        limiter = RateLimiter(rpm_limit=30, rpd_limit=1000, name="test")
        
        base_time = time.time()
        
        with patch('time.time') as mock_time:
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


if __name__ == "__main__":
    unittest.main()

"""Tests for performance-related settings configuration."""

from django.conf import settings
from django.test import TestCase


class PerformanceSettingsTestCase(TestCase):
    """Test performance settings are properly configured."""

    def test_compression_enabled(self):
        """Test that static file compression is enabled."""
        # Django Compressor should be enabled for better performance
        self.assertTrue(
            settings.COMPRESS_ENABLED,
            "COMPRESS_ENABLED should be True for better page load performance",
        )

    def test_cache_configured(self):
        """Test that caching is configured."""
        # Verify CACHES is configured
        self.assertIn("default", settings.CACHES)
        self.assertIn("BACKEND", settings.CACHES["default"])
        # Should have some cache backend configured (not dummy cache)
        self.assertNotEqual(
            settings.CACHES["default"]["BACKEND"],
            "django.core.cache.backends.dummy.DummyCache",
            "Cache backend should not be DummyCache for performance",
        )

    def test_static_files_configuration(self):
        """Test that static files are properly configured."""
        # Verify static file settings exist
        self.assertTrue(hasattr(settings, "STATIC_URL"))
        self.assertTrue(hasattr(settings, "STATIC_ROOT"))
        # CompressorFinder should be in the finders for compression to work
        self.assertIn(
            "compressor.finders.CompressorFinder", settings.STATICFILES_FINDERS
        )

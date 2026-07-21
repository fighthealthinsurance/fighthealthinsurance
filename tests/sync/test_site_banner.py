"""Tests for the admin-controlled site header banner (SiteBanner)."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from fighthealthinsurance.constants import SITE_BANNER_CANNED_MESSAGES
from fighthealthinsurance.context_processors import site_banner_context
from fighthealthinsurance.models import SiteBanner

User = get_user_model()


class TestSiteBannerModel(TestCase):
    """Visibility and serialization logic on the SiteBanner model."""

    def test_active_banner_is_visible(self):
        banner = SiteBanner(message="Heads up", active=True)
        self.assertTrue(banner.is_currently_visible())

    def test_inactive_banner_is_not_visible(self):
        banner = SiteBanner(message="Heads up", active=False)
        self.assertFalse(banner.is_currently_visible())

    def test_expired_banner_is_not_visible(self):
        banner = SiteBanner(
            message="Heads up",
            active=True,
            expires_at=timezone.now() - timedelta(hours=1),
        )
        self.assertFalse(banner.is_currently_visible())

    def test_future_expiry_banner_is_visible(self):
        banner = SiteBanner(
            message="Heads up",
            active=True,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.assertTrue(banner.is_currently_visible())

    def test_get_active_banners_excludes_inactive_and_expired(self):
        active = SiteBanner.objects.create(message="Show me", active=True)
        SiteBanner.objects.create(message="Hidden", active=False)
        SiteBanner.objects.create(
            message="Stale",
            active=True,
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        banners = SiteBanner.get_active_banners()
        ids = [b["id"] for b in banners]
        self.assertEqual(ids, [active.id])

    def test_get_active_banners_returns_expected_shape(self):
        banner = SiteBanner.objects.create(
            message="Hi", level=SiteBanner.LEVEL_DANGER, dismissible=False
        )
        [payload] = SiteBanner.get_active_banners()
        self.assertEqual(payload["id"], banner.id)
        self.assertEqual(payload["message"], "Hi")
        self.assertEqual(payload["level"], SiteBanner.LEVEL_DANGER)
        self.assertFalse(payload["dismissible"])
        self.assertIsInstance(payload["version"], int)

    def test_get_active_banners_orders_newest_first(self):
        first = SiteBanner.objects.create(message="first")
        SiteBanner.objects.create(message="second")
        # Re-saving bumps updated_at, so `first` should now sort to the top.
        first.message = "first edited"
        first.save()

        banners = SiteBanner.get_active_banners()
        self.assertEqual(banners[0]["id"], first.id)

    def test_str_includes_level_status_and_preview(self):
        banner = SiteBanner(message="Models are down", active=True)
        text = str(banner)
        self.assertIn("warning", text)
        self.assertIn("active", text)
        self.assertIn("Models are down", text)


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "test-site-banner-cache",
        }
    },
    # Exercise the cache directly; keep the background refresh thread out of it.
    SITE_BANNER_BACKGROUND_REFRESH=False,
)
class TestSiteBannerCache(TestCase):
    """The active-banner list is cached and refreshed off the request path."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def test_refresh_cache_populates_and_returns_active_banners(self):
        from django.core.cache import cache

        banner = SiteBanner.objects.create(message="Warm me")
        result = SiteBanner.refresh_cache()

        self.assertEqual([b["id"] for b in result], [banner.id])
        self.assertEqual(cache.get(SiteBanner.CACHE_KEY), result)

    def test_get_active_banners_serves_warm_cache_without_query(self):
        SiteBanner.objects.create(message="Cached")
        # Warm the cache, then a subsequent read must not touch the DB.
        SiteBanner.refresh_cache()

        with self.assertNumQueries(0):
            banners = SiteBanner.get_active_banners()

        self.assertEqual([b["message"] for b in banners], ["Cached"])

    def test_get_active_banners_repopulates_on_cold_miss(self):
        from django.core.cache import cache

        banner = SiteBanner.objects.create(message="Cold")
        cache.delete(SiteBanner.CACHE_KEY)

        banners = SiteBanner.get_active_banners()

        self.assertEqual([b["id"] for b in banners], [banner.id])
        # The miss also warmed the cache for the next reader.
        self.assertIsNotNone(cache.get(SiteBanner.CACHE_KEY))

    def test_saving_a_banner_invalidates_the_cache(self):
        from django.core.cache import cache

        SiteBanner.refresh_cache()
        self.assertIsNotNone(cache.get(SiteBanner.CACHE_KEY))

        # The post_save signal should drop the cached list.
        SiteBanner.objects.create(message="New banner")
        self.assertIsNone(cache.get(SiteBanner.CACHE_KEY))


class TestSiteBannerRefresher(TestCase):
    """The per-worker background refresher (site_banner_refresh)."""

    def _make_refresher(self):
        from fighthealthinsurance.site_banner_refresh import _SiteBannerRefresher

        return _SiteBannerRefresher()

    @override_settings(SITE_BANNER_BACKGROUND_REFRESH=False)
    def test_ensure_started_is_noop_when_disabled(self):
        refresher = self._make_refresher()
        with patch.object(refresher, "_start_thread") as start:
            refresher.ensure_started()
        start.assert_not_called()

    @override_settings(SITE_BANNER_BACKGROUND_REFRESH=True)
    def test_ensure_started_starts_the_thread_exactly_once(self):
        refresher = self._make_refresher()
        with patch.object(refresher, "_start_thread") as start:
            refresher.ensure_started()
            refresher.ensure_started()  # idempotent
        start.assert_called_once()

    def test_refresh_once_refreshes_cache_and_returns_connections(self):
        refresher = self._make_refresher()
        with patch.object(SiteBanner, "refresh_cache") as refresh, patch(
            "fighthealthinsurance.site_banner_refresh.connections"
        ) as conns:
            refresher._refresh_once()
        refresh.assert_called_once()
        # The refresh thread never gets per-request connection cleanup, so a
        # cycle must always hand its connection back itself (leak prevention).
        conns.close_all.assert_called_once()

    def test_refresh_once_returns_connections_even_when_refresh_fails(self):
        refresher = self._make_refresher()
        with patch.object(
            SiteBanner, "refresh_cache", side_effect=RuntimeError("boom")
        ), patch("fighthealthinsurance.site_banner_refresh.connections") as conns:
            # A failed refresh must not propagate (it would kill the loop)...
            refresher._refresh_once()
        # ...and must still return the connection so failures don't leak.
        conns.close_all.assert_called_once()


class TestSiteBannerContextProcessor(TestCase):
    """The context processor exposes active banners to every template."""

    def setUp(self):
        self.factory = RequestFactory()

    def test_returns_active_banner(self):
        banner = SiteBanner.objects.create(message="Context banner")
        context = site_banner_context(self.factory.get("/"))
        self.assertEqual([b["id"] for b in context["site_banners"]], [banner.id])

    def test_returns_empty_when_no_banners(self):
        context = site_banner_context(self.factory.get("/"))
        self.assertEqual(context["site_banners"], [])

    def test_lookup_failure_degrades_gracefully(self):
        with patch.object(
            SiteBanner, "get_active_banners", side_effect=RuntimeError("boom")
        ):
            context = site_banner_context(self.factory.get("/"))
        self.assertEqual(context["site_banners"], [])


class TestSiteBannerRendering(TestCase):
    """The banner renders in the site header on pages that extend base.html."""

    def setUp(self):
        self.client = Client()
        self.url = reverse("about")

    def test_active_banner_message_renders(self):
        SiteBanner.objects.create(
            message="Our AI models are having difficulty.",
            level=SiteBanner.LEVEL_DANGER,
        )
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Our AI models are having difficulty.")
        self.assertContains(response, "alert-danger")

    def test_inactive_banner_not_rendered(self):
        SiteBanner.objects.create(message="Do not show this", active=False)
        response = self.client.get(self.url)
        self.assertNotContains(response, "Do not show this")

    def test_dismissible_banner_has_close_button(self):
        SiteBanner.objects.create(message="Closeable", dismissible=True)
        response = self.client.get(self.url)
        # The aria-label is unique to the dismiss button (the class name also
        # appears in the always-emitted CSS/JS, so it can't distinguish here).
        self.assertContains(response, 'aria-label="Dismiss message"')

    def test_non_dismissible_banner_has_no_close_button(self):
        SiteBanner.objects.create(message="Sticky", dismissible=False)
        response = self.client.get(self.url)
        self.assertContains(response, "Sticky")
        self.assertNotContains(response, 'aria-label="Dismiss message"')


class TestSiteBannerAdmin(TestCase):
    """Staff manage banners through the Django admin."""

    ADMIN_URL = "/timbit/admin/fighthealthinsurance/sitebanner/"

    def setUp(self):
        self.client = Client()
        self.admin_user = User.objects.create_superuser(
            username="admin", email="admin@test.com", password="adminpass123"
        )
        self.client.login(username="admin", password="adminpass123")

    def test_changelist_loads(self):
        SiteBanner.objects.create(message="Existing banner")
        response = self.client.get(self.ADMIN_URL)
        self.assertEqual(response.status_code, 200)

    def test_add_form_has_canned_message_picker(self):
        response = self.client.get(f"{self.ADMIN_URL}add/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "id_canned_message")
        # The first pre-canned label should be offered as an option.
        self.assertContains(response, SITE_BANNER_CANNED_MESSAGES[0][0])

    def test_add_form_loads_admin_helper_js(self):
        response = self.client.get(f"{self.ADMIN_URL}add/")
        self.assertContains(response, "admin_site_banner.js")

    def test_create_banner_via_admin(self):
        response = self.client.post(
            f"{self.ADMIN_URL}add/",
            {
                "message": "Created via admin",
                "level": SiteBanner.LEVEL_WARNING,
                "active": "on",
                "dismissible": "on",
                "canned_message": "",
                "expires_at_0": "",
                "expires_at_1": "",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302)
        banner = SiteBanner.objects.get(message="Created via admin")
        self.assertTrue(banner.active)
        self.assertTrue(banner.is_currently_visible())

    def test_short_message_truncates_long_text(self):
        from fighthealthinsurance.admin import SiteBannerAdmin

        admin_instance = SiteBannerAdmin(SiteBanner, None)
        banner = SiteBanner(message="x" * 200)
        result = admin_instance.short_message(banner)
        self.assertLessEqual(len(result), 81)
        self.assertTrue(result.endswith("…"))

    def test_canned_choices_come_from_constants(self):
        from fighthealthinsurance.admin import SiteBannerAdminForm

        form = SiteBannerAdminForm()
        values = [value for value, _ in form.fields["canned_message"].choices]
        for _, text in SITE_BANNER_CANNED_MESSAGES:
            self.assertIn(text, values)

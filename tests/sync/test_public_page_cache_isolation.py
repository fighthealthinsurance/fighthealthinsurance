"""The cached public content pages must not leak one visitor's session data.

The public educational pages (glossary, denial-reason decoder, insurer guides,
appeal-deadline calculator, start-appeal) are wrapped in
``cache_control(public=True)`` + ``cache_page(...)``. They all extend
``base.html``, which renders the visitor's session-scoped denial UUID:

    {% if fhi_session_key %}<meta name="fhi-session-key" content="..."></meta>{% endif %}

``cache_page`` is a *view-level* decorator, so its ``process_response`` runs
before ``SessionMiddleware.process_response`` gets a chance to add
``Vary: Cookie``. Without an explicit ``vary_on_cookie``, Django therefore
learns a cache key derived from the URL alone, and the first visitor's rendered
HTML — including their denial UUID — is replayed to every subsequent visitor
for the life of the entry (and by any shared/CDN cache, since it is marked
``public``).

These tests run against a real LocMemCache because the test configurations use
DummyCache, which silently makes any cache-correctness bug invisible.
"""

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

# The cached public routes that render base.html.
CACHED_PUBLIC_ROUTES = [
    ("glossary_index", {}),
    ("glossary_term", {"slug": "external-review"}),
    ("denial_reason_decoder_index", {}),
    ("denial_reason_decoder_detail", {"slug": "out-of-network"}),
    ("insurer_appeal_guide_index", {}),
    ("insurer_appeal_guide", {"slug": "aetna"}),
    ("appeal_deadline_calculator", {}),
    ("start_appeal", {}),
]

LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-public-page-cache-isolation",
    }
}

SECRET_UUID = "11111111-2222-3333-4444-555555555555"


@override_settings(CACHES=LOCMEM_CACHE)
class PublicPageCacheIsolationTest(TestCase):
    """A session-bearing visitor must never poison the shared page cache."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _client_with_denial_session(self) -> Client:
        client = Client()
        session = client.session
        session["denial_uuid"] = SECRET_UUID
        session.save()
        return client

    def test_session_uuid_never_leaks_to_a_later_visitor(self):
        """Visitor A's denial UUID must not appear in visitor B's page."""
        for route_name, kwargs in CACHED_PUBLIC_ROUTES:
            with self.subTest(route=route_name):
                cache.clear()
                url = reverse(route_name, kwargs=kwargs)

                # Visitor A is mid-appeal: their session carries a denial UUID.
                primed = self._client_with_denial_session().get(url)
                self.assertEqual(primed.status_code, 200)

                # Visitor B is an anonymous first-time reader (the SEO
                # audience these pages exist for).
                anonymous = Client().get(url)
                self.assertEqual(anonymous.status_code, 200)
                self.assertNotIn(
                    SECRET_UUID,
                    anonymous.content.decode("utf-8"),
                    msg=(
                        f"{route_name} served visitor A's denial UUID to an "
                        "anonymous visitor — the page cache is not keyed on "
                        "the session cookie"
                    ),
                )

    def test_flash_message_never_leaks_to_a_later_visitor(self):
        """Visitor A's flash message must not be baked into the shared cache.

        base.html renders `{% if messages %}`; without suppression on cached
        pages, A's error banner would be replayed to every later visitor.
        """
        from django.contrib.messages import get_messages

        url = reverse("glossary_index")
        secret_message = "We couldn't find that denial. Please start again."

        primed = Client()
        # Drive a real view that sets a message, then land on the cached page.
        session = primed.session
        session["denial_uuid"] = SECRET_UUID
        session.save()
        primed_response = primed.get(url)
        self.assertEqual(primed_response.status_code, 200)

        anonymous = Client().get(url)
        self.assertEqual(anonymous.status_code, 200)
        self.assertNotIn(secret_message, anonymous.content.decode("utf-8"))
        # And the cached page carries no message markup at all.
        self.assertNotIn('role="alert"', anonymous.content.decode("utf-8"))

    def test_anonymous_page_is_still_cached(self):
        """The caching still works for the anonymous audience it targets.

        vary_on_cookie must not accidentally disable caching outright: two
        cookie-less visitors should share one cache entry.
        """
        url = reverse("glossary_index")
        first = Client().get(url)
        self.assertEqual(first.status_code, 200)
        second = Client().get(url)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.content, second.content)

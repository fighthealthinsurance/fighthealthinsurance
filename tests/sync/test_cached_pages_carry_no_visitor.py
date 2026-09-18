"""A page cached as a whole response must not carry the visitor who warmed it.

``form_persistence_context`` puts the session's denial UUID into every
template and ``base.html`` emits it as a meta tag. ``StaticIshView`` wraps its
pages in ``cache_page``, so from the day that caching landed the first visitor
to warm an entry wrote their own case id into HTML every later visitor was
served, for the life of the entry and again in each browser that held it under
``Cache-Control: public``.

The probe that matters is the last test here: it walks every page the site
marks publicly cacheable and asserts none of them can carry a case id. It does
not enumerate the pages, so a cached route added later is covered without
anyone remembering to add it.
"""

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import get_resolver, reverse

from fighthealthinsurance.views import (
    PublicCachedPageMixin,
    StaticIshView,
    VISITOR_CONTEXT_KEYS,
)

# The test configuration uses DummyCache, which would make every assertion
# here pass by doing nothing at all.
LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "cached-pages-carry-no-visitor",
    }
}

CASE_UUID = "11111111-2222-3333-4444-555555555555"

# Enough of the cached set to fail loudly on the common pages, with the sweep
# below covering the rest.
NAMED_CACHED_ROUTES = ("root", "faq", "privacy_policy", "tos", "about", "about-ai")


def mid_appeal_client() -> Client:
    """Someone who has started an appeal, so their session names a case."""
    client = Client()
    session = client.session
    session["denial_uuid"] = CASE_UUID
    session.save()
    return client


@override_settings(CACHES=LOCMEM_CACHE)
class ACachedPageCarriesNoVisitorTest(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_case_id_does_not_reach_the_next_visitor(self):
        """The defect itself, through a real cache."""
        for name in NAMED_CACHED_ROUTES:
            with self.subTest(route=name):
                cache.clear()
                url = reverse(name)

                warmed = mid_appeal_client().get(url)
                self.assertEqual(warmed.status_code, 200)

                later = Client().get(url)
                self.assertEqual(later.status_code, 200)
                self.assertNotIn(
                    CASE_UUID,
                    later.content.decode(),
                    msg=f"{name} served one visitor's case id to the next",
                )

    def test_the_page_is_still_cached(self):
        """Emptying the values must not be done by turning the cache off."""
        url = reverse("faq")
        cache.clear()

        first = Client().get(url)
        self.assertEqual(first.status_code, 200)
        with self.assertNumQueries(0):
            second = Client().get(url)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.content, second.content)

    def test_two_visitors_are_served_the_same_bytes(self):
        """Rendered, not cached: catches anything visitor-shaped added later.

        A csrf token, a signed-in name or a flash message would all fail here
        before they could ever reach the cache.
        """
        for name in NAMED_CACHED_ROUTES:
            with self.subTest(route=name):
                cache.clear()
                theirs = mid_appeal_client().get(reverse(name))
                cache.clear()
                anybodys = Client().get(reverse(name))

                self.assertEqual(theirs.status_code, 200)
                self.assertEqual(
                    theirs.content,
                    anybodys.content,
                    msg=f"{name} renders differently for the visitor who asks",
                )


class EveryPubliclyCacheablePageTest(TestCase):
    """The sweep: no enumeration, so a new cached route cannot slip past.

    Runs without the cache override on purpose. It asks each page what it
    claims about itself, and a page that says ``Cache-Control: public`` is
    making a promise about who may be served it.
    """

    def _no_argument_routes(self):
        seen = set()
        for pattern in get_resolver().url_patterns:
            for entry in getattr(pattern, "url_patterns", [pattern]):
                name = getattr(entry, "name", None)
                if not name or name in seen:
                    continue
                seen.add(name)
                try:
                    yield name, reverse(name)
                except Exception:
                    continue

    def test_nothing_publicly_cacheable_can_carry_a_case_id(self):
        checked, cacheable = [], []
        for name, url in self._no_argument_routes():
            # A fresh visitor per page. Reusing one client silently defeats
            # this test: some routes clear the case out of the session, and
            # every page after that would be checked with nothing to find.
            try:
                response = mid_appeal_client().get(url)
            except Exception:
                continue
            if response.status_code != 200:
                continue
            checked.append(name)
            if "public" not in response.headers.get("Cache-Control", ""):
                continue
            cacheable.append(name)
            body = response.content.decode(errors="replace")
            self.assertNotIn(
                CASE_UUID,
                body,
                msg=(
                    f"{name} is served with Cache-Control: public and carries "
                    "the case id of the visitor who asked for it"
                ),
            )

        # Floors, so a sweep that silently stops finding pages fails instead
        # of passing empty.
        self.assertGreater(len(checked), 20, f"only reached {checked}")
        self.assertGreater(len(cacheable), 10, f"only found {cacheable} cacheable")


class TheMixinIsWiredTest(TestCase):
    def test_static_ish_pages_use_it(self):
        self.assertTrue(issubclass(StaticIshView, PublicCachedPageMixin))

    def test_it_empties_every_visitor_key(self):
        self.assertEqual(
            VISITOR_CONTEXT_KEYS, ("fhi_session_key", "fhi_request_method")
        )
        response = mid_appeal_client().get(reverse("faq"))
        for key in VISITOR_CONTEXT_KEYS:
            self.assertNotIn(
                f'name="fhi-{key.replace("fhi_", "").replace("_", "-")}"',
                response.content.decode(),
            )

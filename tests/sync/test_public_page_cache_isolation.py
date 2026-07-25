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

from unittest import mock

from django.contrib.messages import get_messages
from django.core.cache import cache
from django.middleware.cache import CacheMiddleware
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import get_resolver, resolve, reverse
from django.utils.decorators import method_decorator
from django.utils.html import escape
from django.views import generic
from django.views.decorators.cache import cache_page

from fighthealthinsurance.views import GlossaryIndexView, PublicCachedPageMixin

# Every cached route that renders base.html. This must include StaticIshView
# pages, not just the new content routes: several StaticIshView subclasses
# build context without chaining super(), which is exactly how five of them
# (faq/tos/privacy_policy/mhmda/contact) kept leaking after the first fix
# attempt while a new-routes-only list stayed green.
CACHED_PUBLIC_ROUTES = [
    ("glossary_index", {}),
    ("glossary_term", {"slug": "external-review"}),
    ("denial_reason_decoder_index", {}),
    ("denial_reason_decoder_detail", {"slug": "out-of-network"}),
    ("insurer_appeal_guide_index", {}),
    ("insurer_appeal_guide", {"slug": "aetna"}),
    ("appeal_deadline_calculator", {}),
    ("start_appeal", {}),
    # StaticIshView pages — the ones that override get_context_data without
    # super() are the highest-risk, and all are linked from the site footer.
    ("faq", {}),
    ("tos", {}),
    ("privacy_policy", {}),
    ("mhmda", {}),
    ("contact", {}),
    ("about", {}),
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
        url = reverse("glossary_index")

        primed = Client()
        # Drive a real view that actually stores a message. ChooseAppeal posts
        # an invalid form, which calls messages.error() and redirects — so the
        # message is genuinely in storage and unread when the next request
        # renders. (Asserting on a page GET alone would be vacuous: no message
        # would ever exist.)
        post = primed.post(reverse("choose_appeal"), {})
        self.assertEqual(post.status_code, 302)
        stored = [str(m) for m in get_messages(post.wsgi_request)]
        self.assertTrue(
            stored, "precondition failed: no flash message was actually stored"
        )

        primed_response = primed.get(url)
        self.assertEqual(primed_response.status_code, 200)

        anonymous = Client().get(url)
        self.assertEqual(anonymous.status_code, 200)
        body = anonymous.content.decode("utf-8")
        for message_text in stored:
            # Compare against the escaped form: the template autoescapes, so a
            # message containing an apostrophe ("We couldn't ...") is rendered
            # as "We couldn&#x27;t ..." and a raw-string assertion could never
            # fail no matter how badly the cache leaked.
            self.assertNotIn(escape(message_text), body)
        # The messages block's distinctive class must be absent. (Asserting on
        # role="alert" would pass coincidentally — the site banner partial and
        # the decoder's deadline callout both use it.)
        self.assertNotIn("alert-dismissible", body)

    def test_anonymous_page_is_still_cached(self):
        """Two cookie-less visitors share one cache entry (no fragmentation).

        Proven by showing the *view* runs only once across two requests —
        comparing response bodies would pass even with caching disabled, since
        the page is deterministic.
        """
        url = reverse("glossary_index")
        with mock.patch.object(
            GlossaryIndexView,
            "get_context_data",
            side_effect=GlossaryIndexView.get_context_data,
            autospec=True,
        ) as spy:
            first = Client().get(url)
            self.assertEqual(first.status_code, 200)
            second = Client().get(url)
            self.assertEqual(second.status_code, 200)

        self.assertEqual(first.content, second.content)
        self.assertEqual(
            spy.call_count,
            1,
            "second anonymous request should have been served from the cache",
        )


# Cached routes whose callback is not a class-based view. These render no
# template and so cannot carry session context; each needs a reason to be here.
NON_TEMPLATE_CACHED_CALLBACKS = {
    # Renders Django's built-in sitemap.xml (the project ships no override), a
    # pure XML listing built from the URLconf. It reads only request.scheme,
    # the Host header and ?p= — never the session — and does not extend
    # base.html, so there is no per-visitor context to strip.
    "fighthealthinsurance.sitemap.sitemap_view",
}


def _iter_url_patterns(resolver):
    """Yield every URLPattern reachable from ``resolver``, including includes."""
    for entry in resolver.url_patterns:
        if hasattr(entry, "url_patterns"):
            yield from _iter_url_patterns(entry)
        else:
            yield entry


def _wraps_cache_page(callback) -> bool:
    """True if ``cache_page`` appears anywhere in ``callback``'s decorator stack.

    ``cache_page`` is built with ``decorator_from_middleware_with_args``. In
    Django 5.2 the middleware is instantiated once per decorated view and
    captured by three nested helpers (``_pre_process_request`` and friends),
    which the returned ``_view_wrapper`` closes over — so the instance is two
    closure levels deep, not one. Searching the whole callable graph reachable
    through ``__wrapped__`` and closure cells finds it wherever it sits, and
    accepts the class itself for older Django, regardless of how many other
    decorators (``cache_control``, ``csrf_exempt``, …) are layered on top.

    ``method_decorator(cache_page(...), name="dispatch")`` — the form the Django
    docs recommend for CBVs — needs the sequence expansion below: it applies its
    decorators per *call*, so no middleware instance exists at import time and
    the only static reference is the decorator *list* held in a closure cell.
    ``test_detector_sees_cache_page_behind_method_decorator`` enforces this.
    """
    seen: set[int] = set()
    retained: list[object] = []  # keep ids unique: a freed object's id can recur
    stack: list[object] = [callback]
    while stack:
        obj = stack.pop()
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        retained.append(obj)
        if obj is CacheMiddleware or isinstance(obj, CacheMiddleware):
            return True
        if isinstance(obj, (list, tuple, set, frozenset)):
            stack.extend(obj)
            continue
        if not callable(obj):
            continue
        stack.append(getattr(obj, "__wrapped__", None))
        # Catches the class-body form `dispatch = cache_page(60)(fn)`. The
        # method_decorator form is reached via the sequence expansion above.
        stack.append(getattr(obj, "dispatch", None))
        for cell in getattr(obj, "__closure__", None) or ():
            try:
                stack.append(cell.cell_contents)
            except ValueError:  # empty cell (recursive closure not yet bound)
                continue
    return False


class CachedRouteStructureTest(SimpleTestCase):
    """Every ``cache_page``-wrapped view must sanitize per-visitor context.

    ``PublicPageCacheIsolationTest`` enumerates routes by hand, and that list
    has silently missed a leak twice: once when the fix only covered the new
    content routes, and again when five StaticIshView subclasses built their
    context without chaining ``super()``. This test needs no list — it walks the
    real URLconf, so a cached route added tomorrow is covered the day it lands.
    """

    def test_detector_sees_cache_page_behind_method_decorator(self):
        """The detector must catch the CBV form the Django docs recommend.

        Without this, a page cached via ``method_decorator`` would be reported
        as uncached and skipped entirely — the test would pass while the leak
        shipped.
        """

        @method_decorator(cache_page(60), name="dispatch")
        class MethodDecoratedView(generic.TemplateView):
            template_name = "about_us.html"

        self.assertTrue(_wraps_cache_page(MethodDecoratedView.as_view()))

    def test_detector_does_not_flag_an_uncached_view(self):
        """A plain view must not be flagged, or the mixin check means nothing."""

        class PlainView(generic.TemplateView):
            template_name = "about_us.html"

        self.assertFalse(_wraps_cache_page(PlainView.as_view()))

    def test_detector_finds_every_hand_listed_cached_route(self):
        """Detection must cover the known routes by identity, not just by count.

        The count floor alone is satisfiable by the StaticIshView routes, so it
        would stay green even if detection broke for every route decorated in
        urls.py.
        """
        for route_name, kwargs in CACHED_PUBLIC_ROUTES:
            with self.subTest(route=route_name):
                view = resolve(reverse(route_name, kwargs=kwargs)).func
                self.assertTrue(_wraps_cache_page(view))

    def test_every_cached_view_extends_public_cached_page_mixin(self):
        cached_routes = []
        offenders = []

        for pattern in _iter_url_patterns(get_resolver()):
            callback = pattern.callback
            if not _wraps_cache_page(callback):
                continue
            cached_routes.append(pattern)

            # as_view() stores the class on the returned function, and
            # functools.wraps copies __dict__ up through each decorator layer,
            # so view_class survives the cache_page/cache_control wrapping.
            view_class = getattr(callback, "view_class", None)
            if view_class is None:
                dotted = f"{callback.__module__}.{callback.__qualname__}"
                if dotted not in NON_TEMPLATE_CACHED_CALLBACKS:
                    offenders.append(f"{pattern.pattern} -> {dotted} (not a CBV)")
            elif not issubclass(view_class, PublicCachedPageMixin):
                offenders.append(f"{pattern.pattern} -> {view_class.__name__}")

        # Guard against the detector silently matching nothing (e.g. if Django
        # changes how cache_page is built), which would make this test vacuous.
        self.assertGreaterEqual(
            len(cached_routes),
            len(CACHED_PUBLIC_ROUTES),
            "cache_page detection found fewer routes than the hand-written "
            "list — _wraps_cache_page is no longer recognizing cached views",
        )
        self.assertEqual(
            offenders,
            [],
            msg=(
                "these cached routes are served from a shared, Cache-Control: "
                "public entry but do not strip per-visitor session context; "
                "they must inherit PublicCachedPageMixin: " + ", ".join(offenders)
            ),
        )

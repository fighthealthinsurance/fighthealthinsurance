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

from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
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
VISITOR_EMAIL = "visitor@example.com"
FLASH_TEXT = f"Your appeal draft is ready, {VISITOR_EMAIL}"

# Enough of the cached set to fail loudly on the common pages, with the sweep
# below covering the rest.
NAMED_CACHED_ROUTES = ("root", "faq", "privacy_policy", "tos", "about", "about-ai")


def _pages_that_take_a_slug():
    """The cached routes ``reverse`` cannot build without an argument.

    Three of the site's cached pages are per-slug: a blog post, a state help
    page, a microsite. The sweep below walks named routes, so without this
    they were the pages it silently skipped, and a leak on one of them would
    have looked like a clean run. The slugs come from the site's own
    registries rather than being typed here, so a rename cannot quietly
    empty this list.
    """
    from fighthealthinsurance.agent_docs import _blog_posts
    from fighthealthinsurance.microsites import get_microsite_slugs
    from fighthealthinsurance.state_help import get_state_help_slugs

    blog_slugs = [
        post.get("slug") for post in _blog_posts() if isinstance(post, dict)
    ]
    for route, slugs in (
        ("blog-post", blog_slugs),
        ("state_help", get_state_help_slugs()),
        ("microsite", get_microsite_slugs()),
    ):
        for slug in slugs:
            if not slug:
                continue
            try:
                yield route, reverse(route, kwargs={"slug": slug})
            except Exception:
                continue
            # One of each is the point: this is a privacy guard on the
            # template, not a content sweep.
            break


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
        """Emptying the values must not be done by turning the cache off.

        Counting queries proves nothing here: once the banner cache is warm
        an uncached render also does none. What proves a cache hit is the
        view never being asked to render the second time.
        """
        url = reverse("faq")
        cache.clear()

        with patch.object(
            StaticIshView,
            "render_to_response",
            autospec=True,
            side_effect=StaticIshView.render_to_response,
        ) as rendered:
            first = Client().get(url)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(rendered.call_count, 1, "the first request must render")

            second = Client().get(url)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(
                rendered.call_count,
                1,
                "the second request rendered again, so it was not served "
                "from the cache",
            )
        self.assertEqual(first.content, second.content)

    def test_two_visitors_are_served_the_same_bytes(self):
        """Rendered, not cached: catches anything visitor-shaped added later.

        A csrf token, a signed-in name or a flash message would all fail here
        before they could ever reach the cache. The visitor is carrying all
        three, because two anonymous clients with nothing to show would
        compare equal however much the template leaked.
        """
        for name in NAMED_CACHED_ROUTES:
            with self.subTest(route=name):
                cache.clear()
                theirs = self._a_visitor_with_something_to_lose().get(reverse(name))
                cache.clear()
                anybodys = Client().get(reverse(name))

                self.assertEqual(theirs.status_code, 200)
                self.assertEqual(
                    theirs.content,
                    anybodys.content,
                    msg=f"{name} renders differently for the visitor who asks",
                )

    def _a_visitor_with_something_to_lose(self):
        """Signed in, mid appeal, and carrying a flash message."""
        from django.contrib.auth import get_user_model
        from django.contrib.messages.storage.session import SessionStorage

        # One per call: this runs once per route in a subTest loop.
        users = get_user_model().objects
        user = users.create_user(
            username=f"cached-page-visitor-{users.count()}",
            email=VISITOR_EMAIL,
            password="not-a-real-password",
        )
        client = Client()
        client.force_login(user)
        session = client.session
        session["denial_uuid"] = CASE_UUID
        session.save()

        request = RequestFactory().get("/")
        request.session = client.session
        storage = SessionStorage(request)
        storage.add(40, FLASH_TEXT)
        storage.update(None)
        request.session.save()
        client.cookies[settings.SESSION_COOKIE_NAME] = request.session.session_key
        return client


class EveryPubliclyCacheablePageTest(TestCase):
    """The sweep, over the pages the site actually caches.

    It asks each page what it claims about itself, and a page that says
    ``Cache-Control: public`` is making a promise about who may be served it.

    It reaches those pages through ``StaticIshView``, which is where the
    site's whole-response caching lives, rather than by requesting every
    route it can reverse. The wider version was not hermetic: among the
    routes it walked was the resources page, which fetches news feeds over
    the network, so a cache privacy test made live outbound calls. The
    source guard below is what keeps the narrower sweep honest, by refusing
    a cached page that does not come through that base class.
    """

    def _cached_pages(self):
        """Every no-argument route served by a StaticIshView subclass."""
        seen = set()
        for pattern in get_resolver().url_patterns:
            for entry in getattr(pattern, "url_patterns", [pattern]):
                name = getattr(entry, "name", None)
                if not name or name in seen:
                    continue
                view_class = getattr(
                    getattr(entry, "callback", None), "view_class", None
                )
                if view_class is None or not issubclass(view_class, StaticIshView):
                    continue
                seen.add(name)
                try:
                    yield name, reverse(name)
                except Exception:
                    # Takes an argument. Those are picked up below, with a
                    # real slug, rather than skipped.
                    continue
        yield from _pages_that_take_a_slug()

    def test_no_cached_page_can_carry_a_case_id(self):
        checked, cacheable = [], []
        for name, url in self._cached_pages():
            # A fresh visitor per page. Reusing one client silently defeats
            # this test: some routes clear the case out of the session, and
            # every page after that would be checked with nothing to find.
            response = mid_appeal_client().get(url)
            if response.status_code != 200:
                continue
            checked.append(name)
            if "public" not in response.headers.get("Cache-Control", ""):
                continue
            cacheable.append(name)
            self.assertNotIn(
                CASE_UUID,
                response.content.decode(errors="replace"),
                msg=(
                    f"{name} is served with Cache-Control: public and carries "
                    "the case id of the visitor who asked for it"
                ),
            )

        # Floors, so a sweep that silently stops finding pages fails instead
        # of passing empty.
        self.assertGreater(len(checked), 15, f"only reached {checked}")
        self.assertGreater(len(cacheable), 15, f"only found {cacheable} cacheable")
        # And every per-slug page the site has content for, because those
        # are the ones this sweep used to miss entirely. A route whose
        # registry is empty in this environment yields no URL and is not
        # required; one that yields a URL has to answer.
        for route, _url in _pages_that_take_a_slug():
            self.assertIn(
                route,
                checked,
                f"{route} has content and was not reached by the sweep",
            )

    def test_no_cached_page_can_carry_who_is_signed_in(self):
        """A case id is not the only thing a visitor brings.

        The sweep above carries a case in the session. This one carries a
        signed-in account and a flash message naming it, because a page
        added later that greets somebody by name would be served to
        everybody who followed them, and nothing in the sweep above would
        have noticed.
        """
        checked = []
        for name, url in self._cached_pages():
            with self.subTest(route=name):
                cache.clear()
                response = self._a_visitor_with_something_to_lose().get(url)
                if response.status_code != 200:
                    continue
                if "public" not in response.headers.get("Cache-Control", ""):
                    continue
                checked.append(name)
                body = response.content.decode(errors="replace")
                self.assertNotIn(
                    VISITOR_EMAIL,
                    body,
                    msg=f"{name} is cached publicly and names the visitor",
                )
                self.assertNotIn(
                    FLASH_TEXT,
                    body,
                    msg=f"{name} is cached publicly and carries their message",
                )

        self.assertGreater(len(checked), 15, f"only found {checked} cacheable")

    def _a_visitor_with_something_to_lose(self):
        """Signed in, mid appeal, and carrying a flash message."""
        from django.contrib.auth import get_user_model
        from django.contrib.messages.storage.session import SessionStorage

        users = get_user_model().objects
        user = users.create_user(
            username=f"swept-page-visitor-{users.count()}",
            email=VISITOR_EMAIL,
            password="not-a-real-password",
        )
        client = Client()
        client.force_login(user)
        session = client.session
        session["denial_uuid"] = CASE_UUID
        session.save()

        request = RequestFactory().get("/")
        request.session = client.session
        storage = SessionStorage(request)
        storage.add(40, FLASH_TEXT)
        storage.update(None)
        request.session.save()
        client.cookies[settings.SESSION_COOKIE_NAME] = request.session.session_key
        return client

    def test_nothing_else_caches_a_whole_page_of_html(self):
        """The tripwire under the sweep above.

        A page cached anywhere but ``StaticIshView`` would not be swept, so
        adding one has to be a decision somebody makes on purpose rather
        than a page nobody checks. Counting only in urls.py was not enough:
        llms.txt and robots.txt carry their own cache_page decorators in
        agent_docs.py, so the whole package is searched.

        The exceptions are all endpoints that render no template, so they
        cannot carry base.html's visitor metadata whatever the cache does.
        The test below proves that rather than trusting it.
        """
        package = Path(__file__).resolve().parent.parent.parent / "fighthealthinsurance"

        found = {
            str(path.relative_to(package)): path.read_text().count("cache_page(")
            for path in package.rglob("*.py")
            if "cache_page(" in path.read_text()
        }

        self.assertEqual(
            found,
            {
                # StaticIshView's own wrapping, which the sweep covers.
                "views.py": 1,
                # The sitemap: XML, no template, no visitor context.
                "urls.py": 1,
                # llms.txt and robots.txt: markdown and plain text.
                "agent_docs.py": 2,
            },
            "something new caches a whole response. If it renders a template "
            "it belongs on StaticIshView, which blanks the visitor's context "
            "and is covered by the sweep above; if it does not, add it here "
            "and to the content-type test below.",
        )

    def test_no_cached_page_renders_around_the_mixin(self):
        """Parameterized pages are cached too, and the sweep cannot reach them.

        Blog posts, microsites and state help are all StaticIshView
        subclasses, and reverse() skips them because they take arguments. A
        subclass that defines its own render_to_response would therefore
        bypass the blanking with nothing to notice. They all inherit it, and
        this says so, which covers the pages the sweep cannot request.
        """
        from fighthealthinsurance import views as site_views

        own = [
            cls.__name__
            for cls in vars(site_views).values()
            if isinstance(cls, type)
            and issubclass(cls, StaticIshView)
            and "render_to_response" in vars(cls)
        ]

        self.assertEqual(
            own,
            [],
            "these cached views render their own way, around the blanking: " "%s" % own,
        )

    def test_the_decorator_is_not_imported_under_another_name(self):
        """The count above is textual, so an alias would walk past it."""
        package = Path(__file__).resolve().parent.parent.parent / "fighthealthinsurance"

        aliased = [
            str(path.relative_to(package))
            for path in package.rglob("*.py")
            if "cache_page as " in path.read_text()
        ]

        self.assertEqual(
            aliased, [], "cache_page is imported under an alias in %s" % aliased
        )

    def test_the_cached_endpoints_outside_that_base_class_are_not_html(self):
        """Why those exceptions are safe, asserted rather than assumed.

        base.html is where the visitor's case id is emitted, so an endpoint
        that renders no HTML cannot leak it however long it is cached.
        """
        for name in ("llms_txt", "robots_txt", "django.contrib.sitemaps.views.sitemap"):
            with self.subTest(route=name):
                response = mid_appeal_client().get(reverse(name))

                self.assertEqual(response.status_code, 200)
                self.assertNotIn(
                    "text/html",
                    response.headers.get("Content-Type", ""),
                    f"{name} is cached and now serves HTML, so it can carry "
                    "the visitor's case id",
                )
                self.assertNotIn(CASE_UUID, response.content.decode(errors="replace"))


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

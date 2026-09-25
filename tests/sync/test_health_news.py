"""The resources page does not wait on kffhealthnews.org.

Every fetch here is a fake standing where the transport adapter stands:
what is measured is how many times the network is asked, whether the
feeds are asked together, how long a page view can be held by a feed that
does not answer, and what a hostile or broken response can and cannot do.
"""

import gzip
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from fighthealthinsurance import health_news

LOCMEM = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "health-news-tests",
    }
}

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>{name}</title>
<item><title>{name} headline one</title><link>https://kffhealthnews.org/{slug}/1</link>
<pubDate>Mon, 21 Sep 2026 10:00:00 +0000</pubDate></item>
<item><title>{name} headline two</title><link>https://kffhealthnews.org/{slug}/2</link></item>
</channel></rss>"""

# A few bytes of nested entity declarations that a forgiving parser turns
# into gigabytes of title.
RSS_ENTITIES = """<?xml version="1.0"?>
<!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>
<rss version="2.0"><channel><title>x</title>
<item><title>&b;&b;&b;</title><link>https://kffhealthnews.org/x/1</link></item>
</channel></rss>"""

# The same declarations, spelled so that no byte check can see them: a
# declaration naming UTF-7, and the rest of the document in UTF-7, where
# "<!" is "+ADwAIQ-".
RSS_UTF7 = b'<?xml version="1.0" encoding="utf-7"?>' + RSS_ENTITIES.split("?>", 1)[
    1
].encode("utf-7")

# The same again, three ways feedparser could be talked into UTF-7 after a
# declaration says utf-8: a second processing instruction on the first
# line, a processing instruction with no declaration at all, and an
# element on the first line carrying an encoding attribute.
UTF7_REST = RSS_ENTITIES.split("?>", 1)[1].encode("utf-7")
RSS_UTF7_VARIANTS = (
    b'<?xml version="1.0" encoding="utf-8"?><?probe encoding="utf-7"?>' + UTF7_REST,
    b'<?probe encoding="utf-7"?>' + UTF7_REST,
    b'<?xml version="1.0" encoding="utf-8"?><rss encoding="utf-7">' + UTF7_REST,
)

RSS_PI_IN_TITLE = """<?xml version="1.0"?><rss version="2.0"><channel><title>{name}</title>
<item><title><![CDATA[Before <?example?> after]]></title><link>https://kffhealthnews.org/{slug}/1</link></item>
</channel></rss>"""

RSS_MANY_OPENERS = """<?xml version="1.0"?><rss version="2.0"><channel><title>{name}</title>
<item><title><![CDATA[{openers}]]></title><link>https://kffhealthnews.org/{slug}/1</link></item>
</channel></rss>"""

# A namespace the length of a novel, and more distinct tag names than any feed
# has: the two things a tree builder multiplies together.
RSS_HUGE_NAMESPACE = (
    '<?xml version="1.0"?><rss version="2.0" xmlns:x="' + "u" * 5000 + '"><channel>'
    "<title>x</title><item><title>t</title><link>https://kffhealthnews.org/x/1</link>"
    "<x:a/></item></channel></rss>"
)
RSS_MANY_TAGS = (
    '<?xml version="1.0"?><rss version="2.0" xmlns:x="urn:x"><channel><title>x</title>'
    "<item><title>t</title><link>https://kffhealthnews.org/x/1</link>"
    + "".join(f"<x:t{n}/>" for n in range(1000))
    + "</item></channel></rss>"
)

RSS_LONG_TITLE = """<?xml version="1.0"?><rss version="2.0"><channel><title>{name}</title>
<item><title>{long}</title><link>https://kffhealthnews.org/{slug}/0</link></item>
<item><title>{name} headline one</title><link>https://kffhealthnews.org/{slug}/1</link></item>
</channel></rss>"""

RSS_NO_LINKS = """<?xml version="1.0"?><rss version="2.0"><channel><title>{name}</title>
<item><title>{name} headline with nowhere to go</title></item>
</channel></rss>"""


def cached_feed(slug: str, **extra) -> dict:
    """A feed as the cache holds it. With no extra keys, the shape cached
    before feeds carried the time they were fetched."""
    return {
        "name": f"KFF Health News - {slug}",
        "description": f"{slug} news",
        "articles": [
            {
                "title": f"{slug} cached headline",
                "url": f"https://kffhealthnews.org/{slug}/1",
                "published_date": datetime(2026, 9, 21, 10, 0),
                "formatted_date": "Sep 21, 2026",
            }
        ],
        **extra,
    }


# The feeds as configured. Every test in every suite runs with FEEDS emptied
# by the conftest fixture so no page load reaches KFF; these tests are about
# the feeds, so they put the real table back.
REAL_FEEDS = dict(health_news.FEEDS)


class FakeRaw:
    """The raw stream, as urllib3 hands it over: read1 gives back what one
    read produced, up to the size asked for."""

    def __init__(self, body: bytes, endless: bool = False, trickle: float = 0.0):
        self.body = body
        self.endless = endless
        self.trickle = trickle
        self.at = 0
        self.reads = 0

    def read1(self, amt: int) -> bytes:
        self.reads += 1
        if self.endless:
            if self.trickle:
                time.sleep(self.trickle)
                return b"x" * 8
            return b"x" * amt
        piece = self.body[self.at : self.at + amt]
        self.at += len(piece)
        return piece


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers=None, **raw):
        self.status_code = status
        self.headers = headers or {}
        self.raw = FakeRaw(body, **raw)
        self.closed = False

    def close(self):
        self.closed = True


class FakeKff:
    """Stands where the transport adapter stands, and writes down every call."""

    def __init__(
        self,
        delay: float = 0.0,
        failing: str = "",
        status: int = 200,
        not_a_feed: str = "",
        endless: str = "",
        no_links: str = "",
        trickling: str = "",
        entities: str = "",
        utf16: str = "",
        gzip_bomb: str = "",
        gzipped: str = "",
        utf7: str = "",
        utf7_variant: int = -1,
        gzip_two: str = "",
        gzip_cut: str = "",
        long_title: str = "",
        pi_in_title: str = "",
        many_openers: str = "",
        huge_namespace: str = "",
        many_tags: str = "",
    ):
        self.huge_namespace = huge_namespace
        self.many_tags = many_tags
        self.pi_in_title = pi_in_title
        self.many_openers = many_openers
        self.utf7 = utf7
        self.utf7_variant = utf7_variant
        self.gzip_two = gzip_two
        self.gzip_cut = gzip_cut
        self.long_title = long_title
        self.delay = delay
        self.failing = failing
        self.status = status
        self.not_a_feed = not_a_feed
        self.endless = endless
        self.no_links = no_links
        self.trickling = trickling
        self.entities = entities
        self.utf16 = utf16
        self.gzip_bomb = gzip_bomb
        self.gzipped = gzipped
        self.calls: list = []
        self.responses: list = []
        self._lock = threading.Lock()

    def _answer(self, response: FakeResponse) -> FakeResponse:
        with self._lock:
            self.responses.append(response)
        return response

    def __call__(self, url, timeout):
        with self._lock:
            self.calls.append(
                {
                    "url": url,
                    "timeout": timeout,
                    "thread": threading.current_thread().name,
                    "at": time.monotonic(),
                }
            )
        if self.delay:
            time.sleep(self.delay)
        slug = url.rstrip("/").split("/")[-2]
        feed = RSS.format(name=slug, slug=slug).encode()
        if self.failing and self.failing in url:
            raise requests.ConnectionError("kff is down")
        if self.not_a_feed and self.not_a_feed in url:
            return self._answer(
                FakeResponse(200, b"<html><body>Page not found</body></html>")
            )
        if self.endless and self.endless in url:
            return self._answer(FakeResponse(200, b"", endless=True))
        if self.trickling and self.trickling in url:
            return self._answer(FakeResponse(200, b"", endless=True, trickle=0.02))
        if self.entities and self.entities in url:
            return self._answer(FakeResponse(200, RSS_ENTITIES.encode()))
        if self.utf16 and self.utf16 in url:
            return self._answer(FakeResponse(200, RSS_ENTITIES.encode("utf-16")))
        if self.no_links and self.no_links in url:
            return self._answer(
                FakeResponse(200, RSS_NO_LINKS.format(name=slug).encode())
            )
        if self.gzip_bomb and self.gzip_bomb in url:
            return self._answer(
                FakeResponse(
                    200,
                    gzip.compress(b"\x00" * (8 * 1024 * 1024)),
                    headers={"Content-Encoding": "gzip"},
                )
            )
        if self.gzipped and self.gzipped in url:
            return self._answer(
                FakeResponse(
                    200, gzip.compress(feed), headers={"Content-Encoding": "gzip"}
                )
            )
        if self.gzip_two and self.gzip_two in url:
            half = len(feed) // 2
            two = gzip.compress(feed[:half]) + gzip.compress(feed[half:])
            return self._answer(
                FakeResponse(200, two, headers={"Content-Encoding": "gzip"})
            )
        if self.gzip_cut and self.gzip_cut in url:
            return self._answer(
                FakeResponse(
                    200, gzip.compress(feed)[:-8], headers={"Content-Encoding": "gzip"}
                )
            )
        if self.utf7 and self.utf7 in url:
            body = (
                RSS_UTF7
                if self.utf7_variant < 0
                else RSS_UTF7_VARIANTS[self.utf7_variant]
            )
            return self._answer(FakeResponse(200, body))
        if self.huge_namespace and self.huge_namespace in url:
            return self._answer(FakeResponse(200, RSS_HUGE_NAMESPACE.encode()))
        if self.many_tags and self.many_tags in url:
            return self._answer(FakeResponse(200, RSS_MANY_TAGS.encode()))
        if self.pi_in_title and self.pi_in_title in url:
            body = RSS_PI_IN_TITLE.format(name=slug, slug=slug)
            return self._answer(FakeResponse(200, body.encode()))
        if self.many_openers and self.many_openers in url:
            body = RSS_MANY_OPENERS.format(name=slug, slug=slug, openers="<?" * 48_000)
            return self._answer(FakeResponse(200, body.encode()))
        if self.long_title and self.long_title in url:
            body = RSS_LONG_TITLE.format(name=slug, slug=slug, long="t" * 5000)
            return self._answer(FakeResponse(200, body.encode()))
        return self._answer(FakeResponse(self.status, feed))

    def calls_for(self, fragment: str) -> int:
        return sum(1 for call in self.calls if fragment in call["url"])


def faking(kff: FakeKff):
    return patch.object(health_news, "_send", side_effect=kff)


@override_settings(CACHES=LOCMEM)
class HealthNewsTest(TestCase):
    def setUp(self):
        cache.clear()
        feeds = patch.object(health_news, "FEEDS", REAL_FEEDS)
        feeds.start()
        self.addCleanup(feeds.stop)
        # A pool per test, drained at the end of it, so the fetch a test
        # leaves sleeping past its budget cannot queue the next test's.
        pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="fhi-health-news")
        patcher = patch.object(health_news, "health_news_executor", pool)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(pool.shutdown, wait=True)

    def _primed(self) -> dict:
        """Every feed fetched and fresh, then the fresh copies expired.

        The registry's cleanup callback runs a moment after the waiter is
        released, so it is waited for; a finished future left there would
        otherwise answer the next call with the old headlines.
        """
        with faking(FakeKff()):
            before = health_news.get_health_news()
        deadline = time.monotonic() + 2.0
        while health_news._in_flight and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(health_news._in_flight, {}, "fetches still registered")
        for feed_key in REAL_FEEDS:
            cache.delete(health_news._fresh_key(feed_key))
        return before

    def test_the_fetches_run_on_their_own_pool(self):
        """Not on one that generation, bridging or cleaning depends on."""
        from fighthealthinsurance.exec import health_news_executor

        name = health_news_executor.submit(
            lambda: threading.current_thread().name
        ).result(timeout=5)
        self.assertTrue(name.startswith("fhi-health-news"), name)

    def test_the_request_goes_through_the_adapter_not_session_send(self):
        """Session.send resolves redirects even with them off, reading a
        3xx body in full on the way; the adapter reads nothing."""
        stub = FakeResponse(200, b"")
        with patch.object(
            requests.Session, "send", side_effect=AssertionError("Session.send")
        ):
            with patch.object(
                requests.adapters.HTTPAdapter, "send", return_value=stub
            ) as adapter:
                self.assertIs(
                    health_news._send("https://kffhealthnews.org/x/feed/", 1.5), stub
                )
        args, kwargs = adapter.call_args
        self.assertEqual(kwargs["timeout"], 1.5)
        self.assertTrue(kwargs["stream"])
        self.assertEqual(args[0].headers["Accept-Encoding"], "identity")

    def test_the_second_request_comes_from_the_cache(self):
        kff = FakeKff()
        with faking(kff):
            first = health_news.get_health_news()
            second = health_news.get_health_news()
        self.assertEqual(len(kff.calls), len(REAL_FEEDS))
        self.assertEqual(first, second)
        self.assertEqual(
            [feed["articles"][0]["title"] for feed in first.values()],
            [
                "insurance headline one",
                "uninsured headline one",
                "health-industry headline one",
            ],
        )

    def test_the_feeds_are_fetched_together_not_in_turn(self):
        """Three feeds at 0.4s each take about 0.4s, not 1.2s."""
        kff = FakeKff(delay=0.4)
        started = time.monotonic()
        with faking(kff):
            found = health_news.get_health_news()
        elapsed = time.monotonic() - started
        self.assertEqual(len(found), 3)
        self.assertLess(elapsed, 0.9, f"three 0.4s fetches took {elapsed:.2f}s")
        self.assertEqual(
            len({call["thread"] for call in kff.calls}), 3, "not on separate threads"
        )
        self.assertTrue(
            all(call["thread"].startswith("fhi-health-news") for call in kff.calls),
            "fetched on a pool that generation or bridging depends on",
        )

    def test_a_feed_that_does_not_answer_cannot_hold_the_page(self):
        """The budget, not the slow feed, decides the wait."""
        kff = FakeKff(delay=1.0)
        started = time.monotonic()
        with patch.object(health_news, "TOTAL_BUDGET_SECONDS", 0.2):
            with faking(kff):
                found = health_news.get_health_news()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.8, f"a 1s feed held the page for {elapsed:.2f}s")
        self.assertEqual(found, {}, "nothing was fetched in time, so nothing shows")

    def test_a_feed_that_fails_keeps_its_last_good_copy_and_is_left_alone(self):
        before = self._primed()
        down = FakeKff(failing="uninsured")
        with faking(down):
            during = health_news.get_health_news()
            again = health_news.get_health_news()
        self.assertEqual(during["uninsured"], before["uninsured"])
        self.assertEqual(again["uninsured"], before["uninsured"])
        self.assertEqual(
            down.calls_for("uninsured"), 1, "a failed feed was asked again at once"
        )
        self.assertEqual(set(during), set(REAL_FEEDS))

    def test_a_feed_that_fails_with_nothing_kept_is_left_out(self):
        with faking(FakeKff(failing="uninsured")):
            found = health_news.get_health_news()
        self.assertEqual(set(found), {"insurance", "health-industry"})

    def test_a_not_found_feed_counts_as_a_failure(self):
        with faking(FakeKff(status=404)):
            found = health_news.get_health_news()
        self.assertEqual(found, {})
        self.assertTrue(cache.get(health_news._failed_key("insurance")))

    def test_a_redirect_is_a_failure_and_its_body_is_never_read(self):
        """A moved feed is a URL for us to update, not somewhere to follow."""
        kff = FakeKff(status=301)
        with faking(kff):
            found = health_news.get_health_news()
        self.assertEqual(found, {})
        self.assertTrue(cache.get(health_news._failed_key("insurance")))
        self.assertEqual({r.raw.reads for r in kff.responses}, {0})
        self.assertTrue(all(r.closed for r in kff.responses))

    def test_a_page_that_is_not_a_feed_does_not_replace_the_last_good_copy(self):
        """A 200 with no entries in it is an HTML error page, not headlines."""
        before = self._primed()
        with faking(FakeKff(not_a_feed="uninsured")):
            after = health_news.get_health_news()
        self.assertEqual(after["uninsured"], before["uninsured"])
        self.assertTrue(cache.get(health_news._failed_key("uninsured")))

    def test_entries_without_links_do_not_replace_the_last_good_copy(self):
        before = self._primed()
        with faking(FakeKff(no_links="uninsured")):
            after = health_news.get_health_news()
        self.assertEqual(after["uninsured"], before["uninsured"])
        self.assertTrue(cache.get(health_news._failed_key("uninsured")))

    def test_entity_declarations_are_refused_before_parsing(self):
        """A few hundred bytes of nested entities become gigabytes of
        title inside feedparser; the byte limit cannot see that."""
        before = self._primed()
        started = time.monotonic()
        kff = FakeKff(entities="uninsured")
        with faking(kff):
            after = health_news.get_health_news()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(kff.responses and kff.responses[0].raw.reads > 0, "not served")
        self.assertEqual(after["uninsured"], before["uninsured"])
        self.assertTrue(cache.get(health_news._failed_key("uninsured")))

    def test_a_utf16_body_cannot_carry_the_same_declarations_past_the_check(self):
        before = self._primed()
        started = time.monotonic()
        with faking(FakeKff(utf16="uninsured")):
            after = health_news.get_health_news()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(after["uninsured"], before["uninsured"])
        self.assertTrue(cache.get(health_news._failed_key("uninsured")))

    def test_a_declared_utf7_encoding_cannot_smuggle_declarations_past_the_check(self):
        """feedparser honours the declaration's encoding; the body is decoded
        here as UTF-8 and made to say so, so "+ADwAIQ-" stays four characters."""
        before = self._primed()
        started = time.monotonic()
        kff = FakeKff(utf7="uninsured")
        with faking(kff):
            after = health_news.get_health_news()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(kff.responses and kff.responses[0].raw.reads > 0, "not served")
        self.assertEqual(after["uninsured"], before["uninsured"])
        self.assertTrue(cache.get(health_news._failed_key("uninsured")))
        for feed in after.values():
            for article in feed["articles"]:
                self.assertLessEqual(len(article["title"]), health_news.MAX_TITLE_CHARS)

    def test_no_second_instruction_or_first_line_element_can_name_another_encoding(
        self,
    ):
        for variant in range(len(RSS_UTF7_VARIANTS)):
            with self.subTest(variant=variant):
                cache.clear()
                before = self._primed()
                kff = FakeKff(utf7="uninsured", utf7_variant=variant)
                started = time.monotonic()
                with faking(kff):
                    after = health_news.get_health_news()
                self.assertLess(time.monotonic() - started, 2.0)
                self.assertTrue(kff.responses and kff.responses[0].raw.reads > 0)
                self.assertEqual(after["uninsured"], before["uninsured"])
                self.assertTrue(cache.get(health_news._failed_key("uninsured")))
                for feed in after.values():
                    for article in feed["articles"]:
                        self.assertLessEqual(
                            len(article["title"]), health_news.MAX_TITLE_CHARS
                        )

    def test_a_document_shaped_to_balloon_in_a_tree_builder_costs_nothing_here(self):
        """A namespace the length of a novel and a thousand distinct tag names
        are what a tree builder multiplies together; nothing here keeps
        either, so both read like any other feed."""
        for option in ("huge_namespace", "many_tags"):
            with self.subTest(option=option):
                cache.clear()
                kff = FakeKff(**{option: "insurance"})
                started = time.monotonic()
                with faking(kff):
                    found = health_news.get_health_news()
                self.assertLess(time.monotonic() - started, 2.0)
                self.assertEqual(
                    [a["title"] for a in found["insurance"]["articles"]], ["t"]
                )

    def test_a_document_broken_after_its_newest_items_is_not_a_feed(self):
        """Three good items and then a broken tail: read to the end, and
        refused, rather than cached on the strength of the first three."""
        before = self._primed()
        good = RSS.format(name="uninsured", slug="uninsured")
        items = "".join(
            f"<item><title>h{n}</title><link>https://kffhealthnews.org/u/{n}</link></item>"
            for n in range(3)
        )
        broken = good.replace("</channel>", items + "<item><title>unclosed</channel>")
        with patch.object(health_news, "_send") as send:
            send.side_effect = lambda url, timeout: FakeResponse(200, broken.encode())
            after = health_news.get_health_news()
        self.assertEqual(after["uninsured"], before["uninsured"])
        self.assertTrue(cache.get(health_news._failed_key("uninsured")))

    def test_a_document_nested_deeper_than_a_feed_is_refused(self):
        deep = '<?xml version="1.0"?>' + "<a>" * 5000 + "</a>" * 5000
        with faking(FakeKff()):
            with patch.object(health_news, "_send") as send:
                send.side_effect = lambda url, timeout: FakeResponse(200, deep.encode())
                started = time.monotonic()
                found = health_news.get_health_news()
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(found, {})

    def test_a_processing_instruction_inside_a_headline_is_left_alone(self):
        with faking(FakeKff(pi_in_title="insurance")):
            found = health_news.get_health_news()
        self.assertEqual(
            found["insurance"]["articles"][0]["title"], "Before <?example?> after"
        )

    def test_many_unclosed_openers_do_not_cost_more_than_a_moment(self):
        """96KB of "<?" with no "?>" is text, not a pattern to scan twice over."""
        started = time.monotonic()
        with faking(FakeKff(many_openers="insurance")):
            found = health_news.get_health_news()
        self.assertLess(time.monotonic() - started, 1.5)
        # That text is the entry's whole title, so the entry is dropped as
        # oversized and the feed has nothing usable: a failure, and a quick one.
        self.assertNotIn("insurance", found)
        self.assertTrue(cache.get(health_news._failed_key("insurance")))

    def test_an_entry_past_the_size_of_a_headline_is_not_one(self):
        with faking(FakeKff(long_title="insurance")):
            found = health_news.get_health_news()
        titles = [a["title"] for a in found["insurance"]["articles"]]
        self.assertEqual(titles, ["insurance headline one"])

    def test_a_headline_that_runs_on_past_the_size_is_not_kept_as_its_prefix(self):
        """Short words, then whitespace, then a great deal more: the buffer
        would hold a tidy prefix, and the item is refused anyway."""
        body = RSS_LONG_TITLE.format(
            name="insurance",
            slug="insurance",
            long="Tidy prefix" + " " * 20 + "t" * 5000,
        ).encode()
        with patch.object(health_news, "_send") as send:
            send.side_effect = lambda url, timeout: FakeResponse(200, body)
            found = health_news.get_health_news()
        titles = [a["title"] for a in found["insurance"]["articles"]]
        self.assertEqual(titles, ["insurance headline one"])

    def test_a_compressed_body_must_be_whole_and_one_member(self):
        before = self._primed()
        with faking(FakeKff(gzip_two="uninsured")):
            after = health_news.get_health_news()
        self.assertEqual(after["uninsured"], before["uninsured"])
        self.assertTrue(cache.get(health_news._failed_key("uninsured")))

        cache.clear()
        before = self._primed()
        with faking(FakeKff(gzip_cut="uninsured")):
            after = health_news.get_health_news()
        self.assertEqual(after["uninsured"], before["uninsured"])
        self.assertTrue(cache.get(health_news._failed_key("uninsured")))

    def test_an_endless_response_is_cut_off_at_the_byte_limit(self):
        kff = FakeKff(endless="insurance")
        started = time.monotonic()
        with faking(kff):
            found = health_news.get_health_news()
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertNotIn("insurance", found)
        self.assertTrue(cache.get(health_news._failed_key("insurance")))
        self.assertTrue(all(r.closed for r in kff.responses))

    def test_a_server_that_trickles_is_cut_off_at_the_deadline(self):
        """Bytes arriving inside the socket timeout never trip it; the
        fetch's own deadline has to, piece by piece."""
        kff = FakeKff(trickling="insurance")
        started = time.monotonic()
        with patch.object(health_news, "FETCH_TIMEOUT_SECONDS", 0.3):
            with faking(kff):
                found = health_news.get_health_news()
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertNotIn("insurance", found)
        self.assertTrue(cache.get(health_news._failed_key("insurance")))

    def test_a_compressed_answer_is_inflated_under_the_same_limit(self):
        with faking(FakeKff(gzipped="insurance")):
            found = health_news.get_health_news()
        self.assertEqual(
            found["insurance"]["articles"][0]["title"], "insurance headline one"
        )

        cache.clear()
        started = time.monotonic()
        with faking(FakeKff(gzip_bomb="insurance")):
            found = health_news.get_health_news()
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertNotIn("insurance", found)
        self.assertTrue(cache.get(health_news._failed_key("insurance")))

    def test_two_cold_requests_at_once_fetch_each_feed_once(self):
        kff = FakeKff(delay=0.3)
        with faking(kff):
            with ThreadPoolExecutor(max_workers=2) as visitors:
                first, second = [
                    visitor.result()
                    for visitor in [
                        visitors.submit(health_news.get_health_news) for _ in range(2)
                    ]
                ]
        self.assertEqual(len(kff.calls), len(REAL_FEEDS), "a feed was fetched twice")
        self.assertEqual(first, second)

    def test_a_miss_another_request_just_filled_is_not_fetched_again(self):
        """Between one request's cache check and its submission, another
        can finish the same feed; the submission rechecks the cache."""
        kff = FakeKff()
        with faking(kff):
            first = health_news.get_health_news()
            settled = health_news._fetch_once("insurance")
        self.assertEqual(settled.result(timeout=1), first["insurance"])
        self.assertEqual(kff.calls_for("insurance"), 1)

        cache.delete(health_news._fresh_key("insurance"))
        cache.set(health_news._failed_key("insurance"), True, 60)
        with faking(kff):
            left_alone = health_news._fetch_once("insurance")
        self.assertIsNotNone(left_alone.exception(timeout=1))
        self.assertEqual(kff.calls_for("insurance"), 1)

    def test_each_request_carries_a_short_timeout(self):
        kff = FakeKff()
        with faking(kff):
            health_news.get_health_news()
        self.assertEqual(
            {call["timeout"] for call in kff.calls}, {health_news.FETCH_TIMEOUT_SECONDS}
        )
        self.assertLessEqual(health_news.FETCH_TIMEOUT_SECONDS, 5.0)

    def test_a_cache_that_cannot_be_written_does_not_cost_the_headlines(self):
        with patch.object(health_news.cache, "set", side_effect=RuntimeError("full")):
            with faking(FakeKff()):
                found = health_news.get_health_news()
        self.assertEqual(set(found), set(REAL_FEEDS))

    def test_a_cache_that_cannot_be_read_does_not_cost_the_headlines(self):
        with patch.object(health_news.cache, "get", side_effect=RuntimeError("down")):
            with faking(FakeKff(failing="uninsured")):
                found = health_news.get_health_news()
        self.assertEqual(set(found), {"insurance", "health-industry"})

    def test_each_feed_carries_the_time_it_was_fetched(self):
        """In what the page is handed and in both cached copies, so the
        last good copy still knows its age when it is all there is."""
        started = datetime.now(timezone.utc)
        with faking(FakeKff()):
            found = health_news.get_health_news()
        finished = datetime.now(timezone.utc)
        self.assertEqual(set(found), set(REAL_FEEDS))
        for feed_key, feed in found.items():
            with self.subTest(feed=feed_key):
                fetched_at = feed["fetched_at"]
                self.assertIsNotNone(fetched_at.utcoffset(), "a naive time")
                self.assertTrue(started <= fetched_at <= finished, fetched_at)
                for key in (
                    health_news._fresh_key(feed_key),
                    health_news._last_good_key(feed_key),
                ):
                    self.assertEqual(cache.get(key)["fetched_at"], fetched_at, key)
        self.assertEqual(
            health_news.oldest_fetch(found),
            min(feed["fetched_at"] for feed in found.values()),
        )

    def test_a_last_good_copy_keeps_its_own_time_and_the_page_goes_by_it(self):
        """The honest age is the oldest feed shown. A copy kept through an
        outage is days old while the other two were fetched just now."""
        self._primed()
        days_ago = datetime.now(timezone.utc) - timedelta(days=3)
        kept = cache.get(health_news._last_good_key("uninsured"))
        kept["fetched_at"] = days_ago
        cache.set(health_news._last_good_key("uninsured"), kept, 60)
        with faking(FakeKff(failing="uninsured")):
            during = health_news.get_health_news()
        self.assertEqual(set(during), set(REAL_FEEDS))
        self.assertEqual(during["uninsured"]["fetched_at"], days_ago)
        self.assertGreater(during["insurance"]["fetched_at"], days_ago)
        self.assertEqual(health_news.oldest_fetch(during), days_ago)

        # And on the path that skips a feed left alone after a failure.
        with faking(FakeKff(failing="uninsured")) as send:
            again = health_news.get_health_news()
        self.assertEqual(send.call_count, 0, "a failed feed was asked again at once")
        self.assertEqual(health_news.oldest_fetch(again), days_ago)

    def test_a_feed_with_no_time_on_it_means_no_age_rather_than_a_guess(self):
        now = datetime.now(timezone.utc)
        timed = {"insurance": cached_feed("insurance", fetched_at=now)}
        self.assertEqual(health_news.oldest_fetch(timed), now)
        for label, feeds in (
            ("no feeds", {}),
            ("cached before times", {"insurance": cached_feed("insurance")}),
            (
                "one of two untimed",
                {**timed, "uninsured": cached_feed("uninsured")},
            ),
            (
                "a naive time",
                {"insurance": cached_feed("insurance", fetched_at=datetime.now())},
            ),
            (
                "not a time",
                {"insurance": cached_feed("insurance", fetched_at="yesterday")},
            ),
            ("not a feed", {"insurance": None}),
        ):
            with self.subTest(label):
                self.assertIsNone(health_news.oldest_fetch(feeds))

    def test_the_uninsured_feed_points_where_kff_moved_it(self):
        """KFF turned the topic into a tag; the old URL is a 404."""
        self.assertEqual(
            REAL_FEEDS["uninsured"]["url"],
            "https://kffhealthnews.org/tag/uninsured/feed/",
        )
        for feed in REAL_FEEDS.values():
            self.assertNotIn("/topics/uninsured/", feed["url"])


@override_settings(CACHES=LOCMEM)
class ResourcesPageTest(TestCase):
    def setUp(self):
        cache.clear()
        feeds = patch.object(health_news, "FEEDS", REAL_FEEDS)
        feeds.start()
        self.addCleanup(feeds.stop)

    def test_the_page_renders_the_cached_headlines_without_the_network(self):
        kff = FakeKff()
        with faking(kff):
            health_news.get_health_news()
            response = self.client.get(reverse("other-resources"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "insurance headline one")
        self.assertEqual(len(kff.calls), len(REAL_FEEDS), "the page fetched again")

    def test_the_page_still_renders_when_the_feeds_blow_up(self):
        with patch.object(
            health_news, "get_health_news", side_effect=RuntimeError("no feeds")
        ):
            response = self.client.get(reverse("other-resources"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Latest Health Policy News")
        self.assertNotContains(response, "Headlines updated")

    def test_the_page_says_how_old_the_headlines_are(self):
        """By the oldest feed shown, in the words timesince uses."""
        now = datetime.now(timezone.utc)
        ages = {"insurance": 1, "uninsured": 3, "health-industry": 2}
        for feed_key, hours in ages.items():
            cache.set(
                health_news._fresh_key(feed_key),
                cached_feed(feed_key, fetched_at=now - timedelta(hours=hours)),
                60,
            )
        with faking(FakeKff()) as send:
            response = self.client.get(reverse("other-resources"))
        self.assertEqual(send.call_count, 0, "the page fetched past a fresh cache")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "uninsured cached headline")
        # timesince keeps its number and unit together with a no-break space.
        self.assertContains(
            response, '<p class="rss-updated">Headlines updated 3\xa0hours ago.</p>'
        )

    def test_headlines_cached_before_feeds_carried_a_time_show_no_line(self):
        """What the cache already holds on the day this ships: the page
        renders it as it always did, and says nothing about its age."""
        for feed_key in REAL_FEEDS:
            cache.set(health_news._fresh_key(feed_key), cached_feed(feed_key), 60)
        with faking(FakeKff()) as send:
            response = self.client.get(reverse("other-resources"))
        self.assertEqual(send.call_count, 0)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "insurance cached headline")
        self.assertNotContains(response, "Headlines updated")
        self.assertIsNone(response.context["rss_fetched_at"])

    def test_no_headlines_shown_means_no_line(self):
        with faking(FakeKff(failing="kffhealthnews.org")):
            response = self.client.get(reverse("other-resources"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["rss_feeds"], {})
        self.assertNotContains(response, "Headlines updated")

"""The KFF Health News headlines on the resources page.

Before this, every view of /other-resources fetched three feeds from
kffhealthnews.org one after another, ten seconds allowed for each, and kept
nothing: three seconds on a good day, thirty on a bad one, on a page that
is otherwise static. One of the three had been a 404 since KFF moved the
topic, so a second of that was spent fetching a "not found" page.

Now a feed is fetched at most once every FRESH_SECONDS per process and
served from the cache in between. On a miss the feeds are fetched together
rather than in turn, each with a short timeout and all of them under one
budget, so the slowest feed decides the wait rather than the sum. A feed
that fails keeps showing its last good copy for a week and is not asked
again for RETRY_AFTER_SECONDS, so an outage at KFF costs one bounded wait
per process rather than one per visitor.

Blocking: does network IO with ``requests``. Callers on the event loop must
bridge.
"""

import functools
import re
import threading
import time
import xml.parsers.expat
import zlib
from concurrent.futures import Future
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import requests
from django.core.cache import cache
from loguru import logger

from fighthealthinsurance.exec import health_news_executor

# KFF moved the uninsured topic to a tag: /topics/uninsured/ now redirects to
# /tag/uninsured/, and the old feed URL answered 404 (checked 2026-09-22).
FEEDS: Dict[str, Dict[str, str]] = {
    "insurance": {
        "name": "KFF Health News - Insurance",
        "url": "https://kffhealthnews.org/topics/insurance/feed/",
        "description": "Insurance-related health policy news",
    },
    "uninsured": {
        "name": "KFF Health News - Uninsured",
        "url": "https://kffhealthnews.org/tag/uninsured/feed/",
        "description": "News about uninsured populations and coverage",
    },
    "health-industry": {
        "name": "KFF Health News - Health Industry",
        "url": "https://kffhealthnews.org/topics/health-industry/feed/",
        "description": "Health industry news and analysis",
    },
}

ARTICLES_PER_FEED = 3
USER_AGENT = "Mozilla/5.0 (compatible; HealthPolicyRSSBot/1.0)"

# How long a fetched feed is served before KFF is asked again.
FRESH_SECONDS = 6 * 60 * 60
# How long the last good copy of a feed is kept, for when a fetch fails.
STALE_SECONDS = 7 * 24 * 60 * 60
# How long a feed that just failed is left alone before it is tried again.
RETRY_AFTER_SECONDS = 15 * 60
# Per request, connect and read. KFF answers in about a second.
FETCH_TIMEOUT_SECONDS = 5.0
# For all the misses on one page view together.
TOTAL_BUDGET_SECONDS = 6.0
# KFF's feeds are about 250KB. Anything past this is not a feed.
MAX_FEED_BYTES = 2 * 1024 * 1024
# A headline and a link have a size. An entry past these is not one.
MAX_TITLE_CHARS = 500
MAX_LINK_CHARS = 2000

# A leading XML declaration. Anchored, and linear whatever follows.
_LEADING_DECLARATION = re.compile(r"\A<\?xml\b[^>]*\?>")
# A feed's items sit at rss/channel/item; anything nested deeper than this
# is not a feed and is not walked.
MAX_DEPTH = 16
_UTF8_DECLARATION = '<?xml version="1.0" encoding="utf-8"?>\n'


def _fresh_key(feed_key: str) -> str:
    return f"health_news:fresh:{feed_key}"


def _last_good_key(feed_key: str) -> str:
    return f"health_news:last_good:{feed_key}"


def _failed_key(feed_key: str) -> str:
    return f"health_news:failed:{feed_key}"


def _send(url: str, timeout: float) -> requests.Response:
    """One GET through the transport adapter alone, nothing read yet.

    Session.send resolves redirects even with them switched off, and reads
    a 3xx body in full on the way, before anything here could bound it.
    The adapter hands back the response as it stands. identity encoding is
    asked for so the bytes counted are the bytes on the wire; a compressed
    answer is inflated under the same limit below.
    """
    session = requests.Session()
    request = session.prepare_request(
        requests.Request(
            "GET",
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
        )
    )
    return session.get_adapter(url).send(request, stream=True, timeout=timeout)


def _read_bounded(response: Any, limit: int, deadline: float) -> bytes:
    """The body, or an exception once it is too big or too slow.

    requests' timeout bounds the wait for each read, not the download, and
    its own readers fill a buffer or a whole chunk before handing anything
    over. read1 on the raw stream returns whatever one read produced, so
    the size and the deadline are checked piece by piece as bytes arrive.
    What this cannot bound is http.client's own reading of chunk framing,
    which happens before read1 returns: a server that trickled a chunk-size
    line could hold one of the three feed threads past the deadline. It
    could never hold the page, which waits under its own budget, and the
    feeds are KFF's, not anyone's.
    """
    raw = response.raw
    pieces: List[bytes] = []
    size = 0
    while True:
        piece = raw.read1(64 * 1024)
        if not piece:
            break
        size += len(piece)
        if size > limit:
            raise ValueError(f"more than {limit} bytes is not a feed")
        if time.monotonic() > deadline:
            raise TimeoutError("the download ran past its budget")
        pieces.append(piece)
    body = b"".join(pieces)
    encoding = (response.headers.get("Content-Encoding") or "identity").strip().lower()
    if encoding != "identity":
        body = _inflate_bounded(body, encoding, limit)
    return body


def _inflate_bounded(body: bytes, encoding: str, limit: int) -> bytes:
    """Decompress, refusing anything that would come out past the limit."""
    if encoding == "gzip":
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        inflater = zlib.decompressobj(zlib.MAX_WBITS)
    else:
        raise ValueError(f"{encoding!r} is not an encoding we read")
    out = inflater.decompress(body, limit + 1)
    if inflater.unconsumed_tail or len(out) > limit:
        raise ValueError(f"inflates past {limit} bytes; not a feed")
    out += inflater.flush()
    if len(out) > limit:
        raise ValueError(f"inflates past {limit} bytes; not a feed")
    if not inflater.eof or inflater.unused_data:
        # Cut short, or more than one member: either way not the whole feed,
        # and a partial one must not become the fresh or last good copy.
        raise ValueError("incomplete compressed body; not a feed")
    return out


def _as_text(body: bytes) -> str:
    """The body as text, UTF-8 and nothing else, declaring itself so.

    A parser honours whatever encoding the XML declaration names, so a
    body declaring UTF-7 could spell "<!" as "+ADwAIQ-", pass any check on
    the bytes, and be decoded into entity declarations afterwards. The
    body is decoded here, strictly, as the UTF-8 that KFF sends; its own
    declaration goes and ours stands first; and the declaration check
    runs on the text the parser will actually see.
    """
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("not UTF-8; not the feed we read") from None
    text = text.lstrip("\ufeff").lstrip()
    text = _LEADING_DECLARATION.sub("", text, count=1)
    if "<!ENTITY" in text or "<!DOCTYPE" in text:
        raise ValueError("entity declarations; not a feed we read")
    return _UTF8_DECLARATION + text


def _articles(text: str) -> List[Dict[str, Any]]:
    """The newest ARTICLES_PER_FEED items of an RSS 2.0 document.

    That is what KFF publishes and all this page shows. expat reads it
    directly, with namespace processing off and no tree built: a tree
    builder copies a namespace's URI into every name that uses it, and
    keeps every name, so a body under the byte limit could still balloon.
    Here nothing is kept but the three fields of the item being read, each
    in a bounded buffer; once enough items are in hand the rest is read and
    not kept, so a document that is malformed after its newest items is a
    failure rather than a cached success. expat is linear and has no
    regular expressions; with entity declarations refused before it sees
    the text, nothing expands.
    """
    articles: List[Dict[str, Any]] = []
    path: List[str] = []
    item: Optional[Dict[str, str]] = None
    field: Optional[str] = None
    buffer: List[str] = []
    buffered = 0
    cap = max(MAX_TITLE_CHARS, MAX_LINK_CHARS)

    def start(name: str, attributes: Dict[str, str]) -> None:
        nonlocal item, field, buffer, buffered
        path.append(name)
        if len(path) > MAX_DEPTH:
            raise ValueError("deeper than a feed")
        if item is None:
            if len(articles) < ARTICLES_PER_FEED and path == ["rss", "channel", "item"]:
                item = {"title": "", "link": "", "pubDate": ""}
        elif len(path) == 4 and name in item:
            field, buffer, buffered = name, [], 0

    def data(chunk: str) -> None:
        nonlocal buffered
        if field is None or item is None:
            return
        buffered += len(chunk)
        if buffered > cap:
            # Past the size of any headline or link: the item is not one,
            # whatever prefix the buffer holds.
            item["oversized"] = "yes"
        elif buffered <= cap:
            buffer.append(chunk)

    def end(name: str) -> None:
        nonlocal item, field, buffer
        if item is not None and field == name and len(path) == 4:
            item[name] = "".join(buffer)
            field, buffer = None, []
        elif item is not None and name == "item" and len(path) == 3:
            finished, item = item, None
            article = _article(finished)
            if article is not None:
                articles.append(article)
        path.pop()

    parser = xml.parsers.expat.ParserCreate()  # no namespace_separator: none processed
    parser.buffer_text = True
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = data
    try:
        parser.Parse(text.encode("utf-8"), True)
    except xml.parsers.expat.ExpatError as e:
        raise ValueError(f"not well-formed XML: {e}") from None
    if not articles:
        raise ValueError("no usable entries; not a feed")
    return articles


def _article(item: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """One item as the page shows it, or None if it is not the shape of one."""
    if item.get("oversized"):
        return None
    title = item["title"].strip()
    link = item["link"].strip()
    if not (title and link):
        return None
    if len(title) > MAX_TITLE_CHARS or len(link) > MAX_LINK_CHARS:
        return None
    published = datetime.now()
    pub_date = item["pubDate"].strip()
    if pub_date:
        try:
            published = (
                parsedate_to_datetime(pub_date)
                .astimezone(timezone.utc)
                .replace(tzinfo=None)
            )
        except (TypeError, ValueError, IndexError):
            pass
    return {
        "title": title,
        "url": link,
        "published_date": published,
        "formatted_date": published.strftime("%b %d, %Y"),
    }


def fetch_feed(url: str, timeout: Optional[float] = None) -> List[Dict[str, Any]]:
    """The newest ARTICLES_PER_FEED entries of one feed, or an exception.

    A response that is not a feed (a redirect, an HTML error page with a
    200, a moved topic, entries with no titles or links) is an exception
    too: it must not become the last good copy.
    """
    if timeout is None:
        timeout = FETCH_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    response = _send(url, timeout)
    try:
        if response.status_code != 200:
            # A moved feed is a URL for us to update, not somewhere to follow.
            raise ValueError(f"HTTP {response.status_code} is not a feed")
        body = _read_bounded(response, MAX_FEED_BYTES, deadline)
    finally:
        response.close()
    # A feed has no business declaring entities: a few hundred bytes of
    # nested declarations become gigabytes of text inside any parser that
    # expands them. NUL bytes are refused first, so a UTF-16 body cannot
    # carry a declaration past the check that _as_text runs on the text.
    if b"\x00" in body:
        raise ValueError("NUL bytes; not the UTF-8 feed we read")
    return _articles(_as_text(body))


def _fetch_and_remember(feed_key: str) -> Dict[str, Any]:
    info = FEEDS[feed_key]
    try:
        articles = fetch_feed(info["url"])
    except Exception:
        # Marked here as well as by the waiter, so a fetch that fails after
        # the page stopped waiting for it still leaves the feed alone.
        _remember(_failed_key(feed_key), True, RETRY_AFTER_SECONDS)
        raise
    feed = {
        "name": info["name"],
        "description": info["description"],
        "articles": articles,
    }
    _remember(_fresh_key(feed_key), feed, FRESH_SECONDS)
    _remember(_last_good_key(feed_key), feed, STALE_SECONDS)
    return feed


def _remember(key: str, value: Any, seconds: int) -> None:
    """A cache write that cannot cost the page what was just fetched."""
    try:
        cache.set(key, value, seconds)
    except Exception as e:
        logger.warning(f"health_news: could not cache {key}: {e!r}")


# One fetch per feed at a time. Two cold requests at once share the same
# future rather than each queueing a copy, and a copy queued behind a slow
# feed cannot run on after everyone stopped waiting.
_in_flight: Dict[str, "Future[Dict[str, Any]]"] = {}
_in_flight_lock = threading.Lock()


def _forget(feed_key: str, done: "Future[Dict[str, Any]]") -> None:
    with _in_flight_lock:
        if _in_flight.get(feed_key) is done:
            del _in_flight[feed_key]


def _settled(value: Any = None, error: Optional[Exception] = None) -> "Future[Any]":
    done: "Future[Any]" = Future()
    if error is not None:
        done.set_exception(error)
    else:
        done.set_result(value)
    return done


def _fetch_once(feed_key: str) -> "Future[Dict[str, Any]]":
    with _in_flight_lock:
        future = _in_flight.get(feed_key)
        # A finished future whose cleanup callback has not run yet is not
        # in flight; the cache says what it produced.
        if future is not None and not future.done():
            return future
        # Another request may have finished with this feed between the
        # caller's cache check and here; the registry alone would not know.
        fresh = _recall(_fresh_key(feed_key))
        if fresh is not None:
            return _settled(fresh)
        if _recall(_failed_key(feed_key)):
            return _settled(error=RuntimeError("left alone after a recent failure"))
        future = health_news_executor.submit(_fetch_and_remember, feed_key)
        _in_flight[feed_key] = future
    # Registered outside the lock: a future that has already finished runs
    # its callback right here, in this thread, and _forget needs the lock.
    future.add_done_callback(functools.partial(_forget, feed_key))
    return future


def _recall(key: str) -> Any:
    """A cache read that cannot cost the page the feeds it has."""
    try:
        return cache.get(key)
    except Exception as e:
        logger.warning(f"health_news: could not read {key} from the cache: {e!r}")
        return None


def _last_good(feed_key: str) -> Optional[Dict[str, Any]]:
    kept = _recall(_last_good_key(feed_key))
    return kept if isinstance(kept, dict) else None


def get_health_news() -> Dict[str, Dict[str, Any]]:
    """Every feed that has something to show, in FEEDS order.

    From the cache where it can be; fetched together where it cannot. A feed
    with neither a fresh copy, a last good copy, nor a successful fetch is
    left out, which is what the template already expects.
    """
    found: Dict[str, Dict[str, Any]] = {}
    to_fetch: List[str] = []
    for feed_key in FEEDS:
        fresh = _recall(_fresh_key(feed_key))
        if fresh is not None:
            found[feed_key] = fresh
        elif _recall(_failed_key(feed_key)):
            last_good = _last_good(feed_key)
            if last_good is not None:
                found[feed_key] = last_good
        else:
            to_fetch.append(feed_key)

    if to_fetch:
        deadline = time.monotonic() + TOTAL_BUDGET_SECONDS
        futures = {feed_key: _fetch_once(feed_key) for feed_key in to_fetch}
        for feed_key, future in futures.items():
            try:
                found[feed_key] = future.result(
                    timeout=max(0.0, deadline - time.monotonic())
                )
            except Exception as e:
                logger.warning(
                    f"health_news: {FEEDS[feed_key]['url']} not fetched: {e!r}"
                )
                # Leave it alone for a while; a KFF outage should cost one
                # bounded wait per process, not one per visitor.
                _remember(_failed_key(feed_key), True, RETRY_AFTER_SECONDS)
                last_good = _last_good(feed_key)
                if last_good is not None:
                    found[feed_key] = last_good

    return {feed_key: found[feed_key] for feed_key in FEEDS if feed_key in found}

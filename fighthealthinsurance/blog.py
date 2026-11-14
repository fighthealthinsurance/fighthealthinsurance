"""Blog post metadata helpers: dates, BlogPosting JSON-LD and the RSS feed.

Post metadata is the front matter list from ``agent_docs._blog_posts`` (the
generated blog_posts.json, or the .md sources when it is absent).
"""

import re
from datetime import date, datetime, timezone
from typing import Any, Optional

from django.contrib.syndication.views import Feed
from django.urls import reverse

CANONICAL_ORIGIN = "https://www.fighthealthinsurance.com"
ORGANIZATION_ID = f"{CANONICAL_ORIGIN}/#organization"
SHARE_IMAGE_URL = f"{CANONICAL_ORIGIN}/static/images/og-share.jpg"

_MARKDOWN_LINK = re.compile(r"^\[([^\]]+)\]\((https?://[^)\s]+)\)")


def post_date(post: dict[str, Any], key: str = "date") -> Optional[date]:
    """The post's ``date`` (or ``updated``) front matter as a date, if valid."""
    value = post.get(key)
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def last_changed(post: dict[str, Any]) -> Optional[date]:
    """``updated`` when the post has one, else its publish date."""
    return post_date(post, "updated") or post_date(post)


def post_url(slug: str) -> str:
    return CANONICAL_ORIGIN + reverse("blog-post", kwargs={"slug": slug})


def _author(raw: Any) -> dict[str, Any]:
    """A Person node from front matter like "Name, Role" or "[Name](url) | Role".

    Team bylines credit the organization itself.
    """
    text = str(raw or "").strip()
    if not text or text.endswith("Team"):
        return {"@id": ORGANIZATION_ID}
    person: dict[str, Any] = {"@type": "Person"}
    link = _MARKDOWN_LINK.match(text)
    if link:
        person["name"] = link.group(1).strip()
        person["url"] = link.group(2)
    else:
        person["name"] = re.split(r"[|,]", text, maxsplit=1)[0].strip()
    return person


def blog_posting_json_ld(post: dict[str, Any], slug: str) -> dict[str, Any]:
    url = post_url(slug)
    node: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": post.get("title") or slug,
        "url": url,
        "mainEntityOfPage": {"@type": "WebPage", "@id": url},
        "image": SHARE_IMAGE_URL,
        "author": _author(post.get("author")),
        "publisher": {"@id": ORGANIZATION_ID},
        "inLanguage": "en",
    }
    description = post.get("description") or post.get("excerpt")
    if description:
        node["description"] = description
    published = post_date(post)
    if published:
        node["datePublished"] = published.isoformat()
    # Only claim a modified date the post actually states.
    updated = post_date(post, "updated")
    if updated:
        node["dateModified"] = updated.isoformat()
    return node


class BlogFeed(Feed):
    """RSS for /blog/, newest first, from the same post list as the pages."""

    title = "Fight Health Insurance blog"
    description = "Insights and strategies for fighting health insurance denials."

    def link(self) -> str:
        return CANONICAL_ORIGIN + reverse("blog")

    def items(self) -> list[dict[str, Any]]:
        # Imported here: agent_docs imports the sitemap, which imports this module.
        from fighthealthinsurance.agent_docs import _blog_posts

        posts = list(_blog_posts())
        posts.sort(key=lambda p: str(p.get("date", "")), reverse=True)
        return posts

    def item_title(self, item: dict[str, Any]) -> str:
        return str(item.get("title") or item["slug"])

    def item_description(self, item: dict[str, Any]) -> str:
        return str(item.get("description") or item.get("excerpt") or "")

    def item_link(self, item: dict[str, Any]) -> str:
        return post_url(item["slug"])

    def item_pubdate(self, item: dict[str, Any]) -> Optional[datetime]:
        published = post_date(item)
        if published is None:
            return None
        return datetime(
            published.year, published.month, published.day, tzinfo=timezone.utc
        )

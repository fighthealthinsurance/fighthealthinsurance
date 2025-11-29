"""
Sitemap generation for Fight Health Insurance.

This module provides sitemap classes for generating XML sitemaps that help
search engines discover and index the site's public pages.

REST API URLs (under /ziggy/rest/) are excluded from the sitemap as they
are not intended for direct user access.
"""

import json
from typing import Any

from django.contrib.sitemaps import Sitemap
from django.contrib.staticfiles.storage import staticfiles_storage
from django.urls import reverse
from loguru import logger


class StaticViewSitemap(Sitemap):
    """Sitemap for static pages that don't change frequently."""

    priority = 0.5
    changefreq = "weekly"

    def items(self) -> list[str]:
        """Return list of URL names for static pages."""
        return [
            "root",
            "about",
            "pbs-newshour",
            "other-resources",
            "faq",
            "medicaid-faq",
            "tos",
            "privacy_policy",
            "mhmda",
            "contact",
            "blog",
        ]

    def location(self, item: str) -> str:
        """Return the URL for a given item."""
        return reverse(item)


class BlogSitemap(Sitemap):
    """Sitemap for blog posts."""

    priority = 0.6
    changefreq = "monthly"

    def items(self) -> list[dict[str, Any]]:
        """Return list of blog post slugs from blog_posts.json."""
        try:
            with staticfiles_storage.open("blog_posts.json", "r") as f:
                contents = f.read()
                if not isinstance(contents, str):
                    contents = contents.decode("utf-8")
                posts = json.loads(contents)
            return posts if isinstance(posts, list) else []
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load blog_posts.json for sitemap: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error loading blog posts for sitemap: {e}")
            return []

    def location(self, item: dict[str, Any]) -> str:
        """Return the URL for a blog post."""
        slug = item.get("slug", "")
        return reverse("blog-post", kwargs={"slug": slug})


# Dictionary mapping sitemap section names to sitemap classes
sitemaps = {
    "static": StaticViewSitemap,
    "blog": BlogSitemap,
}

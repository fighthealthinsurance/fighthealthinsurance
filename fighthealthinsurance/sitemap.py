"""
Sitemap generation for Fight Health Insurance.

This module provides sitemap classes for generating XML sitemaps that help
search engines discover and index the site's public pages.

REST API URLs (under /ziggy/rest/) are excluded from the sitemap as they
are not intended for direct user access.

The sitemap uses the request's host header to determine the domain, so it
will correctly serve localhost for local development and the production
domain (www.fighthealthinsurance.com) in production.
"""

import json
from typing import Any

from django.contrib.sitemaps import Sitemap
from django.contrib.sites.requests import RequestSite
from django.contrib.staticfiles.storage import staticfiles_storage
from django.http import HttpRequest, HttpResponse
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
            "preparing-2026",
            "other-resources",
            "faq",
            "medicaid-faq",
            "denial-language-library",
            "tos",
            "privacy_policy",
            "mhmda",
            "contact",
            "blog",
            "microsite_directory",
        ]

    def location(self, item: str) -> str:
        """Return the URL for a given item."""
        return reverse(item)


class MicrositeSitemap(Sitemap):
    """Sitemap for microsite landing pages."""

    priority = 0.7
    changefreq = "monthly"

    def items(self) -> list[str]:
        """Return list of microsite slugs from microsites.json, excluding WIP microsites."""
        try:
            from fighthealthinsurance.microsites import get_all_microsites

            # Get all microsites and filter out those marked as WIP
            microsites = get_all_microsites()

            # Only include microsites where wip is False (default) or not set
            live_slugs = [
                slug
                for slug, microsite in microsites.items()
                if not getattr(microsite, "wip", False)
            ]

            return live_slugs
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load microsites.json for sitemap: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error loading microsites for sitemap: {e}")
            return []

    def location(self, item: str) -> str:
        """Return the URL for a microsite."""
        return reverse("microsite", kwargs={"slug": item})


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
            if not isinstance(posts, list):
                return []
            # Filter out posts without valid slugs
            return [post for post in posts if post.get("slug")]
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load blog_posts.json for sitemap: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error loading blog posts for sitemap: {e}")
            return []

    def location(self, item: dict[str, Any]) -> str:
        """Return the URL for a blog post."""
        slug = item.get("slug")
        if not slug:
            # This shouldn't happen since items() filters them out,
            # but provide a safe fallback
            logger.warning(f"Blog post item missing slug: {item}")
            return reverse("blog")
        return reverse("blog-post", kwargs={"slug": slug})


class StateHelpSitemap(Sitemap):
    """Sitemap for state help pages."""

    priority = 0.6
    changefreq = "monthly"

    def items(self) -> list[str]:
        """Return list of validated state help slugs."""
        try:
            # Use load_state_help() to get only validated entries
            # This avoids 404s from invalid entries like "national" that lack required fields
            from fighthealthinsurance.state_help import load_state_help

            valid_states = load_state_help()
            # Add the index page slug first
            return ["index"] + list(valid_states.keys())
        except Exception as e:
            logger.warning(f"Could not load state help for sitemap: {e}")
            return []

    def location(self, item: str) -> str:
        """Return the URL for a state help page."""
        if item == "index":
            return reverse("state_help_index")
        return reverse("state_help", kwargs={"slug": item})


# Dictionary mapping sitemap section names to sitemap classes
sitemaps = {
    "static": StaticViewSitemap,
    "blog": BlogSitemap,
    "microsites": MicrositeSitemap,
    "state_help": StateHelpSitemap,
}


def sitemap_view(
    request: HttpRequest,
    sitemaps: dict[str, type[Sitemap]] = sitemaps,
    template_name: str = "sitemap.xml",
    content_type: str = "application/xml",
) -> HttpResponse:
    """
    Custom sitemap view that uses the request's host header for the domain.

    This ensures the sitemap URLs match the serving domain (localhost for
    local development, www.fighthealthinsurance.com in production, etc.)
    instead of using the hardcoded SITE_ID from django.contrib.sites.

    The xmlns attribute (http://www.sitemaps.org/schemas/sitemap/0.9) is
    automatically included by Django's sitemap.xml template.
    """
    from django.core.paginator import EmptyPage, PageNotAnInteger
    from django.http import Http404
    from django.template.response import TemplateResponse

    # Use RequestSite to get the domain from the request's host header
    req_site = RequestSite(request)
    req_protocol = request.scheme

    maps = sitemaps.values()
    page = request.GET.get("p", 1)

    urls = []
    for site_class in maps:
        try:
            site_instance = site_class()
            urls.extend(
                site_instance.get_urls(page=page, site=req_site, protocol=req_protocol)
            )
        except EmptyPage:
            raise Http404("Page %s empty" % page)
        except PageNotAnInteger:
            raise Http404("No page '%s'" % page)

    return TemplateResponse(
        request,
        template_name,
        {"urlset": urls},
        content_type=content_type,
    )

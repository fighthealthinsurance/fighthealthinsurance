"""
Microsite definitions for Fight Health Insurance.

This module loads microsite configurations from the static microsites.json file
and provides helper functions for accessing microsite data.

Microsites are cached in memory after first load for performance.
"""

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Union

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage

from loguru import logger


class MicrositeValidationError(ValueError):
    """Raised when a microsite configuration is invalid."""

    pass


# Required keys that must be present in every microsite
REQUIRED_MICROSITE_KEYS = {
    "slug",
    "title",
    "default_procedure",
    "tagline",
    "hero_h1",
    "hero_subhead",
    "intro",
    "how_we_help",
    "cta",
}


class Microsite:
    """Represents a microsite configuration."""

    def __init__(self, data: dict[str, Any]):
        # Validate required keys are present
        missing_keys = REQUIRED_MICROSITE_KEYS - set(data.keys())
        if missing_keys:
            raise MicrositeValidationError(
                f"Microsite '{data.get('slug', '<unknown>')}' missing required keys: {missing_keys}"
            )

        # Required fields - will raise KeyError if missing (shouldn't happen after validation)
        self.slug: str = data["slug"]
        self.title: str = data["title"]
        self.default_procedure: str = data["default_procedure"]
        self.tagline: str = data["tagline"]
        self.hero_h1: str = data["hero_h1"]
        self.hero_subhead: str = data["hero_subhead"]
        self.intro: str = data["intro"]
        self.how_we_help: str = data["how_we_help"]
        self.cta: str = data["cta"]

        # Optional condition (e.g., "Asthma", "Migraine", "Gender Dysphoria")
        # Some microsites are condition-focused, others are procedure-focused,
        # and some have both (FFS for Gender Dysphoria)
        self.default_condition: Optional[str] = data.get("default_condition")

        # Optional lists with sensible defaults
        self.common_denial_reasons: list[str] = data.get("common_denial_reasons", [])
        self.faq: list[dict[str, str]] = data.get("faq", [])
        self.evidence_snippets: list[str] = data.get("evidence_snippets", [])
        self.pubmed_search_terms: list[str] = data.get("pubmed_search_terms", [])

        # Optional image URL for displaying medicine/procedure images
        self.image: Optional[str] = data.get("image")

        # Optional alternatives section for drugs where patients might consider
        # other options while fighting insurance denials
        self.alternatives: list[str] = data.get("alternatives", [])

        # Optional assistance programs (patient assistance, copay cards, etc.)
        # Each entry should have: name, url, description
        self.assistance_programs: list[dict[str, str]] = data.get(
            "assistance_programs", []
        )

        # Optional Medicare flag to indicate Medicare-specific content
        self.medicare: bool = data.get("medicare", False)

        # Optional blog post URL to link to related blog content
        self.blog_post_url: Optional[str] = data.get("blog_post_url")

        # Optional manufacturer name (for drug/device microsites)
        self.manufacturer: Optional[str] = data.get("manufacturer")

        # Optional advocacy resources (patient advocacy groups, support organizations)
        # Each entry should have: name, url, description
        self.advocacy_resources: list[dict[str, str]] = data.get(
            "advocacy_resources", []
        )

        # Optional extralinks (external documents/PDFs/guidelines)
        # Each entry should have: url (required), title, description, category, priority (int: 0=highest)
        self.extralinks: list[dict] = data.get("extralinks", [])

        # Validate extralinks structure
        for link in self.extralinks:
            if not isinstance(link, dict):
                raise MicrositeValidationError(
                    f"Each extralink must be a dict, got {type(link)}"
                )
            if "url" not in link:
                raise MicrositeValidationError(
                    f"Extralink missing required 'url' field: {link}"
                )
            # Validate priority if present (should be integer)
            if "priority" in link and not isinstance(link["priority"], int):
                raise MicrositeValidationError(
                    f"Extralink priority must be an integer (0=highest), got {type(link['priority'])}: {link}"
                )

        # Optional WIP (work-in-progress) flag
        # If true, the microsite is excluded from the sitemap but still accessible
        # This allows working on and iterating on microsites before they go live
        self.wip: bool = data.get("wip", False)

    def __repr__(self) -> str:
        return f"<Microsite: {self.slug}>"

    async def get_extralink_context(
        self,
        max_docs: int = 5,
        max_chars_per_doc: int = 2000,
    ) -> str:
        """
        Get formatted context from extralink documents for this microsite.

        Args:
            max_docs: Maximum number of documents to include
            max_chars_per_doc: Maximum characters per document

        Returns:
            Formatted markdown context string, or empty string if unavailable.
        """
        from fighthealthinsurance.extralink_context_helper import (
            ExtraLinkContextHelper,
        )

        return await ExtraLinkContextHelper.fetch_extralink_context_for_microsite(
            self.slug,
            max_docs=max_docs,
            max_chars_per_doc=max_chars_per_doc,
        )

    async def get_pubmed_context(
        self,
        pubmed_tools,
        max_terms: int = 3,
        max_articles_per_term: int = 5,
        max_total_articles: int = 20,
        since: str = "2020",
        return_count: bool = False,
    ) -> Union[str, tuple[str, int]]:
        """
        Get formatted PubMed search results context for this microsite.

        Args:
            pubmed_tools: PubMed tools instance for searching
            max_terms: Maximum number of search terms to use
            max_articles_per_term: Maximum articles per search term
            max_total_articles: Maximum total articles to include
            since: Only include articles published since this year
            return_count: If True, return tuple of (context, article_count)

        Returns:
            Formatted context string with PubMed IDs, or empty string if no results.
            If return_count is True, returns tuple of (context, article_count).
        """
        if not self.pubmed_search_terms:
            return ("", 0) if return_count else ""

        try:
            all_articles = []
            # Trigger PubMed searches for each search term
            for search_term in self.pubmed_search_terms[:max_terms]:
                try:
                    articles = await pubmed_tools.find_pubmed_article_ids_for_query(
                        search_term, since=since
                    )
                    if articles:
                        logger.debug(
                            f"Found {len(articles)} articles for search term: {search_term}"
                        )
                        all_articles.extend(articles[:max_articles_per_term])
                except Exception as e:
                    logger.warning(
                        f"Error searching PubMed for '{search_term}': {e}"
                    )

            if all_articles:
                # Build context string from articles
                context_parts = [
                    f"PubMed search results for {self.default_procedure}:"
                ]
                limited_articles = all_articles[:max_total_articles]
                for pmid in limited_articles:
                    context_parts.append(f"- PMID: {pmid}")

                context = "\n".join(context_parts)
                return (context, len(limited_articles)) if return_count else context

            return ("", 0) if return_count else ""

        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error getting PubMed context for microsite {self.slug}: {e}"
            )
            return ("", 0) if return_count else ""

    async def get_combined_context(
        self,
        pubmed_tools=None,
        max_extralink_docs: int = 5,
        max_extralink_chars: int = 2000,
        max_pubmed_terms: int = 3,
        max_pubmed_articles: int = 20,
    ) -> str:
        """
        Get combined extralink and PubMed context for this microsite.

        Args:
            pubmed_tools: Optional PubMed tools instance. If None, only extralinks are fetched.
            max_extralink_docs: Maximum extralink documents
            max_extralink_chars: Maximum chars per extralink document
            max_pubmed_terms: Maximum PubMed search terms
            max_pubmed_articles: Maximum total PubMed articles

        Returns:
            Combined formatted context string.
        """
        contexts = []

        # Fetch extralinks if available
        extralink_context = await self.get_extralink_context(
            max_docs=max_extralink_docs,
            max_chars_per_doc=max_extralink_chars,
        )
        if extralink_context:
            contexts.append(extralink_context)

        # Fetch PubMed if available and tools provided
        if pubmed_tools and self.pubmed_search_terms:
            pubmed_context = await self.get_pubmed_context(
                pubmed_tools,
                max_terms=max_pubmed_terms,
                max_total_articles=max_pubmed_articles,
            )
            if pubmed_context:
                contexts.append(pubmed_context)

        return "\n\n".join(contexts) if contexts else ""


@lru_cache(maxsize=1)
def _find_microsites_json() -> Optional[str]:
    """
    Find microsites.json in staticfiles or STATICFILES_DIRS.

    Returns the file contents as a string, or None if not found.
    """
    # First try staticfiles_storage (works when collectstatic has been run)
    try:
        with staticfiles_storage.open("microsites.json", "r") as f:
            contents = f.read()
            if not isinstance(contents, str):
                contents = contents.decode("utf-8")
            return str(contents)
    except Exception as e:
        logger.debug(f"Could not open microsites.json via staticfiles_storage: {e}")

    # Fallback: search STATICFILES_DIRS directly (for test environments)
    staticfiles_dirs = getattr(settings, "STATICFILES_DIRS", [])
    for static_dir in staticfiles_dirs:
        json_path = Path(static_dir) / "microsites.json"
        if json_path.exists():
            logger.debug(f"Found microsites.json in STATICFILES_DIRS: {json_path}")
            with open(json_path, "r") as f:
                return f.read()

    # Final fallback: check STATIC_ROOT
    static_root = getattr(settings, "STATIC_ROOT", None)
    if static_root:
        json_path = Path(static_root) / "microsites.json"
        if json_path.exists():
            logger.debug(f"Found microsites.json in STATIC_ROOT: {json_path}")
            with open(json_path, "r") as f:
                return f.read()

    return None


def _load_microsites_cached() -> tuple[tuple[str, Microsite], ...]:
    """
    Load microsite definitions from the static microsites.json file.

    Returns a tuple of tuples for hashability (required by lru_cache).
    Use load_microsites() to get a dict instead.

    Never raises exceptions to avoid breaking startup - logs errors and returns
    empty tuple on failure.
    """
    try:
        contents = _find_microsites_json()
        if contents is None:
            logger.warning(
                "microsites.json not found in static files or STATICFILES_DIRS"
            )
            return ()

        data = json.loads(contents)

        # Validate that parsed JSON is a dict
        if not isinstance(data, dict):
            logger.error(
                f"microsites.json has invalid shape: expected dict, got {type(data).__name__}"
            )
            return ()

        microsites = []
        for slug, microsite_data in data.items():
            # Validate each microsite entry is a dict
            if not isinstance(microsite_data, dict):
                logger.error(
                    f"Microsite '{slug}' has invalid shape: expected dict, got {type(microsite_data).__name__}"
                )
                continue
            try:
                microsites.append((slug, Microsite(microsite_data)))
            except MicrositeValidationError:
                logger.exception(f"Validation error for microsite '{slug}'")
                continue

        return tuple(microsites)
    except json.JSONDecodeError:
        logger.exception("Error parsing microsites.json")
        return ()
    except Exception:
        logger.exception("Unexpected error loading microsites")
        return ()


def load_microsites() -> dict[str, Microsite]:
    """
    Load microsite definitions from the static microsites.json file.

    Results are cached in memory after first load.

    Returns:
        Dictionary mapping slug to Microsite object
    """
    return dict(_load_microsites_cached())


def get_microsite(slug: str) -> Optional[Microsite]:
    """
    Get a microsite by its slug.

    Args:
        slug: The URL slug of the microsite

    Returns:
        Microsite object if found, None otherwise
    """
    microsites = load_microsites()
    result = microsites.get(slug)
    if result is not None:
        return result
    result = microsites.get(f"{slug}-denial")
    if result is not None:
        return result
    result = microsites.get(slug.replace("-denial", ""))
    if result is not None:
        return result
    result = microsites.get(slug.replace("-denial", "") + "-denial")
    if result is not None:
        return result
    return None


def get_all_microsites() -> dict[str, Microsite]:
    """
    Get all available microsites.

    Returns:
        Dictionary mapping slug to Microsite object
    """
    return load_microsites()


def get_microsite_slugs() -> list[str]:
    """
    Get a list of all microsite slugs.

    Returns:
        List of slug strings
    """
    return list(load_microsites().keys())


def get_microsites_sorted_by_title() -> list[Microsite]:
    """
    Get all microsites sorted alphabetically by title.

    Returns:
        List of Microsite objects sorted by title
    """
    return sorted(load_microsites().values(), key=lambda m: m.title)

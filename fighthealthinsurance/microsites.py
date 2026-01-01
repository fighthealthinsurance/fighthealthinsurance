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
from typing import Any, Optional

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

    def __repr__(self) -> str:
        return f"<Microsite: {self.slug}>"


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
    else:
        return microsites.get(f"{slug}-denial")


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

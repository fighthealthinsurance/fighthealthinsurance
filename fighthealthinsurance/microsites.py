"""
Microsite definitions for Fight Health Insurance.

This module loads microsite configurations from the static microsites.json file
and provides helper functions for accessing microsite data.

Microsites are cached in memory after first load for performance.
"""

import json
from functools import lru_cache
from typing import Any, Optional

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
        self.google_scholar_search_terms: list[str] = data.get("google_scholar_search_terms", [])

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

    def __repr__(self) -> str:
        return f"<Microsite: {self.slug}>"


@lru_cache(maxsize=1)
def _load_microsites_cached() -> tuple[tuple[str, Microsite], ...]:
    """
    Load microsite definitions from the static microsites.json file.

    Returns a tuple of tuples for hashability (required by lru_cache).
    Use load_microsites() to get a dict instead.

    Never raises exceptions to avoid breaking startup - logs errors and returns
    empty tuple on failure.
    """
    try:
        with staticfiles_storage.open("microsites.json", "r") as f:
            contents = f.read()
            if not isinstance(contents, str):
                contents = contents.decode("utf-8")
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
    except FileNotFoundError:
        logger.warning("microsites.json not found in static files")
        return ()
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
    return microsites.get(slug)


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

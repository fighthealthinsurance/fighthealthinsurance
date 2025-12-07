"""
Microsite definitions for Fight Health Insurance.

This module loads microsite configurations from the static microsites.json file
and provides helper functions for accessing microsite data.
"""

import json
from typing import Any, Optional

from django.contrib.staticfiles.storage import staticfiles_storage
from loguru import logger


class Microsite:
    """Represents a microsite configuration."""

    def __init__(self, data: dict[str, Any]):
        self.slug: str = data.get("slug", "")
        self.title: str = data.get("title", "")
        self.default_procedure: str = data.get("default_procedure", "")
        self.tagline: str = data.get("tagline", "")
        self.hero_h1: str = data.get("hero_h1", "")
        self.hero_subhead: str = data.get("hero_subhead", "")
        self.intro: str = data.get("intro", "")
        self.common_denial_reasons: list[str] = data.get("common_denial_reasons", [])
        self.how_we_help: str = data.get("how_we_help", "")
        self.cta: str = data.get("cta", "")
        self.faq: list[dict[str, str]] = data.get("faq", [])
        self.evidence_snippets: list[str] = data.get("evidence_snippets", [])
        self.pubmed_search_terms: list[str] = data.get("pubmed_search_terms", [])

    def __repr__(self) -> str:
        return f"<Microsite: {self.slug}>"


def load_microsites() -> dict[str, Microsite]:
    """
    Load microsite definitions from the static microsites.json file.

    Returns:
        Dictionary mapping slug to Microsite object
    """
    try:
        with staticfiles_storage.open("microsites.json", "r") as f:
            contents = f.read()
            if not isinstance(contents, str):
                contents = contents.decode("utf-8")
            data = json.loads(contents)

        microsites = {}
        for slug, microsite_data in data.items():
            microsites[slug] = Microsite(microsite_data)

        return microsites
    except FileNotFoundError:
        logger.warning("microsites.json not found in static files")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing microsites.json: {e}")
        return {}
    except Exception as e:
        logger.error(f"Unexpected error loading microsites: {e}")
        return {}


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

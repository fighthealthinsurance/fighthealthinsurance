"""
State Help definitions for Fight Health Insurance.

This module loads state help resource configurations from the static state_help.json file
and provides helper functions for accessing state-by-state assistance data.

State help resources are cached in memory after first load for performance.
"""

import json
from functools import lru_cache
from typing import Any, Optional

from django.contrib.staticfiles.storage import staticfiles_storage
from loguru import logger


class StateHelpValidationError(ValueError):
    """Raised when a state help configuration is invalid."""

    pass


# Required keys that must be present in every state help entry
REQUIRED_STATE_HELP_KEYS = {
    "slug",
    "name",
    "abbreviation",
    "insurance_department",
    "consumer_assistance",
    "medicaid",
}


class InsuranceDepartment:
    """Represents a state insurance department."""

    def __init__(self, data: dict[str, Any]):
        self.name: str = data.get("name", "")
        self.url: str = data.get("url", "")
        self.phone: str = data.get("phone", "")
        self.consumer_line: Optional[str] = data.get("consumer_line")
        self.complaint_url: Optional[str] = data.get("complaint_url")

    def __repr__(self) -> str:
        return f"<InsuranceDepartment: {self.name}>"


class ConsumerAssistance:
    """Represents consumer assistance programs for a state."""

    def __init__(self, data: dict[str, Any]):
        self.cap_name: Optional[str] = data.get("cap_name")
        self.cap_url: Optional[str] = data.get("cap_url")
        self.cap_phone: Optional[str] = data.get("cap_phone")
        self.ship_name: Optional[str] = data.get("ship_name")
        self.ship_url: Optional[str] = data.get("ship_url")
        self.ship_phone: Optional[str] = data.get("ship_phone")

    def __repr__(self) -> str:
        return f"<ConsumerAssistance: {self.cap_name or self.ship_name}>"


class MedicaidOmbudsman:
    """Represents a Medicaid ombudsman program."""

    def __init__(self, data: dict[str, Any]):
        self.name: str = data.get("name", "")
        self.phone: Optional[str] = data.get("phone")
        self.url: Optional[str] = data.get("url")

    def __repr__(self) -> str:
        return f"<MedicaidOmbudsman: {self.name}>"


class MedicaidInfo:
    """Represents Medicaid information for a state."""

    def __init__(self, data: dict[str, Any]):
        self.agency_name: str = data.get("agency_name", "")
        self.agency_url: Optional[str] = data.get("agency_url")
        self.agency_phone: Optional[str] = data.get("agency_phone")
        ombudsman_data = data.get("managed_care_ombudsman")
        self.managed_care_ombudsman: Optional[MedicaidOmbudsman] = (
            MedicaidOmbudsman(ombudsman_data) if ombudsman_data else None
        )

    def __repr__(self) -> str:
        return f"<MedicaidInfo: {self.agency_name}>"


class ExternalReviewInfo:
    """Represents external review information for a state."""

    def __init__(self, data: dict[str, Any]):
        self.available: bool = data.get("available", False)
        self.info_url: Optional[str] = data.get("info_url")

    def __repr__(self) -> str:
        return f"<ExternalReviewInfo: available={self.available}>"


class AdditionalResource:
    """Represents an additional resource for a state."""

    def __init__(self, data: dict[str, Any]):
        self.name: str = data.get("name", "")
        self.url: Optional[str] = data.get("url")
        self.description: Optional[str] = data.get("description")

    def __repr__(self) -> str:
        return f"<AdditionalResource: {self.name}>"


class StateHelp:
    """Represents a state help configuration."""

    def __init__(self, data: dict[str, Any]):
        # Validate required keys are present
        missing_keys = REQUIRED_STATE_HELP_KEYS - set(data.keys())
        if missing_keys:
            raise StateHelpValidationError(
                f"StateHelp '{data.get('slug', '<unknown>')}' missing required keys: {missing_keys}"
            )

        # Required fields
        self.slug: str = data["slug"]
        self.name: str = data["name"]
        self.abbreviation: str = data["abbreviation"]

        # Structured data
        self.insurance_department = InsuranceDepartment(
            data.get("insurance_department", {})
        )
        self.consumer_assistance = ConsumerAssistance(
            data.get("consumer_assistance", {})
        )
        self.medicaid = MedicaidInfo(data.get("medicaid", {}))

        # Optional external review info
        external_review_data = data.get("external_review")
        self.external_review: Optional[ExternalReviewInfo] = (
            ExternalReviewInfo(external_review_data) if external_review_data else None
        )

        # Optional additional resources list
        self.additional_resources: list[AdditionalResource] = [
            AdditionalResource(r) for r in data.get("additional_resources", [])
        ]

    def __repr__(self) -> str:
        return f"<StateHelp: {self.name} ({self.abbreviation})>"


@lru_cache(maxsize=1)
def _load_state_help_cached() -> tuple[tuple[str, StateHelp], ...]:
    """
    Load state help definitions from the static state_help.json file.

    Returns a tuple of tuples for hashability (required by lru_cache).
    Use load_state_help() to get a dict instead.

    Never raises exceptions to avoid breaking startup - logs errors and returns
    empty tuple on failure.
    """
    try:
        with staticfiles_storage.open("state_help.json", "r") as f:
            contents = f.read()
            if not isinstance(contents, str):
                contents = contents.decode("utf-8")
            data = json.loads(contents)

        # Validate that parsed JSON is a dict
        if not isinstance(data, dict):
            logger.error(
                f"state_help.json has invalid shape: expected dict, got {type(data).__name__}"
            )
            return ()

        state_help_list = []
        for slug, state_help_data in data.items():
            # Validate each state help entry is a dict
            if not isinstance(state_help_data, dict):
                logger.error(
                    f"StateHelp '{slug}' has invalid shape: expected dict, got {type(state_help_data).__name__}"
                )
                continue
            try:
                state_help_list.append((slug, StateHelp(state_help_data)))
            except StateHelpValidationError:
                logger.exception(f"Validation error for state help '{slug}'")
                continue

        return tuple(state_help_list)
    except FileNotFoundError:
        logger.warning("state_help.json not found in static files")
        return ()
    except json.JSONDecodeError:
        logger.exception("Error parsing state_help.json")
        return ()
    except Exception:
        logger.exception("Unexpected error loading state help")
        return ()


def load_state_help() -> dict[str, StateHelp]:
    """
    Load state help definitions from the static state_help.json file.

    Results are cached in memory after first load.

    Returns:
        Dictionary mapping slug to StateHelp object
    """
    return dict(_load_state_help_cached())


def get_state_help(slug: str) -> Optional[StateHelp]:
    """
    Get a state help entry by its slug.

    Args:
        slug: The URL slug of the state (e.g., "california", "new-york")

    Returns:
        StateHelp object if found, None otherwise
    """
    state_help = load_state_help()
    return state_help.get(slug)


def get_state_help_by_abbreviation(abbreviation: str) -> Optional[StateHelp]:
    """
    Get a state help entry by its abbreviation.

    Args:
        abbreviation: The two-letter state abbreviation (e.g., "CA", "NY")

    Returns:
        StateHelp object if found, None otherwise
    """
    abbreviation_upper = abbreviation.upper()
    for state in load_state_help().values():
        if state.abbreviation == abbreviation_upper:
            return state
    return None


def get_all_state_help() -> dict[str, StateHelp]:
    """
    Get all available state help entries.

    Returns:
        Dictionary mapping slug to StateHelp object
    """
    return load_state_help()


def get_state_help_slugs() -> list[str]:
    """
    Get a list of all state help slugs.

    Returns:
        List of slug strings
    """
    return list(load_state_help().keys())


def get_states_sorted_by_name() -> list[StateHelp]:
    """
    Get all states sorted by name.

    Returns:
        List of StateHelp objects sorted alphabetically by state name
    """
    return sorted(load_state_help().values(), key=lambda s: s.name)

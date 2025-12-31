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
        """
        Initialize an InsuranceDepartment from a dictionary of raw data.

        Parameters:
            data (dict[str, Any]): Mapping containing insurance department fields. Recognized keys:
                - "name": department name (defaults to an empty string).
                - "url": department URL (defaults to an empty string).
                - "phone": department phone number (defaults to an empty string).
                - "consumer_line": consumer assistance phone number (optional).
                - "complaint_url": URL for filing complaints (optional).
        """
        self.name: str = data.get("name", "")
        self.url: str = data.get("url", "")
        self.phone: str = data.get("phone", "")
        self.consumer_line: Optional[str] = data.get("consumer_line")
        self.complaint_url: Optional[str] = data.get("complaint_url")

    def __repr__(self) -> str:
        """
        Return a concise developer-facing identifier for the InsuranceDepartment instance.

        Returns:
            A string of the form "<InsuranceDepartment: {name}>" where {name} is the department's name.
        """
        return f"<InsuranceDepartment: {self.name}>"


class ConsumerAssistance:
    """Represents consumer assistance programs for a state."""

    def __init__(self, data: dict[str, Any]):
        """
        Initialize a ConsumerAssistance from a dictionary of state consumer assistance fields.

        Parameters:
            data (dict[str, Any]): Mapping containing optional consumer assistance keys:
                - "cap_name": name of the Consumer Assistance Program (CAP)
                - "cap_url": URL for the CAP
                - "cap_phone": phone number for the CAP
                - "ship_name": name of the State Health Insurance Assistance Program (SHIP)
                - "ship_url": URL for the SHIP
                - "ship_phone": phone number for the SHIP

        The constructor sets corresponding attributes to the values found in `data` or `None` if a key is absent.
        """
        self.cap_name: Optional[str] = data.get("cap_name")
        self.cap_url: Optional[str] = data.get("cap_url")
        self.cap_phone: Optional[str] = data.get("cap_phone")
        self.ship_name: Optional[str] = data.get("ship_name")
        self.ship_url: Optional[str] = data.get("ship_url")
        self.ship_phone: Optional[str] = data.get("ship_phone")

    def __repr__(self) -> str:
        """
        Return a concise identifier for the ConsumerAssistance instance.

        Returns:
            str: A string of the form "<ConsumerAssistance: NAME>" where NAME is `cap_name` if present, otherwise `ship_name`.
        """
        return f"<ConsumerAssistance: {self.cap_name or self.ship_name}>"


class MedicaidOmbudsman:
    """Represents a Medicaid ombudsman program."""

    def __init__(self, data: dict[str, Any]):
        """
        Create a MedicaidOmbudsman from a raw mapping of values.

        Parameters:
            data (dict[str, Any]): Source mapping. Expected keys:
                - "name": ombudsman name (defaults to empty string if missing)
                - "phone": contact phone number (optional)
                - "url": information URL (optional)
        """
        self.name: str = data.get("name", "")
        self.phone: Optional[str] = data.get("phone")
        self.url: Optional[str] = data.get("url")

    def __repr__(self) -> str:
        """
        Provide a concise developer-facing representation of the MedicaidOmbudsman.

        Returns:
            str: A string formatted as "<MedicaidOmbudsman: {name}>" where {name} is the ombudsman's name.
        """
        return f"<MedicaidOmbudsman: {self.name}>"


class MedicaidInfo:
    """Represents Medicaid information for a state."""

    def __init__(self, data: dict[str, Any]):
        """
        Initialize a MedicaidInfo object from a dictionary of Medicaid-related fields.

        Parameters:
            data (dict[str, Any]): Mapping containing Medicaid info. Recognized keys:
                - "agency_name": display name of the Medicaid agency; defaults to "" if missing.
                - "agency_url": agency website URL; optional, stored as None if absent.
                - "agency_phone": agency phone number; optional, stored as None if absent.
                - "managed_care_ombudsman": optional mapping used to construct a `MedicaidOmbudsman` instance; stored as `None` if not provided.
        """
        self.agency_name: str = data.get("agency_name", "")
        self.agency_url: Optional[str] = data.get("agency_url")
        self.agency_phone: Optional[str] = data.get("agency_phone")
        ombudsman_data = data.get("managed_care_ombudsman")
        self.managed_care_ombudsman: Optional[MedicaidOmbudsman] = (
            MedicaidOmbudsman(ombudsman_data) if ombudsman_data else None
        )

    def __repr__(self) -> str:
        """
        Provide a concise, developer-focused string identifying the MedicaidInfo instance.

        Returns:
            str: A representation in the form "<MedicaidInfo: {agency_name}>", where `{agency_name}` is the Medicaid agency's name.
        """
        return f"<MedicaidInfo: {self.agency_name}>"


class ExternalReviewInfo:
    """Represents external review information for a state."""

    def __init__(self, data: dict[str, Any]):
        """
        Initialize an ExternalReviewInfo from a parsed state-help mapping.

        Parameters:
            data (dict[str, Any]): Source mapping, expected to contain optional keys:
                - "available" (bool): whether external review is available.
                - "info_url" (str): URL with details about external review.
        """
        self.available: bool = data.get("available", False)
        self.info_url: Optional[str] = data.get("info_url")

    def __repr__(self) -> str:
        """
        Return a concise representation of the ExternalReviewInfo instance indicating availability.

        Returns:
            A string of the form "<ExternalReviewInfo: available=True>" or "<ExternalReviewInfo: available=False>".
        """
        return f"<ExternalReviewInfo: available={self.available}>"


class AdditionalResource:
    """Represents an additional resource for a state."""

    def __init__(self, data: dict[str, Any]):
        """
        Initialize an AdditionalResource from a raw mapping.

        Parameters:
            data (dict[str, Any]): Source mapping with keys:
                - "name" (str): Resource name (required; falls back to empty string if missing).
                - "url" (str, optional): Resource URL.
                - "description" (str, optional): Short description of the resource.
        """
        self.name: str = data.get("name", "")
        self.url: Optional[str] = data.get("url")
        self.description: Optional[str] = data.get("description")

    def __repr__(self) -> str:
        """
        Provide a concise developer-facing string identifying the AdditionalResource.

        Returns:
            str: Angle-bracketed identifier that includes the resource name (e.g., "<AdditionalResource: Resource Name>").
        """
        return f"<AdditionalResource: {self.name}>"


class StateHelp:
    """Represents a state help configuration."""

    def __init__(self, data: dict[str, Any]):
        # Validate required keys are present
        """
        Create a StateHelp object from a raw dictionary representation and validate required keys.

        Parameters:
            data (dict[str, Any]): Parsed JSON mapping for a single state's help configuration. Must include the keys: "slug", "name", "abbreviation", "insurance_department", "consumer_assistance", and "medicaid". Optional keys: "external_review", "additional_resources".

        Description:
            On success, sets the following attributes on the instance:
            - slug, name, abbreviation
            - insurance_department (InsuranceDepartment)
            - consumer_assistance (ConsumerAssistance)
            - medicaid (MedicaidInfo)
            - external_review (ExternalReviewInfo or None)
            - additional_resources (list[AdditionalResource])

        Raises:
            StateHelpValidationError: If any required keys are missing from `data`.
        """
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
        """
        Return a concise, human-readable identifier for the StateHelp instance.

        Returns:
            str: A short identifying string containing the state's name and abbreviation in angle brackets, e.g. "<StateHelp: California (CA)>".
        """
        return f"<StateHelp: {self.name} ({self.abbreviation})>"


@lru_cache(maxsize=1)
def _load_state_help_cached() -> tuple[tuple[str, StateHelp], ...]:
    """
    Load state help definitions from the static "state_help.json" file.

    Returns:
        tuple[tuple[str, StateHelp], ...]: A tuple of `(slug, StateHelp)` pairs parsed from the file.
            Returns an empty tuple if the file is missing, malformed, or entries fail validation.
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
            # Skip "national" entry - it has a different structure for national resources
            if slug == "national":
                continue
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
    Retrieve a state's help configuration by its slug.

    Parameters:
        slug (str): State URL slug (e.g., "california", "new-york").

    Returns:
        StateHelp or None: The matching StateHelp if found, `None` otherwise.
    """
    state_help = load_state_help()
    return state_help.get(slug)


def get_state_help_by_abbreviation(abbreviation: str) -> Optional[StateHelp]:
    """
    Finds the StateHelp entry for a U.S. state given its two-letter postal abbreviation.

    Parameters:
        abbreviation (str): Two-letter state abbreviation (case-insensitive), e.g. "CA" or "ny".

    Returns:
        The matching StateHelp if found, `None` otherwise.
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
    List all available state help slugs.

    Returns:
        list[str]: A list of state slug strings.
    """
    return list(load_state_help().keys())


def get_states_sorted_by_name() -> list[StateHelp]:
    """
    Return StateHelp entries sorted alphabetically by state name.

    Returns:
        A list of StateHelp objects sorted alphabetically by their `name` attribute.
    """
    return sorted(load_state_help().values(), key=lambda s: s.name)

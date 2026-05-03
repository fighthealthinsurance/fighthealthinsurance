"""Canonical references for the UCR (Usual & Customary Rate) feature.

Importing from this module is the only correct way for code to reference UCR
sources, area kinds, and the percentile set.
"""

from django.db import models


class UCRSource(models.TextChoices):
    """Recognized UCR data sources, ordered by lookup priority elsewhere."""

    MEDICARE_PFS = "medicare_pfs", "CMS Medicare PFS"
    FAIR_HEALTH = "fair_health", "FAIR Health"
    FHI_AGGREGATE = "fhi_aggregate", "FHI Aggregate"


class UCRAreaKind(models.TextChoices):
    """Geographic area granularities supported by UCRGeographicArea."""

    ZIP3 = "zip3", "ZIP3"
    MSA = "msa", "MSA"
    STATE = "state", "State"
    NATIONAL = "national", "National"
    MEDICARE_LOCALITY = "medicare_locality", "Medicare Locality"


# Percentiles emitted by the loader and surfaced by default in comparisons.
# The schema permits any positive integer; this list is what we standardize on.
UCR_PERCENTILES: tuple[int, ...] = (50, 80, 90)


# Source priority when more than one source matches a query.
# Higher-index source wins (so fhi_aggregate beats fair_health beats medicare_pfs).
# TextChoices members inherit from str, so they satisfy tuple[str, ...] directly
# at runtime and keep this tuple tied to the enum (rename-safe).
UCR_SOURCE_PRIORITY: tuple[str, ...] = (
    UCRSource.MEDICARE_PFS,
    UCRSource.FAIR_HEALTH,
    UCRSource.FHI_AGGREGATE,
)


# Sentinel key inside Denial.ucr_context so callers can avoid stringly-typed access.
UCR_CONTEXT_HASH_KEY = "hash"

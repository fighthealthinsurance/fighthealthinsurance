"""Canonical references for the UCR (Usual & Customary Rate) feature.

See UCR-OON-Reimbursement-Plan.md §3.1 for the source of truth on these values.
Importing from this module is the only correct way for code to reference UCR
sources, area kinds, and the percentile set.
"""

from typing import Mapping

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
# Spelled out as plain strings (rather than UCRSource.X.value) because mypy without
# the django-stubs plugin reads TextChoices members as tuple[str, str] and rejects
# .value. The strings here MUST match the enum values above.
UCR_SOURCE_PRIORITY: tuple[str, ...] = (
    "medicare_pfs",
    "fair_health",
    "fhi_aggregate",
)


# Default Medicare-multiplier proxies used until commercial percentile data lands.
# Override per-environment via settings.UCR_MEDICARE_PERCENTILE_MULTIPLIERS.
UCR_DEFAULT_MEDICARE_MULTIPLIERS: Mapping[int, float] = {50: 1.5, 80: 2.0, 90: 2.5}


# Sentinel keys inside Denial.ucr_context so callers can avoid stringly-typed access.
UCR_CONTEXT_HASH_KEY = "hash"
UCR_CONTEXT_STATUS_KEY = "status"
UCR_CONTEXT_STATUS_PENDING = "pending"
UCR_CONTEXT_STATUS_READY = "ready"

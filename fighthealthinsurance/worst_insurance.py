"""Data helpers for the public worst-insurance-by-state pages.

DB-backed analog of ``state_help.py`` (deliberately no lru_cache: the data
changes on ingest and the URL-layer ``cache_page`` already absorbs load).
Pages stay dark — views 404 and the sitemap stays empty — until a report
has been ingested.
"""

from dataclasses import dataclass
from typing import List, Optional

from fighthealthinsurance.models import WorstInsuranceRanking, WorstInsuranceReport
from fighthealthinsurance.state_help import StateHelp, get_states_sorted_by_name


@dataclass
class StateRankingRow:
    """One state's card on the index page."""

    state_help: StateHelp
    status: str  # "ok" | "insufficient_data"
    reason: str
    worst: Optional[WorstInsuranceRanking]
    worst_est_hours: Optional[int] = None

    @property
    def slug(self) -> str:
        return self.state_help.slug

    @property
    def name(self) -> str:
        return self.state_help.name


def get_latest_report() -> Optional[WorstInsuranceReport]:
    """The most recent ingested report, or None (pages stay unpublished)."""
    return WorstInsuranceReport.objects.order_by("-period").first()


def get_index_rows(report: WorstInsuranceReport) -> List[StateRankingRow]:
    """A row per state (sorted by name): its worst insurer or why not."""
    # No select_related("insurance_company"): the pages render issuer_name
    # directly, and joining pulls InsuranceCompany's RegexFields through
    # their converter, which rejects the NULLs of unmatched rows.
    worst_by_state = {
        ranking.state: ranking for ranking in report.rankings.filter(rank=1)
    }
    statuses = report.state_statuses or {}
    rows = []
    for state in get_states_sorted_by_name():
        worst = worst_by_state.get(state.abbreviation)
        if worst is not None:
            rows.append(
                StateRankingRow(
                    state_help=state,
                    status="ok",
                    reason="",
                    worst=worst,
                    worst_est_hours=estimated_hours(worst),
                )
            )
        else:
            status_block = statuses.get(state.abbreviation, {})
            rows.append(
                StateRankingRow(
                    state_help=state,
                    status="insufficient_data",
                    reason=str(status_block.get("reason", "no_data")),
                    worst=None,
                )
            )
    return rows


def get_state_rankings(
    report: WorstInsuranceReport, abbreviation: str
) -> List[WorstInsuranceRanking]:
    return list(report.rankings.filter(state=abbreviation.upper()).order_by("rank"))


def get_time_burden_assumptions(report: WorstInsuranceReport) -> dict:
    """The cited estimated-hours assumptions from the ingested artifact."""
    raw = report.raw_data or {}
    return raw.get("time_burden_assumptions", {}) or {}


# Display metadata for the ranking-table columns, in column order. The
# estimated-hours metric leads.
METRIC_COLUMNS = [
    (
        "time_burden",
        "Est. hours wasted / year",
        "estimated patient + provider time (see methodology)",
    ),
    ("denial_rate", "Claims denied", "of in-network claims (marketplace plans)"),
    (
        "complaint_rate",
        "Confirmed complaints",
        "per 100k member-months (state regulator)",
    ),
    ("overturn_rate", "Appeals overturned", "of appeal decisions"),
    (
        "enforcement_rate",
        "Enforcement actions",
        "per 1M member-months (state regulator)",
    ),
]


def estimated_hours(ranking: "WorstInsuranceRanking") -> Optional[int]:
    """The plan's estimated hours-wasted-per-year, or None if not computed."""
    metric = (ranking.metrics or {}).get("time_burden")
    if not metric:
        return None
    detail = metric.get("detail") or {}
    hours = detail.get("estimated_hours_per_year", metric.get("value"))
    try:
        return int(hours)
    except (TypeError, ValueError):
        return None


def _format_metric(key: str, metric: dict) -> dict:
    # ``.get(k, 0)`` returns None when the key exists with a null value, so
    # coalesce with ``or 0`` — a metric dict carrying nulls must render a dash,
    # not 500 the page.
    value = metric.get("value") or 0
    numerator = int(metric.get("numerator") or 0)
    denominator = int(metric.get("denominator") or 0)
    if key == "time_burden":
        detail_data = metric.get("detail") or {}
        won = int(detail_data.get("won_appeal_hours") or 0)
        est = int(detail_data.get("unappealed_denial_hours") or 0)
        display = f"~{int(value):,}"
        if est:
            detail = f"{won:,} from overturned appeals + ~{est:,} estimated"
        else:
            detail = f"{won:,} from overturned appeals"
    elif key in ("denial_rate", "overturn_rate"):
        display = f"{value * 100:.1f}%"
        detail = f"{numerator:,} of {denominator:,}"
    elif key == "complaint_rate":
        display = f"{value:.1f}"
        detail = f"{numerator:,} confirmed complaints"
    else:  # enforcement_rate
        display = f"{value:.1f}"
        penalty = (metric.get("detail") or {}).get("penalty_dollars")
        detail = f"{numerator:,} actions"
        if penalty:
            detail += f", ${penalty:,.0f} in penalties"
    return {"display": display, "detail": detail}


@dataclass
class RankingTable:
    """Pre-formatted ranking table for a state page."""

    columns: List[dict]
    rows: List[dict]


def build_ranking_table(rankings: List[WorstInsuranceRanking]) -> RankingTable:
    present: set[str] = set()
    for ranking in rankings:
        present.update((ranking.metrics or {}).keys())
    columns = [
        {"key": key, "label": label, "hint": hint}
        for key, label, hint in METRIC_COLUMNS
        if key in present
    ]
    rows = []
    for ranking in rankings:
        metrics = ranking.metrics or {}
        cells = [
            (
                _format_metric(col["key"], metrics[col["key"]])
                if col["key"] in metrics
                else None
            )
            for col in columns
        ]
        rows.append({"ranking": ranking, "cells": cells})
    return RankingTable(columns=columns, rows=rows)

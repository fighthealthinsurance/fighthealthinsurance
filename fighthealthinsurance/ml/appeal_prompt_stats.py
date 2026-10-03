"""How people's letter choices compare across appeal prompt versions.

The unit is a pick: a chosen ProposedAppeal row that carries the ids of the
drafts that were on screen when the person chose (presented_ids). Picks
without that report are left out: which drafts they were choosing between
is a guess, and a guess is not good enough to compare prompts with. Only
drafts with a prompt version (written by a letter prompt) count; templates,
synthesized letters and older drafts are left out of both sides.

Three views:

* By version: how often a draft of each version was picked when it was on
  screen, with a 95% interval.
* By model and version: the same, per model, since the contract can help one
  model and do nothing for another.
* Head to head: only the pages that showed drafts of both versions. Each
  such pick is compared with what blind chance would give, from how many
  drafts of each version that page showed. This is the fairest view, since
  the two versions met on the same page, for the same case.
"""

from __future__ import annotations

import datetime
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from fighthealthinsurance.ml.appeal_prompt_versions import PROMPT_V1, PROMPT_V2
from fighthealthinsurance.ml.model_identity import normalize_model_label

VERSIONS = (PROMPT_V1, PROMPT_V2)

# How many picks the head-to-head needs before the page states a lean.
MIN_MIXED_PICKS = 100


def wilson_interval(successes: int, trials: int) -> Optional[Tuple[float, float]]:
    """95% Wilson score interval for a rate, as fractions; None with no trials."""
    if trials <= 0:
        return None
    z = 1.96
    p = successes / trials
    denom = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass
class RateRow:
    label: str
    version: str
    chosen: int = 0
    presented: int = 0
    edited: int = 0

    @property
    def rate(self) -> Optional[float]:
        return self.chosen / self.presented if self.presented else None

    @property
    def interval(self) -> Optional[Tuple[float, float]]:
        return wilson_interval(self.chosen, self.presented)

    @property
    def edited_rate(self) -> Optional[float]:
        return self.edited / self.chosen if self.chosen else None

    # Display strings for the staff page, so the template does no arithmetic.
    @property
    def rate_text(self) -> str:
        rate = self.rate
        return "—" if rate is None else f"{rate * 100:.1f}%"

    @property
    def interval_text(self) -> str:
        bounds = self.interval
        if bounds is None:
            return "—"
        return f"{bounds[0] * 100:.1f} to {bounds[1] * 100:.1f}%"

    @property
    def edited_text(self) -> str:
        rate = self.edited_rate
        return "—" if rate is None else f"{rate * 100:.0f}%"


@dataclass
class HeadToHead:
    picks: int = 0
    v1_chosen: int = 0
    v2_chosen: int = 0
    other_chosen: int = 0
    v2_expected: float = 0.0

    @property
    def versioned_picks(self) -> int:
        return self.v1_chosen + self.v2_chosen

    @property
    def enough(self) -> bool:
        return self.versioned_picks >= MIN_MIXED_PICKS

    @property
    def v2_ratio(self) -> Optional[float]:
        """v2 picks over the number blind chance gives; 1.0 is no difference."""
        return self.v2_chosen / self.v2_expected if self.v2_expected else None

    @property
    def v2_expected_text(self) -> str:
        return f"{self.v2_expected:.1f}"

    @property
    def v2_ratio_text(self) -> str:
        ratio = self.v2_ratio
        return "—" if ratio is None else f"{ratio:.2f}"


@dataclass
class PromptComparison:
    since: Optional[datetime.datetime]
    picks_considered: int = 0
    picks_without_report: int = 0
    by_version: List[RateRow] = field(default_factory=list)
    by_model: List[RateRow] = field(default_factory=list)
    head_to_head: HeadToHead = field(default_factory=HeadToHead)


def compare_prompt_versions(
    since: Optional[datetime.datetime] = None,
) -> PromptComparison:
    """Compare picks across prompt versions for picks made since ``since``."""
    from fighthealthinsurance.models import ProposedAppeal

    picks = ProposedAppeal.objects.filter(chosen=True)
    if since is not None:
        picks = picks.filter(created_at__gte=since)
    result = PromptComparison(since=since)
    result.picks_without_report = picks.filter(presented_ids__isnull=True).count()

    reported = list(
        picks.exclude(presented_ids__isnull=True).values_list(
            "presented_ids", "prompt_version", "model_name", "editted"
        )
    )
    shown_ids = sorted(
        {
            int(i)
            for ids, _v, _m, _e in reported
            if isinstance(ids, list)
            for i in ids
            if isinstance(i, int)
        }
    )
    draft: Dict[int, Tuple[str, str]] = {}
    # sqlite caps query parameters per statement, so long lists go in chunks.
    for start in range(0, len(shown_ids), 500):
        for draft_id, version, model_name in ProposedAppeal.objects.filter(
            id__in=shown_ids[start : start + 500], prompt_version__in=VERSIONS
        ).values_list("id", "prompt_version", "model_name"):
            draft[draft_id] = (
                str(version),
                normalize_model_label(model_name) or "unknown",
            )

    by_version = {v: RateRow(label=v, version=v) for v in VERSIONS}
    by_model: Dict[Tuple[str, str], RateRow] = {}
    h2h = result.head_to_head

    def model_row(model: str, version: str) -> RateRow:
        key = (model, version)
        if key not in by_model:
            by_model[key] = RateRow(label=model, version=version)
        return by_model[key]

    for ids, chosen_version, chosen_model, edited in reported:
        if not isinstance(ids, list):
            continue
        # Once per pick per draft: a repeated id in a report must not count
        # the same draft twice.
        on_screen = [draft[i] for i in dict.fromkeys(ids) if i in draft]
        if not on_screen:
            continue
        result.picks_considered += 1
        for version, model in on_screen:
            by_version[version].presented += 1
            model_row(model, version).presented += 1
        if chosen_version in by_version:
            row = by_version[chosen_version]
            row.chosen += 1
            row.edited += 1 if edited else 0
            mrow = model_row(
                normalize_model_label(chosen_model) or "unknown", chosen_version
            )
            mrow.chosen += 1
            mrow.edited += 1 if edited else 0
        versions_shown = Counter(v for v, _m in on_screen)
        if versions_shown[PROMPT_V1] and versions_shown[PROMPT_V2]:
            h2h.picks += 1
            if chosen_version in by_version:
                # Blind chance among the versioned drafts on this page. A
                # pick of something else (a template, a synthesized letter)
                # says nothing about the two prompts, so it adds no
                # expectation either.
                h2h.v2_expected += versions_shown[PROMPT_V2] / len(on_screen)
                if chosen_version == PROMPT_V2:
                    h2h.v2_chosen += 1
                else:
                    h2h.v1_chosen += 1
            else:
                h2h.other_chosen += 1

    result.by_version = [by_version[v] for v in VERSIONS]
    result.by_model = sorted(
        by_model.values(), key=lambda r: (r.label.lower(), r.version)
    )
    return result

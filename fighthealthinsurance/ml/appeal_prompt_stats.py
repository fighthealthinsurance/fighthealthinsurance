"""Head to head: how people choose between appeal prompt versions on pages
that showed both.

The Model Usage dashboard counts picks per version the way it counts picks
per model (ModelUsageDashboardView._prompt_version_stats). This adds the
fairest view of a random-draw test: for each pair of versions, only pages
whose drafts on screen included both, for the same case, compared with what
blind chance would give from how many drafts of each of the two that page
showed.

The unit is a pick that reported which drafts were on screen
(``presented_ids``). Picks without that report are left out: which drafts
they chose between is a guess, and a guess is not good enough here.
"""

from __future__ import annotations

import datetime
import itertools
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional

from fighthealthinsurance.ml.appeal_prompt_versions import (
    PROMPT_V1,
    PROMPT_V2,
    PROMPT_V3,
)

VERSIONS = (PROMPT_V1, PROMPT_V2, PROMPT_V3)

# Every pair of versions, earlier version first: v1-v2, v1-v3, v2-v3.
PAIRS = tuple(itertools.combinations(VERSIONS, 2))

# How many picks the head-to-head needs before the page states a lean.
MIN_MIXED_PICKS = 100


@dataclass
class HeadToHead:
    """One pair of versions on the pages that showed both. ``second`` is the
    version measured: its picks against what blind chance gives it."""

    first: str = PROMPT_V1
    second: str = PROMPT_V2
    picks: int = 0
    first_chosen: int = 0
    second_chosen: int = 0
    other_chosen: int = 0
    second_expected: float = 0.0

    @property
    def versioned_picks(self) -> int:
        return self.first_chosen + self.second_chosen

    @property
    def enough(self) -> bool:
        return self.versioned_picks >= MIN_MIXED_PICKS

    @property
    def second_ratio(self) -> Optional[float]:
        """``second``'s picks over the number blind chance gives; 1.0 is no
        difference."""
        return (
            self.second_chosen / self.second_expected if self.second_expected else None
        )

    @property
    def second_expected_text(self) -> str:
        return f"{self.second_expected:.1f}"

    @property
    def second_ratio_text(self) -> str:
        ratio = self.second_ratio
        return "—" if ratio is None else f"{ratio:.2f}"


def head_to_head(since: Optional[datetime.datetime] = None) -> List[HeadToHead]:
    """The head-to-head of every pair of versions (PAIRS order) for picks
    made since ``since``."""
    from fighthealthinsurance.models import ProposedAppeal

    picks = ProposedAppeal.objects.filter(chosen=True).exclude(
        presented_ids__isnull=True
    )
    if since is not None:
        picks = picks.filter(created_at__gte=since)
    reported = list(picks.values_list("presented_ids", "prompt_version"))
    shown_ids = sorted(
        {
            int(i)
            for ids, _v in reported
            if isinstance(ids, list)
            for i in ids
            if isinstance(i, int)
        }
    )
    version_of: Dict[int, str] = {}
    # sqlite caps query parameters per statement, so long lists go in chunks.
    for start in range(0, len(shown_ids), 500):
        for draft_id, version in ProposedAppeal.objects.filter(
            id__in=shown_ids[start : start + 500], prompt_version__in=VERSIONS
        ).values_list("id", "prompt_version"):
            version_of[draft_id] = str(version)

    pairs = [HeadToHead(first=first, second=second) for first, second in PAIRS]
    for ids, chosen_version in reported:
        if not isinstance(ids, list):
            continue
        # Once per pick per draft: a repeated id must not count twice.
        on_screen = [version_of[i] for i in dict.fromkeys(ids) if i in version_of]
        shown = Counter(on_screen)
        for h2h in pairs:
            if not (shown[h2h.first] and shown[h2h.second]):
                continue
            h2h.picks += 1
            if chosen_version in (h2h.first, h2h.second):
                # Blind chance among this pair's drafts on the page. A pick of
                # something else (another version, a template, a synthesized
                # letter) says nothing about these two prompts, so it adds no
                # expectation either.
                h2h.second_expected += shown[h2h.second] / (
                    shown[h2h.first] + shown[h2h.second]
                )
                if chosen_version == h2h.second:
                    h2h.second_chosen += 1
                else:
                    h2h.first_chosen += 1
            else:
                h2h.other_chosen += 1
    return pairs

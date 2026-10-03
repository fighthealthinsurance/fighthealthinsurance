"""Head to head: how people choose between appeal prompt versions on pages
that showed both.

The Model Usage dashboard counts picks per version the way it counts picks
per model (ModelUsageDashboardView._prompt_version_stats). This adds the
fairest view of a half-and-half test: only pages whose drafts on screen
included both versions, for the same case, compared with what blind chance
would give from how many drafts of each version that page showed.

The unit is a pick that reported which drafts were on screen
(``presented_ids``). Picks without that report are left out: which drafts
they chose between is a guess, and a guess is not good enough here.
"""

from __future__ import annotations

import datetime
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Optional

from fighthealthinsurance.ml.appeal_prompt_versions import PROMPT_V1, PROMPT_V2

VERSIONS = (PROMPT_V1, PROMPT_V2)

# How many picks the head-to-head needs before the page states a lean.
MIN_MIXED_PICKS = 100


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


def head_to_head(since: Optional[datetime.datetime] = None) -> HeadToHead:
    """The head-to-head for picks made since ``since``."""
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

    h2h = HeadToHead()
    for ids, chosen_version in reported:
        if not isinstance(ids, list):
            continue
        # Once per pick per draft: a repeated id must not count twice.
        on_screen = [version_of[i] for i in dict.fromkeys(ids) if i in version_of]
        shown = Counter(on_screen)
        if not (shown[PROMPT_V1] and shown[PROMPT_V2]):
            continue
        h2h.picks += 1
        if chosen_version in VERSIONS:
            # Blind chance among the versioned drafts on this page. A pick of
            # something else (a template, a synthesized letter) says nothing
            # about the two prompts, so it adds no expectation either.
            h2h.v2_expected += shown[PROMPT_V2] / len(on_screen)
            if chosen_version == PROMPT_V2:
                h2h.v2_chosen += 1
            else:
                h2h.v1_chosen += 1
        else:
            h2h.other_chosen += 1
    return h2h

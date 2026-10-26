"""The Denial Language hero's count and its caption can be read.

"Common Denial Phrases" under the count was grey (#6c757d) written straight on
the hero photograph, and the count was Bootstrap's bright green; against the
picture both measured about 1:1, and the contrast gate could only report the
pair as unresolved. On a panel of their own the gate measures both, from the
site's ink tokens.
"""

import pathlib
import re

from tests.sync.test_contrast import (
    MINIMUM_RATIO,
    all_pairs,
    load_rules,
    load_template_rules,
)

TEMPLATE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fighthealthinsurance"
    / "templates"
    / "denial_language_library.html"
)


def _stat_pairs() -> dict[str, list]:
    found: dict[str, list] = {}
    for pair in all_pairs(load_rules() + load_template_rules()):
        if pair.where == "denial_language_library.html" and pair.selector in (
            ".stat-number",
            ".stat-label",
        ):
            found.setdefault(pair.selector, []).append(pair)
    return found


def test_the_caption_and_the_count_are_measured_not_left_unresolved() -> None:
    pairs = _stat_pairs()
    assert set(pairs) == {".stat-number", ".stat-label"}
    for selector, found in pairs.items():
        for pair in found:
            assert pair.verdict != "unresolved", f"{selector} is on the photograph again"


def test_the_caption_and_the_count_meet_aa() -> None:
    for selector, found in _stat_pairs().items():
        for pair in found:
            assert pair.ratio >= MINIMUM_RATIO, (
                f"{selector} measures {pair.ratio:.2f}:1 on its panel"
            )


def test_their_colours_come_from_tokens() -> None:
    text = TEMPLATE.read_text()
    for selector in (".stat-number", ".stat-label", ".stat-item"):
        body = re.search(rf"\n\s*{re.escape(selector)} \{{([^}}]*)\}}", text).group(1)
        for prop in ("color", "background"):
            value = re.search(rf"(?:^|;)\s*{prop}\s*:\s*([^;]+)", body)
            if value:
                assert value.group(1).strip().startswith("var(--fhi-"), (
                    f"{selector} {prop} is a literal: {value.group(1).strip()}"
                )

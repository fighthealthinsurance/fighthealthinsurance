"""The appeal page's draft ordering, pinned at the source level.

There is no JS test runner in this repo, so the client half of the ranking
feature is pinned by reading appeal_fetcher.ts: the frame it must accept, the
moment it may reorder, the copy it shows, and the limit it folds at. Each of
these is a promise the server side or the owner relies on.
"""

import re
from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2]
    / "fighthealthinsurance"
    / "static"
    / "js"
    / "appeal_fetcher.ts"
).read_text()


def test_score_frames_are_handled_before_the_no_content_skip():
    score_branch = SRC.index("parsedLine.type === 'score'")
    content_skip = SRC.index("Skipping non-appeal frame without content")
    assert score_branch < content_skip


def test_ordering_is_applied_only_when_generation_is_really_over():
    # One call site (the definition does not end in a semicolon), in the
    # terminal branch of done(): the server's per-attempt done frame must
    # not trigger it, because done() may still start another attempt.
    assert SRC.count("finalizeRanking();") == 1
    done_fn = SRC.index("function done(): void {")
    call = SRC.index("finalizeRanking();")
    assert done_fn < call < SRC.index("\nfunction ", done_fn + 10)
    assert SRC.index("} else {", done_fn) < call, "in the no-retry branch"
    assert "finalizeRanking" not in SRC[SRC.index("if (phase === 'done')") : SRC.index("if (phase === 'done')") + 2500]
    # A score frame records and returns; it must not reorder mid-stream.
    branch = SRC[SRC.index("parsedLine.type === 'score'") :].split("return;", 1)[0]
    assert "finalizeRanking" not in branch and "rankedDrafts" not in branch


def test_caption_makes_no_promise_about_the_outcome():
    caption = re.search(r'RANKING_CAPTION =\s*"([^"]+)"', SRC).group(1)
    assert "not by a prediction of the outcome" in caption
    for banned in ("%", "chance", "likely", "success"):
        assert banned not in caption.lower()


def test_three_visible_then_a_show_more_button_not_a_delete():
    assert re.search(r"const RANKED_VISIBLE_LIMIT = 3;", SRC)
    assert "Show ${hidden.length} more draft" in SRC
    assert "el.hidden = true" in SRC and ".remove()" not in SRC.split("const hidden = ordered.slice")[1].split("button.remove()")[0]


def test_ungrounded_drafts_are_demoted_like_the_server_does():
    from fighthealthinsurance.ml import letter_quality

    client = int(re.search(r"const GROUNDING_DEMOTE_BELOW = (\d+);", SRC).group(1))
    assert client == letter_quality.GROUNDING_DEMOTE_BELOW


def test_scores_survive_retries_and_embedded_scores_beat_dedup():
    # doQuery also drives the automatic retries: resetting there would drop
    # every re-served draft's score right before dedup skips its letter.
    assert "draftScores = new Map();" not in SRC.split("export function doQuery", 1)[1]
    handler = SRC[SRC.index("const appealText = parsedLine.content;") :]
    assert handler.index("recordDraftScore(parsedLine.id, parsedLine.quality_score") < handler.index("appealsSoFar.some(")


def test_finalize_rebuilds_from_scratch():
    body = SRC[SRC.index("function finalizeRanking()") : SRC.index("const ordered = rankedDrafts();")]
    for cleanup in ('getElementById("appeal-ranking-note")?.remove()', 'getElementById("appeal-show-more")?.remove()', ".appeal-recommended-badge"):
        assert cleanup in body
    assert "el.hidden = false;" in SRC[SRC.index("function finalizeRanking()") :]


def test_nothing_moves_unless_every_draft_with_a_row_id_was_scored():
    start = SRC.index("function finalizeRanking()")
    body = SRC[start : SRC.index("\nfunction ", start + 10)]  # the whole function
    assert "return !!id && draftScores.has(id);" in body, "an unsaved draft is uncovered"
    assert "scorers.size === 1 &&" in body, "one scale only"
    gate = body.index("if (!everyDraftScored) {")
    # ...and an earlier, fully scored pass is undone: back to arrival order.
    incomplete = body[gate : body.index("const ordered = rankedDrafts();")]
    assert 'data-arrival-index' in incomplete and "outputContainer.append(el)" in incomplete
    assert 'clonedForm.attr("data-arrival-index"' in SRC
    assert gate < body.index("const ordered = rankedDrafts();")
    assert gate < body.index('note.id = "appeal-ranking-note"')
    assert gate < body.index("const hidden = ordered.slice")


def test_an_edited_draft_never_carries_the_label():
    assert 'clonedForm.attr("data-dirty", "1");' in SRC
    body = SRC[SRC.index("function finalizeRanking()") : SRC.index("\nfunction ", SRC.index("function finalizeRanking()") + 10)]
    assert 'top.getAttribute("data-dirty") === "1"' in body
    assert "if (!topIsDirty && draftSortKey(top)[0] === 2" in body


def test_show_more_keeps_keyboard_focus_and_exposes_state():
    body = SRC[SRC.index('button.id = "appeal-show-more";') :]
    assert 'setAttribute("aria-expanded", "false")' in body
    assert 'setAttribute("aria-controls"' in body
    assert ".focus();" in body.split("button.remove();", 1)[0]

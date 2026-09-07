"""The appeal page's draft ordering, pinned at the source level.

There is no JS test runner in this repo, so the client half of the ranking
feature is pinned by reading appeal_fetcher.ts: the frame it must accept, the
moments it reorders, the copy it shows, and the limit it folds at. Each of
these is a promise the server side or the owner relies on.

Ordering is live: a landed draft, a score frame, and the end of generation
each run the pass. Nothing moves while the reader is typing in a draft. Fresh
unscored drafts are appended and stay revealed; scored drafts past the limit
fold behind a button; a draft the reader edited is never hidden or labelled.
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


def _fn(name: str) -> str:
    start = SRC.index(f"function {name}(")
    return SRC[start : SRC.index("\nfunction ", start + 10)]


def test_score_frames_are_handled_before_the_no_content_skip():
    score_branch = SRC.index("parsedLine.type === 'score'")
    content_skip = SRC.index("Skipping non-appeal frame without content")
    assert score_branch < content_skip


def test_ranking_runs_live_and_once_more_at_the_real_end():
    # A landed draft: appended, then a live pass.
    landed = SRC.index("outputContainer.append(clonedForm);")
    assert SRC.index("applyRanking(false);", landed) < SRC.index("hasAutoScrolledToFirstAppeal", landed)
    # A score frame: recorded, then a live pass, before it returns.
    branch = SRC[SRC.index("parsedLine.type === 'score'") :].split("return;", 1)[0]
    assert "recordDraftScore(" in branch and "applyRanking(false);" in branch
    assert branch.index("recordDraftScore(") < branch.index("applyRanking(false);")
    # The final pass: exactly once, in done()'s no-retry branch, never on the
    # per-attempt done frame (done() may still start another attempt).
    assert SRC.count("applyRanking(true);") == 1
    done_fn = SRC.index("function done(): void {")
    call = SRC.index("applyRanking(true);")
    assert done_fn < call < SRC.index("\nfunction ", done_fn + 10)
    assert SRC.index("} else {", done_fn) < call, "in the no-retry branch"
    phase_done = SRC.index("if (phase === 'done')")
    assert "applyRanking" not in SRC[phase_done : phase_done + 2500]


def test_nothing_moves_while_the_reader_is_typing():
    body = _fn("applyRanking")
    assert body.index("if (readerIsTyping()) {") < body.index('getElementById("appeal-ranking-note")?.remove()')
    typing = _fn("readerIsTyping")
    assert "document.activeElement" in typing and 'tagName === "TEXTAREA"' in typing
    assert "outputContainer[0].contains(active)" in typing
    # ...and the pass is not lost: it runs once focus leaves the drafts, and
    # a deferred final pass stays final.
    deferred = body[body.index("if (readerIsTyping()) {") : body.index('getElementById("appeal-ranking-note")?.remove()')]
    assert 'addEventListener(\n        "focusout"' in deferred or 'addEventListener("focusout"' in deferred
    assert "rankingPending = rankingPending === true || final;" in deferred
    assert "setTimeout(() => applyRanking(pending), 0)" in deferred


def test_caption_makes_no_promise_about_the_outcome():
    for name in ("RANKING_CAPTION", "RANKING_CAPTION_PARTIAL"):
        caption = re.search(name + r' =\s*"([^"]+)"', SRC).group(1)
        assert "not by a prediction of the outcome" in caption
        for banned in ("%", "chance", "likely", "success"):
            assert banned not in caption.lower()


def test_three_visible_then_a_show_more_button_not_a_delete():
    assert re.search(r"const RANKED_VISIBLE_LIMIT = 3;", SRC)
    assert "Show ${hidden.length} more draft" in SRC
    fold = SRC.split("const hidden = scored.slice(RANKED_VISIBLE_LIMIT)", 1)[1].split("button.remove()", 1)[0]
    assert "el.hidden = true" in fold and ".remove()" not in fold


def test_fresh_and_edited_drafts_are_never_hidden():
    body = _fn("applyRanking")
    # Only SCORED drafts past the limit fold; unscored ones are appended after
    # them and stay revealed. An edited draft is filtered out of the fold.
    assert 'const hidden = scored.slice(RANKED_VISIBLE_LIMIT).filter((el) => el.getAttribute("data-dirty") !== "1");' in body
    assert "const ordered = [...scored, ...unscored];" in body


def test_show_more_is_sticky_for_the_session():
    body = _fn("applyRanking")
    assert "showAllDrafts = true;" in body
    assert "if (partialAtEnd || showAllDrafts) return;" in body
    # doQuery also drives the automatic retries: resetting there would refold
    # what the reader chose to open.
    assert "showAllDrafts = false;" not in SRC.split("export function doQuery", 1)[1]


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


def test_every_pass_rebuilds_from_scratch():
    body = _fn("applyRanking")
    head = body[: body.index("const drafts = outputContainer.children")]
    for cleanup in ('getElementById("appeal-ranking-note")?.remove()', 'getElementById("appeal-show-more")?.remove()', ".appeal-recommended-badge"):
        assert cleanup in head
    assert "for (const el of drafts) el.hidden = false;" in body


def test_one_scale_only_and_partial_coverage_at_the_end_drops_label_and_fold():
    body = _fn("applyRanking")
    assert "const oneScale = scorers.size <= 1;" in body
    assert "const scored = oneScale ? drafts.filter" in body
    assert "return !!id && draftScores.has(id);" in body, "an unsaved draft is uncovered"
    assert "const partialAtEnd = final && !complete;" in body
    # Partial at the end: the caption says so, no badge, no fold; the order
    # already on screen stays (no reshuffle back to arrival order).
    assert "partialAtEnd ? RANKING_CAPTION_PARTIAL : RANKING_CAPTION" in body
    assert "if (!partialAtEnd) {" in body and body.index("if (!partialAtEnd) {") < body.index("top.prepend(badge)")
    assert body.index("if (partialAtEnd || showAllDrafts) return;") < body.index("const hidden = scored.slice")


def test_an_edited_draft_never_carries_the_label():
    assert 'clonedForm.attr("data-dirty", "1");' in SRC
    body = _fn("applyRanking")
    assert 'top.getAttribute("data-dirty") === "1"' in body
    assert "if (!topIsDirty && draftSortKey(top)[0] === 2)" in body


def test_show_more_keeps_keyboard_focus_and_exposes_state():
    body = SRC[SRC.index('button.id = "appeal-show-more";') :]
    assert 'setAttribute("aria-expanded", "false")' in body
    assert 'setAttribute("aria-controls"' in body
    assert ".focus();" in body.split("button.remove();", 1)[0]

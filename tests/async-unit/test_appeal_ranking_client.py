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
    """The declaration and brace-matched body of ``name``, nothing after it.

    Slicing to the next top-level declaration used to carry ninety lines of
    module state along with applyRanking, so an assertion naming it could
    pass on text outside it (review).
    """
    start = SRC.index(f"function {name}(")
    open_at = SRC.index("{", start)
    return SRC[start:open_at] + _brace_block(SRC, open_at)


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


def _brace_block(src: str, open_at: int) -> str:
    depth = 0
    for i in range(open_at, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_at : i + 1]
    raise AssertionError("unbalanced braces")


def test_nothing_moves_while_the_reader_is_using_a_draft():
    body = _fn("applyRanking")
    guard = body.index("if (readerIsInteracting()) {")
    assert guard < body.index('getElementById("appeal-ranking-note")?.remove()')
    block = _brace_block(body, body.index("{", guard))
    # The block must END by returning, and must touch nothing: delete that
    # return and the pass runs under the reader's hands (review).
    assert block.rstrip().endswith("return;\n  }"), block[-80:]
    for mutation in ("outputContainer.append", ".remove()", "el.hidden", "finalApplied ="):
        assert mutation not in block, mutation
    interacting = _fn("readerIsInteracting")
    assert "document.activeElement" in interacting
    assert "active !== document.body" in interacting and "outputContainer[0].contains(active)" in interacting
    # ...and the pass is not lost: it runs once focus leaves the drafts, and
    # a deferred final pass stays final.
    assert "rankingPending = rankingPending === true || final;" in block
    # A queued pass belongs to the generation that queued it: it captures
    # the counter and does nothing once a fresh generation has bumped it
    # (review).
    assert "const generation = rankingGeneration;" in block
    assert "if (generation === rankingGeneration) applyRanking(pending);" in block


def test_after_the_final_pass_every_pass_is_final():
    body = _fn("applyRanking")
    assert body.index("final = final || finalApplied;") < body.index("if (readerIsInteracting()) {")
    assert "if (final) finalApplied = true;" in body
    assert body.index("if (final) finalApplied = true;") < body.index('getElementById("appeal-ranking-note")?.remove()')
    assert "finalApplied = false;" not in SRC.split("export function doQuery", 1)[1]


def test_coverage_is_decided_before_anything_moves_and_a_partial_end_never_reorders():
    body = _fn("applyRanking")
    # The ranked-order move specifically (the mixed-scorer branch has its
    # own arrival-order move, earlier in the function).
    ranked_move = "if (changed) for (const el of ordered) outputContainer.append(el);"
    assert ranked_move in body
    move = body.index(ranked_move)
    assert body.index("const partialAtEnd = final && !complete;") < move
    first_move = body.index("outputContainer.append(el)")
    assert body.index("if (!anyScore && !final) return;") < first_move, "no scores, not final: the DOM is not touched"
    assert body.index("if (!partialAtEnd) {\n      const changed") < move < body.index("const first = ")
    assert "const changed = ordered.some((el, i) => el !== drafts[i]);" in body


def test_a_deduplicated_reserved_letter_with_a_score_still_reranks():
    dup = SRC[SRC.index('console.log("Duplicate appeal found. Skipping.");') :].split("return;", 1)[0]
    assert "if (parsedLine.quality_score !== undefined) applyRanking(false);" in dup


def test_two_scorers_restore_arrival_order_and_say_so_at_the_end():
    body = _fn("applyRanking")
    start = body.index("if (anyScore && !oneScale) {")
    two = _brace_block(body, body.index("{", start))
    # The caption claims arrival order, so the block must produce it: an
    # earlier single-scorer pass may have moved drafts (review).
    assert "const byArrival = drafts.slice().sort((a, b) => draftArrival(a) - draftArrival(b));" in two
    assert "if (byArrival.some((el, i) => el !== drafts[i])) for (const el of byArrival) outputContainer.append(el);" in two
    assert two.index("outputContainer.append(el)") < two.index("RANKING_CAPTION_UNRANKED")
    assert "if (final) {" in two
    # The branch no longer returns: the shared fold below applies at the end.
    assert "return;" not in two


def test_a_fresh_generation_drops_the_final_latch_but_keeps_scores_and_the_open_fold():
    # The "external models enabled" handler is the one place a NEW generation
    # starts on the same page (everything else is an automatic retry).
    start = SRC.index("External models enabled. Generating additional appeals")
    handler = SRC[start : SRC.index("doQuery(my_backend_url, my_data, my_rest_fallback_url);", start)]
    assert "retries = 0;" in handler
    assert "finalApplied = false;" in handler and "rankingPending = null;" in handler
    assert "rankingGeneration += 1;" in handler
    assert "draftScores = new Map" not in handler and "showAllDrafts = false" not in handler


def test_caption_makes_no_promise_about_the_outcome():
    for name in ("RANKING_CAPTION", "RANKING_CAPTION_PARTIAL", "RANKING_CAPTION_UNRANKED"):
        caption = re.search(name + r' =\s*"([^"]+)"', SRC).group(1)
        for banned in ("%", "chance", "likely", "success"):
            assert banned not in caption.lower()
    for name in ("RANKING_CAPTION", "RANKING_CAPTION_PARTIAL"):
        assert "not by a prediction of the outcome" in re.search(name + r' =\s*"([^"]+)"', SRC).group(1)


def test_three_visible_then_a_show_more_button_not_a_delete():
    assert re.search(r"const RANKED_VISIBLE_LIMIT = 3;", SRC)
    assert "Show ${hidden.length} more draft" in SRC
    fold = SRC.split("const hidden = foldable.slice(RANKED_VISIBLE_LIMIT)", 1)[1].split("button.remove()", 1)[0]
    assert "el.hidden = true" in fold and ".remove()" not in fold


def test_live_folds_only_scored_drafts_and_the_end_folds_whatever_is_displayed():
    body = _fn("applyRanking")
    # Live: only SCORED drafts past the limit fold; unscored ones are appended
    # after them and stay revealed. At the end: the displayed order folds,
    # scored or not (arrival order when unranked). An edited draft is never
    # hidden.
    assert "const foldable = final ? displayed : oneScale ? scored : [];" in body
    assert 'const hidden = foldable.slice(RANKED_VISIBLE_LIMIT).filter((el) => el.getAttribute("data-dirty") !== "1");' in body
    assert "const ordered = [...scored, ...unscored];" in body
    assert "foldable[RANKED_VISIBLE_LIMIT - 1].after(button);" in body


def test_with_no_scores_live_passes_touch_nothing_but_the_end_still_folds():
    body = _fn("applyRanking")
    assert "if (!anyScore && !final) return;" in body
    # ...and that return precedes every DOM move and the fold.
    assert body.index("if (!anyScore && !final) return;") < body.index("outputContainer.append(el)")
    assert body.index("if (!anyScore && !final) return;") < body.index("const foldable = ")


def test_show_more_is_sticky_for_the_session():
    body = _fn("applyRanking")
    assert "showAllDrafts = true;" in body
    assert "if (showAllDrafts) return;" in body
    assert body.index("if (showAllDrafts) return;") < body.index("const foldable = ")
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
    # Partial end: no label, but the fold still applies to the displayed order.
    assert body.index("if (!partialAtEnd) {\n      const top = scored[0];") < body.index("top.prepend(badge)")
    assert "const foldable = final ? displayed : oneScale ? scored : [];" in body


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

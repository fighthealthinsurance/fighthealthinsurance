"""OCR accuracy guards for the browser-side denial reader.

A user reported being told to type their denial while a file was attached.
Fixing the messaging (PR 988) exposed why the read failed so often in the
first place, and these pin the four causes so they cannot come back.

There is no JS test harness in this repo, so this asserts on the source.
"""

import pathlib
import re

JS = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fighthealthinsurance"
    / "static"
    / "js"
)


def _ocr_source() -> str:
    return (JS / "scrub_ocr.ts").read_text()


def _js_function(src: str, name: str) -> str:
    """The body of one JS/TS function, brace-matched.

    The opening brace is located AFTER the parameter list closes, so a
    destructured parameter (`function f({ results })`) is not mistaken for the
    body -- that silently returned a two-word string and made assertions pass
    or fail for the wrong reason.
    """
    start = src.index(name)
    paren = src.index("(", start)
    depth = 0
    end_of_params = None
    for i in range(paren, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                end_of_params = i
                break
    assert end_of_params is not None, f"unbalanced parens reading {name}"

    depth = 0
    for i in range(src.index("{", end_of_params), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces reading {name}")


class TestTextLayerDensity:
    def test_a_scanner_stamp_does_not_suppress_ocr(self):
        """The old test was `pdfText.trim().length < 10`. A scanner stamping
        "Page 1 of 3" into the text layer produces 13 characters, which
        cleared it -- so OCR never ran and the user was handed that stamp as
        their entire denial. Worse than an empty box: the submit gate passes,
        so they send it."""
        src = _ocr_source()
        assert "MIN_TEXT_LAYER_CHARS_PER_PAGE" in src, src
        fn = _js_function(src, "async function recognizePDFPage")
        # Decided from ONE page's own text, never a document-wide average:
        # averaging drops the scanned pages of a mixed document, or discards
        # exact digital text when a short cover page pulls the mean down.
        assert re.search(
            r"pageText\.length\s*>=\s*MIN_TEXT_LAYER_CHARS_PER_PAGE", fn
        ), fn
        assert "numPages" not in fn, fn
        assert "charsPerPage" not in src, src
        # ...and the old absolute test is gone.
        assert not re.search(r"pdfText\.trim\(\)\.length\s*<\s*\d+", src), src

    def test_the_threshold_is_a_real_page_of_text(self):
        """Tens of characters is boilerplate; a real letter carries hundreds."""
        m = re.search(r"const MIN_TEXT_LAYER_CHARS_PER_PAGE\s*=\s*(\d+)", _ocr_source())
        assert m, "threshold not found"
        assert int(m.group(1)) >= 100, m.group(1)

    def test_a_sparse_layer_is_still_better_than_nothing(self):
        """If OCR comes back empty for a page, fall back to that page's own
        text rather than leaving nothing. This is only REACHABLE because the
        OCR failure is caught per page -- runOCREnginesWithGrace throws when
        every engine is empty, so an uncaught throw would skip the fallback
        entirely."""
        fn = _js_function(_ocr_source(), "async function recognizePDFPage")
        assert re.search(r"catch\s*\([^)]*\)\s*\{", fn), fn
        # The page's own text is kept whenever OCR did not reproduce it,
        # which covers "OCR returned nothing" as one case rather than as the
        # only case.
        assert "containsNormalised(ocrText, pageText)" in fn, fn
        assert re.search(r"parts\.push\(pageText\)", fn), fn


class TestRenderResolution:
    def test_pages_are_rendered_for_ocr_not_for_display(self):
        """scale 1.0 is 72 DPI. Tesseract wants ~300. For a scanned PDF that
        is not merely low resolution, it DOWNSAMPLES pixels the file already
        contains -- we were destroying the evidence before reading it."""
        src = _ocr_source()
        assert re.search(r"OCR_TARGET_DPI\s*=\s*(300|[3-9]\d\d)", src), src
        assert re.search(r"PDF_BASE_DPI\s*=\s*72", src), src
        fn = _js_function(src, "async function recognizePDFPage")
        assert "ocrRenderScale(" in fn, fn
        # The render viewport must not be the hardcoded scale-1 one.
        assert not re.search(
            r"getViewport\(\{\s*scale:\s*1\.0\s*\}\)[^\n]*\n[^\n]*render", fn
        ), fn

    def test_the_scale_is_clamped_to_a_canvas_budget(self):
        """Safari on iOS caps canvas area and returns a BLANK canvas past it,
        which is indistinguishable from OCR failing -- the exact symptom being
        fixed. A blind scale bump would break phones the worst."""
        src = _ocr_source()
        fn = _js_function(src, "function ocrRenderScale")
        assert "canvasAreaBudget()" in fn, fn
        assert "Math.min" in fn, fn
        # It MUST be able to go below 1. Refusing to defeats the clamp exactly
        # when it matters: a page already larger than the budget at scale 1
        # would allocate past it and, on iOS, come back blank.
        assert "Math.max(1" not in fn, fn
        # Single-dimension caps are separate from total area.
        assert "MAX_CANVAS_EDGE_PX" in fn, fn

    def test_the_budget_is_smallest_on_ios(self):
        budget = _js_function(_ocr_source(), "function canvasAreaBudget")
        assert "iPad|iPhone|iPod" in budget, budget
        # iPadOS reports as MacIntel; touch points are what identify it.
        assert "maxTouchPoints" in budget, budget
        assert "deviceMemory" in budget, budget

    def test_page_resources_are_released_as_the_loop_runs(self):
        """A multi-page scan at OCR resolution holds a lot of pixels, and
        pdf.js caches page proxies and rendering resources on top of that."""
        src = _ocr_source()
        page_fn = _js_function(src, "async function recognizePDFPage")
        assert re.search(r"canvas\.width\s*=\s*0", page_fn), page_fn
        assert "page.cleanup()" in page_fn, page_fn
        doc_fn = _js_function(src, "const recognizePDF")
        assert "doc.destroy()" in doc_fn, doc_fn


class TestEngines:
    def test_the_browsers_own_ocr_is_used_where_it_exists(self):
        """TextDetector calls the platform engine (Vision on Apple, ML Kit on
        Android): no download, fast, and usually better than tesseract on
        photographs. Free quality where present."""
        src = _ocr_source()
        assert "recognizeWithTextDetector" in src, src
        fn = _js_function(src, "async function recognizeWithTextDetector")
        # Feature-detected, contributing nothing rather than throwing.
        assert "TextDetector" in fn, fn
        assert 'return ""' in fn, fn
        # Decoded pixels are released.
        assert "bitmap.close()" in fn, fn

    def test_all_engines_race_together(self):
        """Adding a third engine must not mean a third bespoke branch."""
        src = _ocr_source()
        fn = _js_function(src, "async function recognizeImageText")
        for engine in ("tesseract", "qwen", "text-detector"):
            assert engine in fn, fn
        assert "runOCREnginesWithGrace(" in fn, fn

    def test_the_wait_starts_from_a_usable_result_not_the_first_to_settle(self):
        """An absent engine resolves "" INSTANTLY. Treating that as the first
        result committed us to a branch before anyone had read anything, and
        on the no-usable path that branch waited unbounded -- a later
        successful tesseract read could never be returned."""
        fn = _js_function(_ocr_source(), "async function runOCREnginesWithGrace")
        assert "sawUsable" in fn, fn
        assert re.search(r"value\.trim\(\)\.length\s*>\s*0", fn), fn
        assert "announceUsable()" in fn, fn

    def test_the_read_is_bounded_even_if_an_engine_hangs(self):
        """Without an overall ceiling one hung engine stalls the read forever:
        the batch never completes and no failure is ever reported."""
        src = _ocr_source()
        assert "OCR_TOTAL_BUDGET_MS" in src, src
        fn = _js_function(src, "async function runOCREnginesWithGrace")
        assert "remainingBudget()" in fn, fn
        # The grace window is shared, and never outlives the overall budget.
        assert re.search(
            r"Math\.min\(\s*OCR_GRACE_PERIOD_MS,\s*remainingBudget\(\)", fn
        ), fn

    def test_the_grace_is_short_enough_for_a_multi_page_scan(self):
        """It is paid PER PAGE before anything is appended, so a long grace
        turns a ten-page scan into minutes of blank box."""
        m = re.search(r"const OCR_GRACE_PERIOD_MS\s*=\s*([\d_]+)", _ocr_source())
        assert m, "grace period not found"
        assert int(m.group(1).replace("_", "")) <= 20_000, m.group(1)

    def test_a_supplementary_engine_cannot_displace_a_better_read(self):
        """TextDetector returns detected BLOCKS, which can be longer than a
        correct read while more fragmented. Longest-wins let it replace an
        accurate tesseract/qwen result the old two-engine merge would have
        kept."""
        src = _ocr_source()
        assert "ENGINE_PRECEDENCE" in src, src
        fn = _js_function(src, "function mergeOCRTexts")
        assert "precedenceOf(" in fn, fn
        # Rank is resolved FIRST and only that tier is compared; containment
        # and length then decide within it.
        assert re.search(r"precedenceOf\(c\.name\)\s*===\s*bestRank", fn), fn
        assert "includes(" in fn, fn

    def test_nothing_usable_is_still_an_error(self):
        """The caller turns this into "we couldn't read your file"; losing it
        would restore the silent failure PR 988 fixed."""
        src = _ocr_source()
        assert 'NO_USABLE_OCR = "All OCR engines failed or timed out"' in src, src
        fn = _js_function(src, "async function runOCREnginesWithGrace")
        assert "throw new Error(NO_USABLE_OCR)" in fn, fn


class TestAdvancedOCRGating:
    def test_qwen_is_gated_on_capability_not_connection(self):
        """Being on a phone used to switch the better engine off. That is
        backwards: a phone photo is the hardest input we get and Qwen is the
        engine best at reading it."""
        src = (JS / "scrub.ts").read_text()
        fn = _js_function(src, "async function initAdvancedOCRCheckbox")
        assert "detectWebGPUAvailability()" in fn, fn
        assert "deviceMemory" in fn, fn
        # Cellular / slow-effectiveType must no longer disable it.
        assert "effectiveType" not in fn, fn
        assert "cellular" not in fn, fn

    def test_an_explicit_save_data_request_is_still_honoured(self):
        """Qwen downloads a model. Overriding someone who asked their browser
        to conserve data would be rude -- that is the one case to respect."""
        src = (JS / "scrub.ts").read_text()
        assert "userAskedToSaveData" in src, src
        fn = _js_function(src, "function userAskedToSaveData")
        assert "saveData" in fn, fn
        # ...and it is ONLY saveData, not a proxy for being on mobile.
        assert "cellular" not in fn, fn
        assert "effectiveType" not in fn, fn


class TestRoundTwoRegressions:
    """Defects found reviewing the first version of this change."""

    def test_worker_startup_is_inside_the_budget(self):
        """Awaiting getTesseractWorker() before launching the others put
        worker creation -- which downloads trained data -- OUTSIDE the timer
        entirely: no other engine had started, and a stalled download stalled
        the whole read with no failure ever reported."""
        fn = _js_function(_ocr_source(), "async function recognizeImageText")
        # It is awaited INSIDE tesseract's own job, so the budget covers it.
        # What must not happen is awaiting it before the engines exist, which
        # is what put worker startup outside the timer.
        assert "getTesseractWorker()" in fn, fn
        assert fn.index("queueTesseractJob(") < fn.index("getTesseractWorker()"), fn
        # Nothing at all is awaited before the engine list is assembled.
        preamble = fn[: fn.index("engines.push(")]
        assert "await " not in preamble, preamble

    def test_qwen_does_not_outrank_tesseract(self):
        """They are peers. Ranking Qwen higher let a short vision-model
        hallucination replace a complete transcription -- worse than the
        two-engine rule it replaced, which took the longer text."""
        src = _ocr_source()
        m = re.search(r"ENGINE_PRECEDENCE[^=]*=\s*\{(.*?)\}", src, re.S)
        assert m, src
        block = m.group(1)
        qwen = int(re.search(r"qwen:\s*(\d+)", block).group(1))
        tess = int(re.search(r"tesseract:\s*(\d+)", block).group(1))
        detector = int(re.search(r'"text-detector":\s*(\d+)', block).group(1))
        assert qwen == tess, block
        assert detector < tess, block

    def test_exact_page_text_survives_an_approximate_ocr_read(self):
        """A sparse page still carries exact text, and on a denial the sparse
        bits are the ones that matter -- a stamped appeal deadline, a claim
        number. Using the text layer ONLY when OCR returned nothing let any
        non-empty approximate read delete it."""
        fn = _js_function(_ocr_source(), "async function recognizePDFPage")
        assert re.search(r"!containsNormalised\(ocrText, pageText\)", fn), fn

    def test_releasing_the_pdf_cannot_trigger_a_second_decode(self):
        """destroy() rethrows transport-teardown failures. Letting one escape
        reached recognize()'s outer catch, which re-decodes the whole PDF
        through the image route -- duplicating text already appended, or
        reporting failure after a clean read."""
        fn = _js_function(_ocr_source(), "const recognizePDF")
        destroy = fn[fn.index("doc.destroy()") - 200 : fn.index("doc.destroy()") + 200]
        assert "try {" in destroy, destroy
        assert "catch" in destroy, destroy


class TestRoundThreeRegressions:
    def test_a_page_that_could_not_be_read_is_reported(self):
        """A three-page denial that dropped page two reported complete
        success, so the missing deadline was never mentioned. Pages that DID
        read are already appended, so the caller sees text plus a failure and
        reports partial."""
        fn = _js_function(_ocr_source(), "const recognizePDF")
        assert "unreadablePages" in fn, fn
        # Every page is still attempted -- one bad page must not end the loop.
        assert re.search(r"catch\s*\([^)]*\)\s*\{[^}]*unreadablePages", fn, re.S), fn
        assert re.search(r"unreadablePages\s*>\s*0", fn), fn
        assert "throw new Error(" in fn, fn

    def test_a_partly_read_document_is_not_decoded_again(self):
        """Falling back to the other decoder after text was already appended
        either duplicates it or reports failure on a usable read."""
        fn = _js_function(_ocr_source(), "export const recognize = async function")
        assert "emitted" in fn, fn
        assert re.search(r"if\s*\(\s*emitted\s*\)\s*\{\s*throw error", fn), fn

    def test_one_page_emits_one_document(self):
        """Callers treat each addText call as a separate document --
        explain_denial inserts a separator per call -- so emitting OCR text
        and embedded text separately made one page look like two uploads."""
        fn = _js_function(_ocr_source(), "async function recognizePDFPage")
        assert "parts.join(" in fn, fn
        # Exactly one emit on the OCR path.
        assert len(re.findall(r"addText\(", fn)) <= 2, fn

    def test_duplicate_detection_ignores_whitespace_differences(self):
        """A text layer joins runs with spaces while OCR emits line breaks, so
        a raw containment check called identical content different and
        appended the whole sparse layer twice."""
        src = _ocr_source()
        assert "function containsNormalised" in src, src
        fn = _js_function(src, "function containsNormalised")
        assert "replace(" in fn, fn
        assert "\\s+" in fn, fn

    def test_a_supplementary_engine_never_outranks_a_primary_one(self):
        """Containment used to be checked before rank, so a fragmented
        TextDetector result that happened to contain the tesseract text won on
        containment despite being the least trusted engine."""
        fn = _js_function(_ocr_source(), "function mergeOCRTexts")
        # Rank is chosen first, and only that tier is compared.
        assert "bestRank" in fn, fn
        assert re.search(r"precedenceOf\(c\.name\)\s*===\s*bestRank", fn), fn
        # ...and containment is evaluated on the tier, not on all candidates.
        assert fn.index("bestRank") < fn.index(".includes("), fn

    def test_an_abandoned_page_does_not_queue_more_work(self):
        """Bounding the wait does not stop the work. The tesseract worker is
        memoized and shared, so a multi-page scan could queue one stale
        full-resolution recognition per abandoned page."""
        fn = _js_function(_ocr_source(), "async function recognizeImageText")
        assert "abandoned" in fn, fn
        assert re.search(r"if\s*\(\s*page\.abandoned\s*\)", fn), fn
        assert re.search(r"finally\s*\{\s*page\.abandoned\s*=\s*true", fn), fn


class TestRoundFourRegressions:
    def test_sparse_text_does_not_mask_a_failed_page(self):
        """Emitting the sparse layer is not the same as having read the page.
        A scanned page whose only embedded text is "Page 2 of 3" contributed
        that stamp and reported success, so the denial content on it was lost
        without a word."""
        fn = _js_function(_ocr_source(), "async function recognizePDFPage")
        assert "ocrFailed" in fn, fn
        # Success is about OCR, not about having emitted something.
        assert re.search(r"return\s+!ocrFailed\s*&&\s*ocrText\.length\s*>\s*0", fn), fn

    def test_containment_will_not_swallow_a_near_miss_identifier(self):
        """ "Appeal by 10/1" IS a substring of an OCR guess of "Appeal by
        10/15", so a plain containment test discarded the EXACT deadline in
        favour of the wrong one. Claim and member numbers fail the same way."""
        fn = _js_function(_ocr_source(), "function containsNormalised")
        assert "isWordChar" in fn, fn
        # The match has to end on a token boundary, not anywhere.
        assert re.search(r"startsToken|openOk", fn), fn
        assert re.search(r"endsToken|closeOk", fn), fn
        # A bare indexOf-and-return would be the bug.
        assert not re.search(r"return\s+flatHay\.includes\(", fn), fn

    def test_tesseract_jobs_are_serialised_so_they_can_be_dropped(self):
        """tesseract.js accepts every recognize() immediately and queues them
        internally, so a page that timed out had ALREADY handed over a
        full-resolution job. Checking abandonment when the job was created
        only covered worker startup; the check has to happen when our turn
        actually arrives."""
        src = _ocr_source()
        assert "queueTesseractJob" in src, src
        queue = _js_function(src, "function queueTesseractJob")
        # A failed page must not break the chain for the next one, and the
        # chain must also advance when a slot stalls (see round five).
        assert re.search(r"tesseractQueue\s*=\s*Promise\.race", queue), queue
        assert re.search(r"run\.then\(", queue), queue

        fn = _js_function(src, "async function recognizeImageText")
        assert "queueTesseractJob(" in fn, fn
        # The abandonment check sits AFTER the worker is awaited, i.e. once
        # our turn has come, and BEFORE the work is submitted.
        job = fn[fn.index("queueTesseractJob(") :]
        assert job.index("await getTesseractWorker()") < job.index(
            "page.abandoned"
        ), job
        assert job.index("page.abandoned") < job.index("worker.recognize("), job


class TestRoundFiveRegressions:
    def test_a_wedged_slot_cannot_block_the_queue_forever(self):
        """A rejected job could not poison the chain, but a job that never
        SETTLES could: tesseractQueue stayed permanently pending, so every
        later page -- and every later upload in the same tab -- waited behind
        it forever."""
        src = _ocr_source()
        assert "TESSERACT_SLOT_TIMEOUT_MS" in src, src
        queue = _js_function(src, "function queueTesseractJob")
        # The chain advances on success, failure, OR a stalled slot.
        assert "Promise.race(" in queue, queue
        assert "TESSERACT_SLOT_TIMEOUT_MS" in queue, queue

    def test_a_stalled_slot_throws_the_worker_away(self):
        """Advancing alone would hand the next job to the still-wedged worker
        and rebuild the same hidden internal queue."""
        src = _ocr_source()
        queue = _js_function(src, "function queueTesseractJob")
        assert "discardTesseractWorker()" in queue, queue
        discard = _js_function(src, "async function discardTesseractWorker")
        # The reference is dropped BEFORE awaiting, so a hung terminate()
        # cannot keep the next job pointed at the dead worker.
        assert discard.index("tesseractWorker = null") < discard.index("await"), discard
        assert "terminate()" in discard, discard

    def test_the_slot_timeout_never_cuts_off_a_healthy_read(self):
        """It must outlast the per-image budget, or it would kill slow but
        working reads rather than stuck ones."""
        src = _ocr_source()
        assert re.search(
            r"TESSERACT_SLOT_TIMEOUT_MS\s*=\s*OCR_TOTAL_BUDGET_MS\s*\+", src
        ), src

    def test_the_worker_is_held_not_memoized(self):
        """A memoizer has no way to say "this one is broken, build another"."""
        src = _ocr_source()
        assert "memoizeOne" not in src, src
        assert "async-memoize-one" not in src, src
        # Anchored past the Raw builder, whose name contains this one.
        fn = _js_function(src, "function getTesseractWorker(): Promise")
        assert "tesseractWorker === null" in fn, fn

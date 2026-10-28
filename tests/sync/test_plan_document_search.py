"""Long plan documents in the appeal flow.

MLPlanDocHelper sends the plan documents a person uploads whole when they
fit its summary's window. These cover the ones that don't: the best local
model is told they are too long, shown their headings and asked what to
search them for, and the passages those search strings select fill the
window. The document is tests/policy_fixtures.py's 46k-char simulated
policy, whose rules sit far past any window.

Model calls are patched throughout (infer_with_fallback and the router's
model lists): nothing here reaches a backend.
"""

import asyncio
import re
from unittest.mock import AsyncMock, patch

import pymupdf
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TransactionTestCase

from fighthealthinsurance.common_view_logic import DenialCreatorHelper
from fighthealthinsurance.ml.ml_plan_doc_helper import MLPlanDocHelper, PlanDocumentText
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import Denial, PlanDocuments
from tests.policy_fixtures import (
    APPEAL_RULE,
    BILLING_CODE,
    EXCEPTION_RULE,
    POLICY_TEXT,
    STEP_THERAPY_RULE,
    squash,
)

INFER = "fighthealthinsurance.ml.ml_plan_doc_helper.infer_with_fallback"

DENIAL_TEXT = (
    "Your request for Humira for Crohn's disease is denied. The step therapy "
    "requirements of your plan's pharmacy policy were not met."
)


def _paged(text: str, lines_per_page: int = 50) -> list[str]:
    """``text`` as pages of about ``lines_per_page`` lines, broken between
    paragraphs (a paragraph longer than a page is broken between lines)."""
    blocks = []
    for paragraph in text.split("\n\n"):
        lines = paragraph.split("\n")
        for i in range(0, len(lines), lines_per_page):
            blocks.append("\n".join(lines[i : i + lines_per_page]))
    pages: list[str] = []
    current: list[str] = []
    used = 0
    for block in blocks:
        size = block.count("\n") + 2
        if current and used + size > lines_per_page:
            pages.append("\n\n".join(current))
            current, used = [], 0
        current.append(block)
        used += size
    if current:
        pages.append("\n\n".join(current))
    return pages


POLICY_PAGES = _paged(POLICY_TEXT)
POLICY_DOCUMENT = PlanDocumentText(
    name="Plan document 1",
    text="\n\n".join(
        f"[Page {number}]\n{page}" for number, page in enumerate(POLICY_PAGES, 1)
    ),
)
SHORT_DOCUMENT = PlanDocumentText(
    name="Plan document 1",
    text="[Page 1]\nPrior authorization is required for all specialty drugs.",
)


def _page_holding(fact: str) -> int:
    return next(
        number
        for number, page in enumerate(POLICY_PAGES, 1)
        if squash(fact) in squash(page)
    )


def _policy_pdf_bytes() -> bytes:
    """The simulated policy as a real multi-page PDF."""
    doc = pymupdf.open()
    for page_text in POLICY_PAGES:
        doc.new_page().insert_text((36, 40), page_text, fontsize=7)
    data: bytes = doc.tobytes()
    doc.close()
    return data


class TestTheFixture(SimpleTestCase):
    def test_the_policy_is_far_too_long_for_the_window(self):
        self.assertGreater(
            len(POLICY_DOCUMENT.text), 4 * MLPlanDocHelper.MAX_CONTEXT_LENGTH
        )

    def test_the_rules_sit_past_the_window(self):
        window = squash(POLICY_DOCUMENT.text[: MLPlanDocHelper.MAX_CONTEXT_LENGTH])
        for rule in (STEP_THERAPY_RULE, EXCEPTION_RULE, APPEAL_RULE):
            self.assertNotIn(squash(rule), window)


class TestParseSearchTerms(SimpleTestCase):
    def test_one_per_line_is_kept_in_order(self):
        self.assertEqual(
            MLPlanDocHelper.parse_search_terms("step therapy\nexception\nappeal"),
            ["step therapy", "exception", "appeal"],
        )

    def test_list_markers_and_quotes_are_dropped(self):
        answer = '1. "Step therapy"\n- exception\n* **appeal rights**\n2) J0135'
        self.assertEqual(
            MLPlanDocHelper.parse_search_terms(answer),
            ["Step therapy", "exception", "appeal rights", "J0135"],
        )

    def test_a_heading_line_is_not_a_term(self):
        self.assertEqual(
            MLPlanDocHelper.parse_search_terms("Search strings:\nstep therapy"),
            ["step therapy"],
        )

    def test_a_comma_separated_answer_is_split(self):
        self.assertEqual(
            MLPlanDocHelper.parse_search_terms("step therapy, exception; appeal"),
            ["step therapy", "exception", "appeal"],
        )

    def test_repeats_and_sentences_are_dropped(self):
        answer = "appeal\nAppeal\nThese are the strings I would search the plan for"
        self.assertEqual(MLPlanDocHelper.parse_search_terms(answer), ["appeal"])

    def test_at_most_the_maximum_is_kept(self):
        answer = "\n".join(f"term {i}" for i in range(20))
        self.assertEqual(
            len(MLPlanDocHelper.parse_search_terms(answer)),
            MLPlanDocHelper.MAX_SEARCH_TERMS,
        )


class TestDocumentOutline(SimpleTestCase):
    def test_the_policys_headings_are_shown(self):
        outline = MLPlanDocHelper.document_outline([POLICY_DOCUMENT])
        for heading in ("STEP THERAPY REQUIREMENTS", "EXCLUSIONS", "APPEAL RIGHTS"):
            self.assertIn(heading, outline)

    def test_body_text_is_not_shown(self):
        outline = MLPlanDocHelper.document_outline([POLICY_DOCUMENT])
        self.assertNotIn("tried and failed", outline)

    def test_the_outline_is_bounded(self):
        outline = MLPlanDocHelper.document_outline([POLICY_DOCUMENT] * 3)
        self.assertLessEqual(len(outline), MLPlanDocHelper.MAX_OUTLINE_CHARS + 10)

    def test_a_document_without_headings_is_shown_by_its_opening(self):
        doc = PlanDocumentText(
            name="Plan document 1",
            text="[Page 1]\nthis plan covers specialty drugs when they are needed.",
        )
        outline = MLPlanDocHelper.document_outline([doc])
        self.assertIn("this plan covers specialty drugs", outline)
        self.assertNotIn("[Page 1]", outline)


class TestRelevantPassages(SimpleTestCase):
    TERMS = ["step therapy", "Humira", "exception", "appeal", BILLING_CODE]

    def setUp(self):
        self.text = MLPlanDocHelper.relevant_passages([POLICY_DOCUMENT], self.TERMS)

    def test_each_rule_the_search_strings_point_to_is_selected(self):
        for rule in (STEP_THERAPY_RULE, EXCEPTION_RULE, APPEAL_RULE):
            with self.subTest(rule=rule[:30]):
                self.assertIn(squash(rule), squash(self.text))

    def test_the_selection_fits_the_window(self):
        self.assertLessEqual(len(self.text), MLPlanDocHelper.MAX_CONTEXT_LENGTH)

    def test_each_passage_names_its_page(self):
        start = squash(self.text).index(squash(STEP_THERAPY_RULE))
        label = re.findall(r"\[Plan document 1, page (\d+)\]", squash(self.text)[:start])
        self.assertEqual(int(label[-1]), _page_holding(STEP_THERAPY_RULE))

    def test_passages_come_in_reading_order(self):
        pages = [int(n) for n in re.findall(r"page (\d+)\]", self.text)]
        self.assertEqual(pages, sorted(pages))

    def test_a_string_on_every_page_does_not_crowd_out_the_billing_code(self):
        text = MLPlanDocHelper.relevant_passages(
            [POLICY_DOCUMENT], ["adalimumab", BILLING_CODE]
        )
        self.assertIn(f"{BILLING_CODE} | Injection, adalimumab, 20 mg", text)

    def test_no_search_strings_select_nothing(self):
        self.assertEqual(MLPlanDocHelper.relevant_passages([POLICY_DOCUMENT], []), "")


class _FakeModel:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return self.name


BEST = _FakeModel("best")
POOL = [_FakeModel("cheap-1"), BEST, _FakeModel("cheap-2"), _FakeModel("cheap-3")]


class TestGenerateSearchTerms(SimpleTestCase):
    def _ask(self, answer, *, best=BEST, **kwargs):
        """Run generate_search_terms over the policy against a model that
        answers ``answer``; returns the terms and the inference mock."""
        infer = AsyncMock(return_value=answer)
        arguments = dict(
            denial_text=DENIAL_TEXT,
            procedure="Humira",
            diagnosis="Crohn's disease",
            documents=[POLICY_DOCUMENT],
        )
        arguments.update(kwargs)
        with (
            patch(INFER, new=infer),
            patch.object(ml_router, "best_internal_model", return_value=best),
            patch.object(
                ml_router, "general_purpose_internal_models", return_value=POOL
            ),
        ):
            terms = asyncio.run(MLPlanDocHelper.generate_search_terms(**arguments))
        return terms, infer

    def _prompt(self):
        _, infer = self._ask("step therapy")
        return infer.await_args.kwargs["prompt"]

    def test_the_best_local_model_is_asked_first(self):
        _, infer = self._ask("step therapy")
        self.assertEqual(infer.await_args.kwargs["models"], [BEST, POOL[0], POOL[2]])

    def test_without_a_best_model_the_cheapest_are_asked(self):
        _, infer = self._ask("step therapy", best=None)
        self.assertEqual(infer.await_args.kwargs["models"], POOL[:3])

    def test_the_prompt_says_the_documents_are_too_long(self):
        prompt = self._prompt()
        self.assertIn(f"{len(POLICY_DOCUMENT.text):,} characters", prompt)
        self.assertIn(f"about {MLPlanDocHelper.MAX_CONTEXT_LENGTH:,} fit", prompt)

    def test_the_prompt_shows_the_documents_headings(self):
        self.assertIn("STEP THERAPY REQUIREMENTS", self._prompt())

    def test_the_prompt_asks_for_the_documents_own_words(self):
        self.assertIn("use the documents' own terms", self._prompt())

    def test_the_prompt_carries_the_denial(self):
        prompt = self._prompt()
        self.assertIn("step therapy requirements of your plan's pharmacy", prompt)
        self.assertIn("Denied procedure: Humira", prompt)

    def test_the_models_answer_becomes_the_search_strings(self):
        terms, _ = self._ask("1. step therapy\n2. exception")
        self.assertEqual(terms[:2], ["step therapy", "exception"])

    def test_the_denied_treatment_is_searched_for_even_if_the_model_left_it_out(self):
        terms, _ = self._ask("step therapy")
        self.assertIn("Humira", terms)

    def test_a_failed_model_falls_back_to_terms_from_the_denial(self):
        terms, _ = self._ask(None)
        self.assertIn("humira", terms)
        self.assertIn("appeal", terms)

    def test_no_denial_details_still_give_something_to_search_for(self):
        terms, infer = self._ask(
            "unused", denial_text="", procedure=None, diagnosis=None
        )
        self.assertTrue(terms)
        infer.assert_not_awaited()


class TestExtractRelevantText(SimpleTestCase):
    DENIAL = Denial(denial_text=DENIAL_TEXT, procedure="Humira")

    def _extract(self, documents, search_terms=None, terms_from_model=()):
        choose = AsyncMock(return_value=list(terms_from_model))
        with (
            patch.object(
                MLPlanDocHelper,
                "load_plan_documents",
                new=AsyncMock(return_value=documents),
            ),
            patch.object(MLPlanDocHelper, "generate_search_terms", new=choose),
        ):
            text = asyncio.run(
                MLPlanDocHelper.extract_relevant_text(
                    1, search_terms, denial=self.DENIAL
                )
            )
        return text, choose

    def test_documents_that_fit_go_in_whole(self):
        text, _ = self._extract([SHORT_DOCUMENT])
        self.assertEqual(text, f"[Plan document 1]\n{SHORT_DOCUMENT.text}")

    def test_documents_that_fit_need_no_search_strings(self):
        _, choose = self._extract([SHORT_DOCUMENT])
        choose.assert_not_awaited()

    def test_documents_too_long_are_searched_with_the_models_strings(self):
        text, _ = self._extract([POLICY_DOCUMENT], terms_from_model=["step therapy"])
        self.assertIn(squash(STEP_THERAPY_RULE), squash(text))

    def test_the_model_choosing_them_is_shown_the_documents(self):
        _, choose = self._extract([POLICY_DOCUMENT], terms_from_model=["appeal"])
        self.assertEqual(choose.await_args.kwargs["documents"], [POLICY_DOCUMENT])

    def test_search_terms_given_are_used_as_they_are(self):
        text, choose = self._extract([SHORT_DOCUMENT], search_terms=["specialty"])
        choose.assert_not_awaited()
        self.assertIn("[Plan document 1, page 1]", text)

    def test_no_documents_give_nothing(self):
        text, _ = self._extract([])
        self.assertEqual(text, "")


class TestWhatThePersonEmptiedIsNotAskedAbout(SimpleTestCase):
    """A procedure or diagnosis emptied on the review page is not put back
    from extraction's copy into the plan-document prompts."""

    EMPTIED = Denial(
        denial_text=DENIAL_TEXT,
        procedure=None,
        candidate_procedure="Humira",
        diagnosis=None,
        candidate_diagnosis="Crohn's disease",
        review_cleared=" diagnosis procedure ",
    )

    def test_the_search_strings_are_not_chosen_for_it(self):
        choose = AsyncMock(return_value=["step therapy"])
        with (
            patch.object(
                MLPlanDocHelper,
                "load_plan_documents",
                new=AsyncMock(return_value=[POLICY_DOCUMENT]),
            ),
            patch.object(MLPlanDocHelper, "generate_search_terms", new=choose),
        ):
            asyncio.run(
                MLPlanDocHelper.extract_relevant_text(1, denial=self.EMPTIED)
            )
        asked = choose.await_args.kwargs
        self.assertEqual((asked["procedure"], asked["diagnosis"]), (None, None))

    def test_the_summary_is_not_asked_about_it(self):
        infer = AsyncMock(return_value="A summary of the plan's rules.")
        with patch(INFER, new=infer):
            asyncio.run(
                MLPlanDocHelper.summarize_relevant_sections(
                    self.EMPTIED,
                    "Specialty drugs need prior authorization. Appeals are due "
                    "within 180 days of the notice.",
                )
            )
        prompt = infer.await_args.kwargs["prompt"]
        self.assertNotIn("Humira", prompt)
        self.assertNotIn("Crohn", prompt)


class TestALongPlanDocumentInTheAppealFlow(TransactionTestCase):
    """generate_plan_documents_summary over a real encrypted multi-page PDF
    of the policy, uploaded with a denial as the appeal flow stores it."""

    SEARCH_TERMS_ANSWER = '1. step therapy\n2. exception\n- appeal\n"quantity limit"'
    SUMMARY = "Step therapy requires two preferred biosimilars first (page 8)."

    def _summarize(self, document_bytes, filename="policy.pdf"):
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("long-plan@example.com"),
            denial_text=DENIAL_TEXT,
            procedure="Humira",
        )
        doc = PlanDocuments(denial=denial)
        doc.plan_document_enc.save(filename, ContentFile(document_bytes), save=True)
        self.calls: dict = {}

        async def fake_infer(**kwargs):
            self.calls[kwargs["label"]] = kwargs
            if kwargs["label"] == "search terms":
                return self.SEARCH_TERMS_ANSWER
            return self.SUMMARY

        with (
            patch(INFER, new=AsyncMock(side_effect=fake_infer)),
            patch.object(ml_router, "best_internal_model", return_value=BEST),
            patch.object(
                ml_router, "general_purpose_internal_models", return_value=POOL
            ),
        ):
            summary = asyncio.run(
                MLPlanDocHelper.generate_plan_documents_summary(denial.denial_id)
            )
        denial.refresh_from_db()
        return summary, denial

    def _summary_prompt(self):
        self._summarize(_policy_pdf_bytes())
        return self.calls["plan doc summary"]["prompt"]

    def test_the_summary_is_saved_on_the_denial(self):
        summary, denial = self._summarize(_policy_pdf_bytes())
        self.assertEqual(summary, self.SUMMARY)
        self.assertEqual(denial.plan_documents_summary, self.SUMMARY)

    def test_the_best_local_model_chose_the_search_strings(self):
        self._summarize(_policy_pdf_bytes())
        self.assertIs(self.calls["search terms"]["models"][0], BEST)

    def test_the_summary_reads_the_rules_buried_in_the_policy(self):
        prompt = squash(self._summary_prompt())
        for rule in (STEP_THERAPY_RULE, EXCEPTION_RULE, APPEAL_RULE):
            with self.subTest(rule=rule[:30]):
                self.assertIn(squash(rule), prompt)

    def test_the_summary_is_told_which_page_each_passage_is_from(self):
        self.assertIn("[Plan document 1, page ", self._summary_prompt())

    def test_a_short_plan_document_is_summarized_whole_without_search_strings(self):
        summary, _ = self._summarize(
            b"Prior authorization is required for all specialty drugs.", "plan.txt"
        )
        self.assertEqual(summary, self.SUMMARY)
        self.assertNotIn("search terms", self.calls)


class TestPlanDocumentTextForTheFaxLookup(TransactionTestCase):
    def test_an_encrypted_plan_document_is_read_decrypted(self):
        # The fax lookup read the stored file's path, which for an
        # encrypted upload is ciphertext.
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("plan-fax@example.com"),
            denial_text="Denied.",
        )
        doc = PlanDocuments(denial=denial)
        doc.plan_document_enc.save(
            "plan.txt",
            ContentFile(b"Fax appeals to (555) 123-4567 within 180 days."),
            save=True,
        )
        text = asyncio.run(
            DenialCreatorHelper.get_plan_documents_text(denial.denial_id)
        )
        self.assertIn("Fax appeals to (555) 123-4567", text)

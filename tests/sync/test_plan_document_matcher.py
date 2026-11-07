"""plan_document_matcher on rows: the carrier, the match, the hashes, the
refresh, the extraction step and the prompt section.

The pure rules (``score_candidate``, ``select``) are covered in
tests/async-unit/test_plan_document_matcher_rules.py. Here the shared
brief's corpus is built as ``PublicPlanDocument`` rows and real denials
are matched against it. Nothing reaches a model: the tie-break
(``plan_document_matcher.infer_with_fallback``) and MLPlanDocHelper's
search-term and summary calls are AsyncMocks.

The sync pieces run in ``TestCase``. Everything that awaits
``arefresh_match`` or ``match_public_plan_documents`` runs in
``TransactionTestCase`` under ``asyncio.run``: the async side does its ORM
work on another thread, which sees only committed rows (the precedent in
test_ml_plan_doc_helper.py).

The letters are invented.
"""

import asyncio
import contextlib
import time
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from django.test import TestCase, TransactionTestCase, override_settings
from loguru import logger

from fighthealthinsurance import pa_requirements, plan_document_matcher
from fighthealthinsurance.common_view_logic import (
    EXTRACTION_OUTCOME_FOUND,
    EXTRACTION_OUTCOME_NOTHING_FOUND,
    DenialCreatorHelper,
)
from fighthealthinsurance.ml.ml_plan_doc_helper import (
    MLPlanDocHelper,
    PlanDocumentText,
)
from fighthealthinsurance.models import (
    Denial,
    InsuranceCompany,
    InsurancePlan,
    LineOfBusiness,
    PlanDocumentMatch,
    PlanDocuments,
    PlanSource,
    PlanSourceRelation,
    PublicPlanDocument,
)
from fighthealthinsurance.plan_document_matcher import (
    CONTEXT_MAX_CHARS,
    PASSAGE_BUDGET_CHARS,
    PLAN_DOCUMENT_END,
    PLAN_DOCUMENT_GUARD,
    SHORT_BUDGET_SECONDS,
    arefresh_match,
    build_relevance,
    current_hashes,
    format_plan_document_context,
    get_plan_document_context_for_denial,
    match_documents,
    resolve_carrier,
)

EOC = PublicPlanDocument.DOCUMENT_TYPE_EVIDENCE_OF_COVERAGE
SBC = PublicPlanDocument.DOCUMENT_TYPE_SUMMARY_OF_BENEFITS

KAISER_REGEX = r"kaiser\s*permanente|kaiser(?!\s*foundation)"
NORCAL_NEGATIVE_REGEX = (
    r"\bsouthern\s+california\b|\blos\s+angeles\b|\bsenior\s+advantage\b"
    r"|\bmedi[-\s]cal\b"
)
NORCAL_SBC_NEGATIVE_REGEX = (
    r"\bsouthern\s+california\b|\blos\s+angeles\b|\bsenior\s+advantage\b"
)
SOCAL_NEGATIVE_REGEX = (
    r"\bnorthern\s+california\b|\bsan\s+francisco\b|\bsenior\s+advantage\b"
)

SF_LETTER = (
    "Kaiser Permanente San Francisco Medical Center denied an MRI as not "
    "medically necessary. Northern California Region."
)
SENIOR_ADVANTAGE_LETTER = (
    "Kaiser Permanente Senior Advantage (Medicare Advantage) denied a wheelchair."
)
BOTH_REGIONS_LETTER = "Kaiser Permanente: treated in San Francisco and Los Angeles."
GENERIC_LETTER = "Kaiser Permanente denied an MRI."
DENTAL_LETTER = "Kaiser Permanente Northern California: your dental crown was denied."
# Names the region and the Deductible HMO product: the NorCal EOC (39),
# its SBC (35) and the 2024 Deductible HMO EOC (44) are all eligible, and
# the HMO conflicts with the other two by plan year within the margin.
TIE_BREAK_LETTER = (
    "Kaiser Permanente Northern California Deductible HMO denied an MRI as "
    "not medically necessary."
)

# Occurs in the NorCal EOC's text and nowhere else: a log line that carried
# document text would carry it.
DOCUMENT_SENTINEL = "the indigo heron clause"
# What the (stubbed) search-term model answers; every document's text
# carries these words, so passage selection always finds something.
SEARCH_TERMS = ["MRI", "medical necessity", "appeal"]
SUMMARY = (
    "Summary A: the plan covers an MRI when medically necessary; appeals go "
    "to Member Services within 180 days."
)
TOPICS = (
    "Definitions",
    "How to obtain services",
    "Benefits and cost sharing",
    "Imaging services",
    "Exclusions and limitations",
    "Grievances and appeals",
    "Member rights",
)


def _page(number: int, topic: str) -> str:
    sentence = (
        f"Section {number}. {topic}. Services are covered when medical necessity "
        "is established by the treating physician under the plan's criteria. "
        "Magnetic resonance imaging (MRI) of the spine requires prior "
        "authorization; an appeal may be filed within 180 days of a denial. "
    )
    return f"[Page {number}]\n" + sentence * 10


def _long_eoc_text() -> str:
    """Seven pages of about 2,700 characters: far past PASSAGE_BUDGET_CHARS,
    so the passages have to be selected."""
    pages = []
    for number, topic in enumerate(TOPICS, start=1):
        text = _page(number, topic)
        if number == 4:
            text += f"Under {DOCUMENT_SENTINEL} an MRI ordered by a plan physician is covered."
        pages.append(text)
    return "\n\n".join(pages)


LONG_EOC_TEXT = _long_eoc_text()
SHORT_TEXT = (
    "[Page 1]\nSummary of Benefits and Coverage. Imaging (MRI) requires prior "
    "authorization; medical necessity criteria apply. You may appeal a denial."
    "\n\n[Page 2]\nMember Services and appeal contacts."
)


@contextlib.contextmanager
def _no_models(*, tie_break_answer=None, summary=SUMMARY, summarize=None):
    """Every model call the matcher can make, stubbed: the tie-break answers
    ``tie_break_answer`` (None: no model answered), MLPlanDocHelper's search
    strings are SEARCH_TERMS and its summary is ``summary`` (or the given
    ``summarize`` mock)."""
    with (
        patch(
            "fighthealthinsurance.plan_document_matcher.infer_with_fallback",
            new=AsyncMock(return_value=tie_break_answer),
        ) as infer,
        patch.object(
            MLPlanDocHelper,
            "generate_search_terms",
            new=AsyncMock(return_value=list(SEARCH_TERMS)),
        ) as search,
        patch.object(
            MLPlanDocHelper,
            "summarize_relevant_sections",
            new=summarize or AsyncMock(return_value=summary),
        ) as summarize_mock,
    ):
        yield SimpleNamespace(infer=infer, search=search, summarize=summarize_mock)


@contextlib.contextmanager
def _captured_logs():
    """Every loguru record emitted inside, formatted, from any thread."""
    lines: list = []
    sink_id = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
    try:
        yield lines
    finally:
        logger.remove(sink_id)


class _CorpusMixin:
    """The shared brief's fixture recipe: Kaiser and Aetna, the Medicare
    Advantage plan, and seven documents. Built in setUp."""

    def build_corpus(self):
        # The name resolver caches its regex candidates by a five-minute
        # time bucket. An entry left by another test's rows would resolve a
        # typed insurer to whichever row now holds that id.
        pa_requirements._regex_candidates.cache_clear()
        self.addCleanup(pa_requirements._regex_candidates.cache_clear)
        self.kaiser = InsuranceCompany.objects.create(
            name="Kaiser Permanente", alt_names="Kaiser\nKP", regex=KAISER_REGEX
        )
        self.aetna = InsuranceCompany.objects.create(
            name="Aetna", alt_names="Aetna Health", regex=r"aetna"
        )
        self.ma_source = PlanSource.objects.create(
            name="Medicare Advantage", regex="medicare advantage", negative_regex=""
        )
        self.ma_plan = InsurancePlan.objects.create(
            insurance_company=self.kaiser, plan_name="Medicare Advantage", state=""
        )
        self.norcal_eoc = self.document(
            "kaiser-norcal-eoc-2025",
            title="Kaiser Permanente Northern California HMO Evidence of Coverage",
            region="Northern California",
            match_keywords=[
                "northern california",
                "san francisco",
                "sacramento",
                "san jose",
                "santa clara",
                "walnut creek",
            ],
            negative_regex=NORCAL_NEGATIVE_REGEX,
            text=LONG_EOC_TEXT,
            source_url="https://example.com/kaiser/norcal-eoc-2025.pdf",
        )
        self.norcal_sbc = self.document(
            "kaiser-norcal-sbc-2025",
            title=(
                "Kaiser Permanente Northern California HMO Summary of Benefits "
                "and Coverage"
            ),
            document_type=SBC,
            region="Northern California",
            match_keywords=["northern california", "san francisco"],
            negative_regex=NORCAL_SBC_NEGATIVE_REGEX,
            text=SHORT_TEXT,
            source_url="https://example.com/kaiser/norcal-sbc-2025.pdf",
        )
        self.socal_eoc = self.document(
            "kaiser-socal-eoc-2025",
            title="Kaiser Permanente Southern California HMO Evidence of Coverage",
            region="Southern California",
            match_keywords=["southern california", "los angeles", "san diego"],
            negative_regex=SOCAL_NEGATIVE_REGEX,
        )
        self.senior_advantage_eoc = self.document(
            "kaiser-senior-advantage-eoc-2025",
            title="Kaiser Permanente Senior Advantage Evidence of Coverage",
            line_of_business=LineOfBusiness.MEDICARE_ADVANTAGE,
            insurance_plan=self.ma_plan,
            match_keywords=["senior advantage", "medicare"],
        )
        self.deductible_hmo_eoc = self.document(
            "kaiser-norcal-deductible-hmo-eoc-2024",
            title="Kaiser Permanente Northern California 2024 Deductible HMO EOC",
            region="Northern California",
            plan_year=2024,
            match_keywords=["northern california", "deductible hmo"],
        )
        self.kaiser_dental = self.document(
            "kaiser-dental-eoc-2025",
            title="Kaiser Permanente Dental HMO Evidence of Coverage",
            coverage_kind=PublicPlanDocument.COVERAGE_KIND_DENTAL,
            match_keywords=["northern california"],
        )
        self.aetna_eoc = self.document(
            "aetna-eoc-2025",
            title="Aetna Open Access Evidence of Coverage",
            insurance_company=self.aetna,
            state="",
            match_keywords=["open access"],
        )

    def document(self, key, **fields):
        values = dict(
            insurance_company=self.kaiser,
            document_type=EOC,
            coverage_kind=PublicPlanDocument.COVERAGE_KIND_MEDICAL,
            line_of_business=LineOfBusiness.COMMERCIAL,
            state="CA",
            plan_year=2025,
            text=(
                f"[Page 1]\n{fields.get('title', key)}. Medical necessity criteria "
                "apply to an MRI; you may appeal a denial."
            ),
        )
        values.update(fields)
        return PublicPlanDocument.objects.create(document_key=key, **values)

    def denial(self, text, **fields):
        values = dict(
            hashed_email=Denial.get_hashed_email("plan-documents@example.com"),
            denial_text=text,
            your_state="CA",
            denial_date=date(2025, 3, 1),
        )
        values.update(fields)
        return Denial.objects.create(**values)

    def rows(self, denial):
        return list(PlanDocumentMatch.objects.filter(denial=denial).order_by("rank"))

    @staticmethod
    def selected_ids(outcome):
        return [s.candidate.id for s in outcome.selected]

    @staticmethod
    def scored_by_id(outcome):
        return {s.candidate.id: s for s in outcome.scored}


class ResolveCarrierTests(_CorpusMixin, TestCase):
    def setUp(self):
        self.build_corpus()

    def test_the_structured_company_on_the_row_wins(self):
        denial = self.denial(SF_LETTER, insurance_company_obj=self.aetna)
        self.assertEqual(resolve_carrier(denial), self.aetna)

    def test_a_typed_insurer_resolves_by_alt_name_before_the_letter_is_read(self):
        denial = self.denial(SF_LETTER, insurance_company="Aetna Health")
        self.assertEqual(resolve_carrier(denial), self.aetna)

    def test_the_carrier_is_read_from_the_letter_when_nothing_else_names_it(self):
        denial = self.denial(SF_LETTER)
        self.assertEqual(resolve_carrier(denial), self.kaiser)

    def test_a_typed_insurer_with_no_row_that_disagrees_with_the_letter_refuses(self):
        denial = self.denial(SF_LETTER, insurance_company="Acme Health")
        self.assertIsNone(resolve_carrier(denial))

    def test_a_typed_insurer_with_no_row_that_agrees_with_the_letter_is_kept(self):
        # "Permanente" resolves to no row by name, alt name or regex, and
        # does not contradict the carrier the letter names.
        denial = self.denial(SF_LETTER, insurance_company="Permanente")
        self.assertEqual(resolve_carrier(denial), self.kaiser)

    def test_a_letter_naming_two_carriers_resolves_none(self):
        denial = self.denial(
            "Kaiser Permanente and Aetna both reviewed this claim and denied an MRI."
        )
        self.assertIsNone(resolve_carrier(denial))

    def test_a_letter_naming_no_carrier_resolves_none(self):
        denial = self.denial("Your MRI was denied as not medically necessary.")
        self.assertIsNone(resolve_carrier(denial))


class MatchDocumentsTests(_CorpusMixin, TestCase):
    def setUp(self):
        self.build_corpus()

    def test_the_sf_letter_selects_the_norcal_eoc_and_its_sbc(self):
        outcome = match_documents(self.denial(SF_LETTER))
        self.assertEqual(
            self.selected_ids(outcome), [self.norcal_eoc.id, self.norcal_sbc.id]
        )
        self.assertEqual(outcome.rivals, [])
        self.assertEqual(outcome.tie_break_candidates, [])
        by_id = self.scored_by_id(outcome)
        self.assertEqual(by_id[self.norcal_eoc.id].score, 49)
        self.assertEqual(by_id[self.norcal_sbc.id].score, 45)

    def test_the_sf_letter_scores_the_eoc_by_the_documented_signals(self):
        outcome = match_documents(self.denial(SF_LETTER))
        eoc = self.scored_by_id(outcome)[self.norcal_eoc.id]
        self.assertEqual(
            eoc.signals,
            {
                "keyword": 20,
                "keyword_hits": 2,
                "state": 10,
                "plan_year": 10,
                "document_type": 6,
                "coverage_kind": 3,
                "identifying": 1,
            },
        )

    def test_the_other_region_the_dental_document_and_other_carriers_are_out(self):
        outcome = match_documents(self.denial(SF_LETTER))
        by_id = self.scored_by_id(outcome)
        self.assertNotIn(self.socal_eoc.id, by_id)  # E6: its negative regex
        self.assertNotIn(self.kaiser_dental.id, by_id)  # E5: a medical claim
        self.assertNotIn(self.aetna_eoc.id, by_id)  # never a candidate

    def test_agreeing_signals_alone_leave_the_senior_advantage_eoc_unselected(self):
        outcome = match_documents(self.denial(SF_LETTER))
        senior = self.scored_by_id(outcome)[self.senior_advantage_eoc.id]
        self.assertEqual(senior.score, 29)
        self.assertEqual(senior.identifying, 0)
        self.assertNotIn(self.senior_advantage_eoc.id, self.selected_ids(outcome))

    def test_the_outcome_carries_the_text_of_the_quotable_documents_only(self):
        outcome = match_documents(self.denial(SF_LETTER))
        self.assertEqual(set(outcome.texts), {self.norcal_eoc.id, self.norcal_sbc.id})
        self.assertEqual(
            outcome.texts[self.norcal_eoc.id].name, self.norcal_eoc.citation_name()
        )
        self.assertEqual(outcome.texts[self.norcal_eoc.id].text, LONG_EOC_TEXT)

    def test_a_confirmed_medicare_advantage_source_selects_the_senior_advantage_eoc(
        self,
    ):
        denial = self.denial(SENIOR_ADVANTAGE_LETTER)
        PlanSourceRelation.objects.create(denial=denial, plan_source=self.ma_source)
        outcome = match_documents(denial)
        self.assertEqual(self.selected_ids(outcome), [self.senior_advantage_eoc.id])
        self.assertEqual(outcome.selected[0].score, 59)
        self.assertEqual(outcome.selected[0].signals["line_of_business"], 10)
        # The commercial documents are out by the confirmed line of business
        # (E4), not merely outscored.
        self.assertEqual(
            set(self.scored_by_id(outcome)), {self.senior_advantage_eoc.id}
        )

    def test_the_same_plan_on_the_row_adds_forty(self):
        denial = self.denial(SENIOR_ADVANTAGE_LETTER, insurance_plan_obj=self.ma_plan)
        PlanSourceRelation.objects.create(denial=denial, plan_source=self.ma_source)
        outcome = match_documents(denial)
        self.assertEqual(self.selected_ids(outcome), [self.senior_advantage_eoc.id])
        self.assertEqual(outcome.selected[0].signals["same_plan"], 40)
        self.assertEqual(outcome.selected[0].score, 99)

    def test_a_plan_moved_to_another_insurer_no_longer_counts_as_the_same_plan(self):
        # The plan was the carrier's when the document was linked to it, then
        # reassigned in the plan admin: its link says nothing about Kaiser's
        # members any more.
        self.ma_plan.insurance_company = self.aetna
        self.ma_plan.save(update_fields=["insurance_company"])
        denial = self.denial(SENIOR_ADVANTAGE_LETTER, insurance_plan_obj=self.ma_plan)
        PlanSourceRelation.objects.create(denial=denial, plan_source=self.ma_source)
        outcome = match_documents(denial, company=self.kaiser)
        scored = self.scored_by_id(outcome)
        self.assertNotIn("same_plan", scored[self.senior_advantage_eoc.id].signals)

    def test_moving_a_plan_to_another_insurer_changes_the_inputs_hash(self):
        denial = self.denial(SENIOR_ADVANTAGE_LETTER, insurance_plan_obj=self.ma_plan)
        before = current_hashes(denial)[0]
        self.ma_plan.insurance_company = self.aetna
        self.ma_plan.save(update_fields=["insurance_company"])
        self.assertNotEqual(current_hashes(denial)[0], before)

    def test_a_dental_document_found_only_by_the_letters_words_goes_to_the_model(self):
        # No medical document is in the running, so there is no rival; the
        # dental document still is not selected by the rules alone.
        PublicPlanDocument.objects.exclude(id=self.kaiser_dental.id).update(
            is_active=False
        )
        outcome = match_documents(self.denial(DENTAL_LETTER))
        self.assertEqual(outcome.selected, [])
        self.assertEqual(
            [s.candidate.id for s in outcome.tie_break_candidates],
            [self.kaiser_dental.id],
        )

    def test_a_letter_naming_both_regions_selects_nothing_and_needs_no_tie_break(self):
        outcome = match_documents(self.denial(BOTH_REGIONS_LETTER))
        self.assertEqual(outcome.selected, [])
        self.assertEqual(outcome.rivals, [])
        self.assertEqual(outcome.tie_break_candidates, [])
        by_id = self.scored_by_id(outcome)
        for doc in (self.norcal_eoc, self.norcal_sbc, self.socal_eoc):
            self.assertNotIn(doc.id, by_id)

    def test_a_generic_letter_scores_the_documents_but_selects_nothing(self):
        outcome = match_documents(self.denial(GENERIC_LETTER))
        self.assertEqual(outcome.selected, [])
        # Scored, not excluded: the negative regex did not fire on
        # "medically" or "Medical Center".
        eoc = self.scored_by_id(outcome)[self.norcal_eoc.id]
        self.assertEqual(eoc.score, 29)
        self.assertEqual(eoc.identifying, 0)
        self.assertTrue(all(s.identifying == 0 for s in outcome.scored))

    def test_the_tie_break_letter_leaves_rivals_for_the_model(self):
        outcome = match_documents(self.denial(TIE_BREAK_LETTER))
        self.assertEqual(outcome.selected, [])
        self.assertEqual(
            [s.candidate.id for s in outcome.rivals],
            [self.norcal_eoc.id, self.norcal_sbc.id],
        )
        self.assertEqual(
            [s.candidate.id for s in outcome.tie_break_candidates],
            [self.deductible_hmo_eoc.id, self.norcal_eoc.id, self.norcal_sbc.id],
        )
        by_id = self.scored_by_id(outcome)
        self.assertEqual(by_id[self.deductible_hmo_eoc.id].score, 44)
        self.assertEqual(by_id[self.norcal_eoc.id].score, 39)
        self.assertEqual(by_id[self.norcal_sbc.id].score, 35)
        self.assertEqual(
            set(outcome.texts),
            {self.deductible_hmo_eoc.id, self.norcal_eoc.id, self.norcal_sbc.id},
        )

    def test_a_flagged_dental_claim_matches_only_the_dental_document(self):
        outcome = match_documents(self.denial(DENTAL_LETTER, dental_claim=True))
        self.assertEqual(set(self.scored_by_id(outcome)), {self.kaiser_dental.id})
        self.assertEqual(self.selected_ids(outcome), [self.kaiser_dental.id])
        self.assertEqual(outcome.selected[0].score, 39)

    def test_a_letter_that_only_mentions_dental_care_is_a_tie_for_the_model(self):
        # The letter may be a dental claim, or a medical one about dental
        # work: the medical documents stay in, the dental one is a rival
        # within the margin, and nothing is selected without the tie-break.
        outcome = match_documents(self.denial(DENTAL_LETTER))
        scored = self.scored_by_id(outcome)
        self.assertIn(self.kaiser_dental.id, scored)
        self.assertIn(self.norcal_eoc.id, scored)
        self.assertEqual(outcome.selected, [])
        top = max(outcome.scored, key=lambda s: s.score)
        kinds = {top.candidate.coverage_kind}
        kinds |= {s.candidate.coverage_kind for s in outcome.rivals}
        self.assertEqual(kinds, {"dental", "medical"})

    def test_a_dental_claim_flagged_on_the_row_counts_without_the_word(self):
        denial = self.denial(
            "Kaiser Permanente Northern California denied a crown.", dental_claim=True
        )
        outcome = match_documents(denial)
        self.assertEqual(set(self.scored_by_id(outcome)), {self.kaiser_dental.id})

    def test_a_medical_claim_never_gets_the_dental_document(self):
        outcome = match_documents(
            self.denial("Kaiser Permanente Northern California denied an MRI.")
        )
        self.assertNotIn(self.kaiser_dental.id, self.scored_by_id(outcome))
        self.assertEqual(
            self.selected_ids(outcome), [self.norcal_eoc.id, self.norcal_sbc.id]
        )

    def test_another_state_excludes_every_california_document(self):
        outcome = match_documents(self.denial(SF_LETTER, your_state="NY"))
        self.assertEqual(outcome.scored, [])
        self.assertEqual(outcome.selected, [])

    def test_a_state_name_is_read_like_its_code(self):
        outcome = match_documents(self.denial(SF_LETTER, your_state="California"))
        self.assertEqual(
            self.selected_ids(outcome), [self.norcal_eoc.id, self.norcal_sbc.id]
        )

    def test_no_carrier_means_no_outcome(self):
        self.assertIsNone(
            match_documents(self.denial(SF_LETTER, insurance_company="Acme Health"))
        )
        self.assertIsNone(match_documents(self.denial("Your MRI was denied.")))

    def test_an_explicit_company_skips_the_resolver(self):
        denial = self.denial(
            "Your MRI in San Francisco was denied. Northern California Region."
        )
        self.assertIsNone(match_documents(denial))
        outcome = match_documents(denial, company=self.kaiser)
        self.assertEqual(
            self.selected_ids(outcome), [self.norcal_eoc.id, self.norcal_sbc.id]
        )

    def test_an_inactive_document_is_never_a_candidate(self):
        self.norcal_sbc.is_active = False
        self.norcal_sbc.save()
        outcome = match_documents(self.denial(SF_LETTER))
        self.assertEqual(self.selected_ids(outcome), [self.norcal_eoc.id])
        self.assertNotIn(self.norcal_sbc.id, self.scored_by_id(outcome))


class CurrentHashesTests(_CorpusMixin, TestCase):
    def setUp(self):
        self.build_corpus()
        self.denial_row = self.denial(SF_LETTER)

    def test_hashes_are_stable_for_an_unchanged_denial(self):
        self.assertEqual(
            current_hashes(self.denial_row), current_hashes(self.denial_row)
        )

    def test_no_carrier_means_no_hashes(self):
        self.assertIsNone(
            current_hashes(self.denial(SF_LETTER, insurance_company="Acme Health"))
        )

    def test_the_inputs_hash_changes_with_the_carrier(self):
        before = current_hashes(self.denial_row)[0]
        self.denial_row.insurance_company_obj = self.aetna
        self.assertNotEqual(current_hashes(self.denial_row)[0], before)

    def test_the_inputs_hash_changes_with_the_state(self):
        before = current_hashes(self.denial_row)[0]
        self.denial_row.your_state = "NV"
        self.assertNotEqual(current_hashes(self.denial_row)[0], before)

    def test_the_inputs_hash_changes_with_the_letter(self):
        before = current_hashes(self.denial_row)[0]
        self.denial_row.denial_text = GENERIC_LETTER
        self.assertNotEqual(current_hashes(self.denial_row)[0], before)

    def test_the_inputs_hash_changes_when_a_document_of_the_carrier_is_deactivated(
        self,
    ):
        before = current_hashes(self.denial_row)[0]
        self.norcal_sbc.is_active = False
        self.norcal_sbc.save()
        self.assertNotEqual(current_hashes(self.denial_row)[0], before)

    def test_the_inputs_hash_changes_when_a_document_of_the_carrier_is_re_imported(
        self,
    ):
        before = current_hashes(self.denial_row)[0]
        self.norcal_sbc.save()  # auto_now moves updated_at: the corpus stamp
        self.assertNotEqual(current_hashes(self.denial_row)[0], before)

    def test_a_document_of_another_carrier_does_not_move_the_hash(self):
        before = current_hashes(self.denial_row)[0]
        self.aetna_eoc.save()
        self.assertEqual(current_hashes(self.denial_row)[0], before)

    def test_the_plan_id_text_is_ignored_while_no_candidate_has_prefixes(self):
        before = current_hashes(self.denial_row)
        self.denial_row.plan_id = "GRP-12345"
        self.assertEqual(current_hashes(self.denial_row), before)

    def test_the_plan_id_text_counts_once_a_candidate_has_prefixes(self):
        self.norcal_eoc.plan_id_prefixes = ["GRP"]
        self.norcal_eoc.save()
        before = current_hashes(self.denial_row)[0]
        self.denial_row.plan_id = "GRP-12345"
        self.assertNotEqual(current_hashes(self.denial_row)[0], before)

    def test_a_keyword_phrase_arriving_in_plan_context_moves_the_hash(self):
        # The generic letter names no region. The phrase reaches the blob
        # through plan_context and turns the corpus's keyword answers (and
        # the SoCal document's negative regex).
        denial = self.denial(GENERIC_LETTER)
        before = current_hashes(denial)[0]
        denial.plan_context = "Northern California Region"
        self.assertNotEqual(current_hashes(denial)[0], before)

    def test_plan_context_that_turns_no_answer_leaves_the_hash_alone(self):
        denial = self.denial(GENERIC_LETTER)
        before = current_hashes(denial)[0]
        denial.plan_context = "An HMO plan through my employer."
        self.assertEqual(current_hashes(denial)[0], before)

    def test_a_denial_date_arriving_moves_the_hash_while_a_candidate_has_a_year(
        self,
    ):
        denial = self.denial(SF_LETTER, denial_date=None)
        before = current_hashes(denial)[0]
        denial.denial_date = date(2025, 3, 1)
        self.assertNotEqual(current_hashes(denial)[0], before)

    def test_the_denial_date_is_ignored_while_no_candidate_has_a_year(self):
        PublicPlanDocument.objects.filter(insurance_company=self.kaiser).update(
            plan_year=None
        )
        denial = self.denial(SF_LETTER, denial_date=None)
        before = current_hashes(denial)[0]
        denial.denial_date = date(2025, 3, 1)
        self.assertEqual(current_hashes(denial)[0], before)

    def test_the_procedure_changes_the_relevance_key_and_not_the_inputs_hash(self):
        inputs_before, key_before = current_hashes(self.denial_row)
        self.denial_row.procedure = "MRI of the lumbar spine"
        inputs_after, key_after = current_hashes(self.denial_row)
        self.assertEqual(inputs_after, inputs_before)
        self.assertNotEqual(key_after, key_before)


class ReaderTests(_CorpusMixin, TestCase):
    """``get_plan_document_context_for_denial``: what it reads and what it
    refuses to."""

    def setUp(self):
        self.build_corpus()
        self.denial_row = self.denial(SF_LETTER)
        self.passage = (
            f"[{self.norcal_eoc.citation_name()}, page 4]\nUnder {DOCUMENT_SENTINEL} "
            "an MRI ordered by a plan physician is covered."
        )

    def current_rows(
        self, *, inputs_hash=None, documents=None, relevant_text=None, relevance_key=None
    ):
        """Selected rows for the denial, rank 1 carrying the passages and
        the summary, with the hashes the denial has now unless given."""
        hashes = current_hashes(self.denial_row)
        inputs_hash = inputs_hash or hashes[0]
        relevance_key = relevance_key or hashes[1]
        documents = documents or [self.norcal_eoc, self.norcal_sbc]
        if relevant_text is None:
            relevant_text = self.passage
        rows = []
        for rank, doc in enumerate(documents, start=1):
            rows.append(
                PlanDocumentMatch.objects.create(
                    denial=self.denial_row,
                    document=doc,
                    company=self.kaiser,
                    score=50 - rank,
                    signals={"keyword": 20, "identifying": 1},
                    rank=rank,
                    selected=True,
                    inputs_hash=inputs_hash,
                    relevant_text=relevant_text if rank == 1 else "",
                    summary=SUMMARY if rank == 1 and relevant_text else "",
                    relevance_key=relevance_key if rank == 1 else "",
                )
            )
        return rows

    def test_nothing_without_rows(self):
        self.assertEqual(get_plan_document_context_for_denial(self.denial_row), "")

    def test_the_section_is_rendered_for_current_selected_rows(self):
        self.current_rows()
        section = get_plan_document_context_for_denial(self.denial_row)
        self.assertTrue(section.startswith(PLAN_DOCUMENT_GUARD))
        self.assertIn(self.norcal_eoc.citation_name(), section)
        self.assertIn(self.norcal_sbc.citation_name(), section)
        self.assertIn(self.passage, section)
        self.assertIn(SUMMARY, section)
        self.assertTrue(section.endswith(PLAN_DOCUMENT_END + "\n"))

    def test_the_reader_does_not_consult_the_flag(self):
        # make_appeals gates on the flag; the reader reads what is there.
        self.current_rows()
        with override_settings(PLAN_DOCUMENT_MATCHING_ENABLED=False):
            self.assertIn(
                PLAN_DOCUMENT_GUARD,
                get_plan_document_context_for_denial(self.denial_row),
            )
        with override_settings(PLAN_DOCUMENT_MATCHING_ENABLED=True):
            self.assertIn(
                PLAN_DOCUMENT_GUARD,
                get_plan_document_context_for_denial(self.denial_row),
            )
            PlanDocumentMatch.objects.filter(denial=self.denial_row).delete()
            self.assertEqual(get_plan_document_context_for_denial(self.denial_row), "")

    def test_passages_built_for_another_procedure_are_not_quoted(self):
        # The selection still holds (same inputs hash), but the passages
        # were chosen for an earlier procedure and their rebuild has not
        # run: the section waits for it rather than argue the old treatment.
        self.current_rows()
        self.assertIn(self.passage, get_plan_document_context_for_denial(self.denial_row))
        self.denial_row.procedure = "Knee arthroscopy"
        self.denial_row.save(update_fields=["procedure"])
        self.assertEqual(get_plan_document_context_for_denial(self.denial_row), "")

    def test_rows_with_a_stale_hash_are_ignored(self):
        self.current_rows(inputs_hash="0" * 64)
        self.assertEqual(get_plan_document_context_for_denial(self.denial_row), "")

    def test_rows_go_stale_when_the_letter_changes(self):
        self.current_rows()
        self.denial_row.denial_text = GENERIC_LETTER
        self.denial_row.save(update_fields=["denial_text"])
        self.assertEqual(get_plan_document_context_for_denial(self.denial_row), "")

    def test_rows_go_stale_when_the_carrier_is_corrected(self):
        self.current_rows()
        self.denial_row.insurance_company_obj = self.aetna
        self.denial_row.save(update_fields=["insurance_company_obj"])
        self.assertEqual(get_plan_document_context_for_denial(self.denial_row), "")

    def test_a_selected_document_since_deactivated_is_left_out(self):
        # Deactivated before the rows are hashed, so the hash is current and
        # only the document's own filter can keep it out.
        self.norcal_sbc.is_active = False
        self.norcal_sbc.save()
        self.current_rows(documents=[self.norcal_eoc, self.norcal_sbc])
        section = get_plan_document_context_for_denial(self.denial_row)
        self.assertIn(self.norcal_eoc.citation_name(), section)
        self.assertNotIn(self.norcal_sbc.citation_name(), section)

    def test_nothing_when_the_rank_one_document_was_deactivated(self):
        self.norcal_eoc.is_active = False
        self.norcal_eoc.save()
        self.current_rows(documents=[self.norcal_eoc, self.norcal_sbc])
        self.assertEqual(get_plan_document_context_for_denial(self.denial_row), "")

    def test_an_uploaded_plan_document_silences_the_section(self):
        self.current_rows()
        PlanDocuments.objects.create(denial=self.denial_row)
        self.assertEqual(get_plan_document_context_for_denial(self.denial_row), "")

    def test_a_selection_without_passages_yet_gives_no_section(self):
        self.current_rows(relevant_text="")
        self.assertEqual(get_plan_document_context_for_denial(self.denial_row), "")

    def test_unselected_rows_are_not_read(self):
        self.current_rows()
        PlanDocumentMatch.objects.filter(denial=self.denial_row).update(selected=False)
        self.assertEqual(get_plan_document_context_for_denial(self.denial_row), "")

    def test_an_unsaved_denial_or_none_gets_nothing(self):
        self.assertEqual(
            get_plan_document_context_for_denial(Denial(denial_text=SF_LETTER)), ""
        )
        self.assertEqual(get_plan_document_context_for_denial(None), "")


class FormatPlanDocumentContextTests(_CorpusMixin, TestCase):
    def setUp(self):
        self.build_corpus()
        self.denial_row = self.denial(SF_LETTER)

    def row(self, doc, rank, **fields):
        return PlanDocumentMatch(
            denial=self.denial_row,
            document=doc,
            company=self.kaiser,
            score=49,
            signals={},
            rank=rank,
            selected=True,
            inputs_hash="x",
            **fields,
        )

    def test_the_guard_the_citations_the_passages_then_the_summary_and_the_end_marker(
        self,
    ):
        passages = (
            f"[{self.norcal_eoc.citation_name()}, page 4]\nUnder {DOCUMENT_SENTINEL} "
            "an MRI ordered by a plan physician is covered."
        )
        rows = [
            self.row(self.norcal_eoc, 1, relevant_text=passages, summary=SUMMARY),
            self.row(self.norcal_sbc, 2),
        ]
        section = format_plan_document_context(rows)
        self.assertTrue(section.startswith(PLAN_DOCUMENT_GUARD + "\n"))
        self.assertIn(
            f"- {self.norcal_eoc.citation_name()} -- Evidence of Coverage, CA, "
            f"Northern California, {self.norcal_eoc.source_url}",
            section,
        )
        self.assertIn(
            f"- {self.norcal_sbc.citation_name()} -- Summary of Benefits and "
            f"Coverage, CA, Northern California, {self.norcal_sbc.source_url}",
            section,
        )
        self.assertLess(
            section.index("Relevant passages"),
            section.index("Summary of these passages"),
        )
        self.assertLess(section.index(passages), section.index(SUMMARY))
        self.assertTrue(section.endswith("\n" + PLAN_DOCUMENT_END + "\n"))
        self.assertLessEqual(len(section), CONTEXT_MAX_CHARS)

    def test_the_citation_carries_the_plan_year_only_when_the_title_lacks_it(self):
        self.assertEqual(
            self.norcal_eoc.citation_name(), self.norcal_eoc.title + " (2025)"
        )
        self.assertEqual(
            self.deductible_hmo_eoc.citation_name(), self.deductible_hmo_eoc.title
        )

    def test_a_national_document_without_a_region_or_url_still_reads(self):
        row = self.row(self.aetna_eoc, 1, relevant_text="[Aetna, page 1]\nCovered.")
        section = format_plan_document_context([row])
        self.assertIn(
            f"- {self.aetna_eoc.citation_name()} -- Evidence of Coverage, national\n",
            section,
        )

    def test_a_row_without_a_summary_has_no_summary_heading(self):
        row = self.row(
            self.norcal_eoc, 1, relevant_text="[x, page 1]\nCovered.", summary=""
        )
        section = format_plan_document_context([row])
        self.assertIn("Relevant passages", section)
        self.assertNotIn("Summary of these passages", section)

    def test_the_section_is_capped_at_context_max_chars(self):
        long_passages = "\n".join(
            f"[{self.norcal_eoc.citation_name()}, page {n}]\n"
            + "Covered when medically necessary. " * 40
            for n in range(1, 12)
        )
        self.assertGreater(len(long_passages), PASSAGE_BUDGET_CHARS * 2)
        row = self.row(
            self.norcal_eoc, 1, relevant_text=long_passages, summary="Summary. " * 300
        )
        section = format_plan_document_context([row])
        self.assertLessEqual(len(section), CONTEXT_MAX_CHARS)
        self.assertTrue(section.startswith(PLAN_DOCUMENT_GUARD))
        self.assertTrue(section.endswith(PLAN_DOCUMENT_END + "\n"))

    def test_no_rows_no_section(self):
        self.assertEqual(format_plan_document_context([]), "")


class InvalidationTests(_CorpusMixin, TestCase):
    def setUp(self):
        self.build_corpus()

    def test_replacing_the_letter_deletes_that_denials_match_rows_only(self):
        denial = self.denial(SF_LETTER)
        other = self.denial(SF_LETTER)
        for row_denial in (denial, other):
            PlanDocumentMatch.objects.create(
                denial=row_denial,
                document=self.norcal_eoc,
                company=self.kaiser,
                score=49,
                signals={},
                rank=1,
                selected=True,
                inputs_hash="x",
            )
        DenialCreatorHelper._invalidate_denial_text_artifacts(denial)
        self.assertFalse(PlanDocumentMatch.objects.filter(denial=denial).exists())
        self.assertTrue(PlanDocumentMatch.objects.filter(denial=other).exists())


class BuildRelevanceTests(TestCase):
    """``build_relevance`` touches no table: the denial is read for its
    letter, procedure and diagnosis only."""

    def setUp(self):
        self.denial = Denial(denial_text=SF_LETTER)
        self.short = [PlanDocumentText(name="SBC", text=SHORT_TEXT)]
        self.long = [PlanDocumentText(name="EOC", text=LONG_EOC_TEXT), self.short[0]]

    def test_documents_that_fit_the_budget_go_whole(self):
        with _no_models() as models:
            relevant, summary = asyncio.run(build_relevance(self.denial, self.short))
        self.assertEqual(relevant, MLPlanDocHelper.whole_text(self.short))
        self.assertEqual(summary, SUMMARY)
        models.search.assert_not_awaited()
        models.summarize.assert_awaited_once()

    def test_longer_documents_are_searched_with_the_models_terms(self):
        with _no_models() as models:
            relevant, summary = asyncio.run(build_relevance(self.denial, self.long))
        models.search.assert_awaited_once()
        self.assertEqual(models.search.await_args.args[0], SF_LETTER)
        self.assertEqual(models.search.await_args.kwargs["documents"], self.long)
        self.assertLess(len(relevant), len(LONG_EOC_TEXT))
        # Within the budget, give or take the labels and separators.
        self.assertLessEqual(len(relevant), PASSAGE_BUDGET_CHARS + 400)
        self.assertIn("[EOC, page", relevant)
        self.assertEqual(summary, SUMMARY)
        # The summary is written from exactly the text that will be quoted.
        self.assertEqual(models.summarize.await_args.args[1], relevant)

    def test_without_a_model_the_fallback_terms_choose_and_there_is_no_summary(self):
        with _no_models() as models:
            relevant, summary = asyncio.run(
                build_relevance(self.denial, self.long, use_model=False)
            )
        models.search.assert_not_awaited()
        models.summarize.assert_not_awaited()
        self.assertIsNone(summary)
        self.assertIn("[EOC, page", relevant)

    def test_no_documents_no_relevance(self):
        with _no_models() as models:
            self.assertEqual(asyncio.run(build_relevance(self.denial, [])), ("", None))
        models.summarize.assert_not_awaited()


class RefreshMatchTests(_CorpusMixin, TransactionTestCase):
    """``arefresh_match``: the rows it stores, when it reuses them, the
    tie-break and the relevance text."""

    def setUp(self):
        self.build_corpus()

    def refresh(self, denial, *, deadline=None, **mocks):
        with _no_models(**mocks) as models:
            found = asyncio.run(arefresh_match(denial, deadline=deadline))
        return found, models

    def test_a_refresh_stores_every_surviving_candidate_ranked_with_its_signals(self):
        denial = self.denial(SF_LETTER)
        found, _ = self.refresh(denial)
        self.assertTrue(found)
        rows = self.rows(denial)
        self.assertEqual(
            [r.document_id for r in rows],
            [
                self.norcal_eoc.id,
                self.norcal_sbc.id,
                self.deductible_hmo_eoc.id,
                self.senior_advantage_eoc.id,
            ],
        )
        self.assertEqual([r.rank for r in rows], [1, 2, 3, 4])
        self.assertEqual([r.selected for r in rows], [True, True, False, False])
        self.assertEqual([r.score for r in rows], [49, 45, 34, 29])
        self.assertEqual({r.method for r in rows}, {PlanDocumentMatch.METHOD_RULES})
        self.assertEqual({r.company_id for r in rows}, {self.kaiser.id})
        self.assertEqual({r.inputs_hash for r in rows}, {current_hashes(denial)[0]})
        self.assertEqual(rows[0].signals["keyword_hits"], 2)
        self.assertEqual(rows[0].signals["identifying"], 1)

    def test_the_passages_and_summary_land_on_the_rank_one_selected_row_only(self):
        denial = self.denial(SF_LETTER)
        found, models = self.refresh(denial)
        self.assertTrue(found)
        rows = self.rows(denial)
        first = rows[0]
        self.assertIn(f"[{self.norcal_eoc.citation_name()}, page", first.relevant_text)
        self.assertIn("medical necessity", first.relevant_text)
        self.assertLess(len(first.relevant_text), len(LONG_EOC_TEXT))
        # Within the budget, give or take the labels and separators.
        self.assertLessEqual(len(first.relevant_text), PASSAGE_BUDGET_CHARS + 400)
        self.assertEqual(first.summary, SUMMARY)
        self.assertEqual(first.relevance_key, current_hashes(denial)[1])
        for row in rows[1:]:
            self.assertEqual(
                (row.relevant_text, row.summary, row.relevance_key), ("", "", "")
            )
        models.search.assert_awaited_once()
        self.assertEqual(models.search.await_args.args[0], SF_LETTER)
        self.assertEqual(
            [d.name for d in models.search.await_args.kwargs["documents"]],
            [self.norcal_eoc.citation_name(), self.norcal_sbc.citation_name()],
        )
        models.summarize.assert_awaited_once()
        models.infer.assert_not_awaited()

    def test_rows_are_reused_while_the_inputs_hash_holds(self):
        denial = self.denial(SF_LETTER)
        self.refresh(denial)
        ids_before = [r.id for r in self.rows(denial)]
        found, models = self.refresh(denial)
        self.assertTrue(found)
        models.search.assert_not_awaited()
        models.summarize.assert_not_awaited()
        models.infer.assert_not_awaited()
        self.assertEqual([r.id for r in self.rows(denial)], ids_before)

    def test_a_changed_procedure_rebuilds_the_passages_and_keeps_the_selection(self):
        denial = self.denial(SF_LETTER)
        self.refresh(denial)
        before = self.rows(denial)
        denial.procedure = "MRI of the lumbar spine"
        denial.save(update_fields=["procedure"])
        found, models = self.refresh(denial, summary="Summary B: the lumbar MRI.")
        self.assertTrue(found)
        after = self.rows(denial)
        self.assertEqual([r.id for r in after], [r.id for r in before])
        self.assertEqual([r.selected for r in after], [r.selected for r in before])
        self.assertEqual(after[0].summary, "Summary B: the lumbar MRI.")
        self.assertNotEqual(after[0].relevance_key, before[0].relevance_key)
        self.assertEqual(after[0].relevance_key, current_hashes(denial)[1])
        models.search.assert_awaited_once()
        self.assertEqual(
            models.search.await_args.kwargs["procedure"], "MRI of the lumbar spine"
        )
        models.infer.assert_not_awaited()

    def test_the_model_breaks_a_tie_among_numbered_candidates_with_internal_models(
        self,
    ):
        denial = self.denial(TIE_BREAK_LETTER)
        found, models = self.refresh(denial, tie_break_answer="1")
        self.assertTrue(found)
        rows = self.rows(denial)
        self.assertEqual(
            [r.document_id for r in rows],
            [
                self.deductible_hmo_eoc.id,
                self.norcal_eoc.id,
                self.norcal_sbc.id,
                self.senior_advantage_eoc.id,
            ],
        )
        self.assertEqual([r.selected for r in rows], [True, False, False, False])
        self.assertEqual({r.method for r in rows}, {PlanDocumentMatch.METHOD_MODEL})
        models.infer.assert_awaited_once()
        kwargs = models.infer.await_args.kwargs
        self.assertNotIn("models", kwargs)
        prompt = kwargs["prompt"]
        self.assertIn(f"1. {self.deductible_hmo_eoc.title}", prompt)
        self.assertIn(f"2. {self.norcal_eoc.title}", prompt)
        self.assertIn(f"3. {self.norcal_sbc.title}", prompt)
        self.assertIn(TIE_BREAK_LETTER, prompt)
        self.assertTrue(kwargs["validator"]("3"))
        self.assertFalse(kwargs["validator"]("4"))
        # The relevance text is built for the pick.
        self.assertEqual(rows[0].summary, SUMMARY)
        self.assertIn(self.deductible_hmo_eoc.citation_name(), rows[0].relevant_text)

    def test_the_picked_document_brings_its_companions(self):
        denial = self.denial(TIE_BREAK_LETTER)
        found, _ = self.refresh(denial, tie_break_answer="2")
        self.assertTrue(found)
        rows = self.rows(denial)
        self.assertEqual(
            [r.document_id for r in rows if r.selected],
            [self.norcal_eoc.id, self.norcal_sbc.id],
        )
        # The first selected row, not the rank-1 row, carries the text.
        self.assertEqual(rows[0].document_id, self.deductible_hmo_eoc.id)
        self.assertEqual(rows[0].relevant_text, "")
        self.assertEqual(rows[1].document_id, self.norcal_eoc.id)
        self.assertEqual(rows[1].summary, SUMMARY)
        section = get_plan_document_context_for_denial(denial)
        self.assertIn(self.norcal_eoc.citation_name(), section)
        self.assertNotIn(self.deductible_hmo_eoc.citation_name(), section)

    def test_a_pick_outranked_by_its_companion_puts_the_text_on_the_lower_rank(
        self,
    ):
        # "3" is the SBC. Its companion, the NorCal EOC, outranks it, so the
        # selection is stored in rank order and the passages land on the
        # EOC's row: the lowest-rank selected row, the one the reader takes
        # first.
        denial = self.denial(TIE_BREAK_LETTER)
        found, models = self.refresh(denial, tie_break_answer="3")
        self.assertTrue(found)
        models.infer.assert_awaited_once()
        rows = self.rows(denial)
        self.assertEqual(
            [(r.document_id, r.rank, r.selected) for r in rows],
            [
                (self.deductible_hmo_eoc.id, 1, False),
                (self.norcal_eoc.id, 2, True),
                (self.norcal_sbc.id, 3, True),
                (self.senior_advantage_eoc.id, 4, False),
            ],
        )
        self.assertEqual({r.method for r in rows}, {PlanDocumentMatch.METHOD_MODEL})
        self.assertIn(
            f"[{self.norcal_eoc.citation_name()}, page", rows[1].relevant_text
        )
        self.assertEqual(rows[1].summary, SUMMARY)
        self.assertEqual(rows[1].relevance_key, current_hashes(denial)[1])
        for row in (rows[0], rows[2], rows[3]):
            self.assertEqual((row.relevant_text, row.summary), ("", ""))
        # The passages were built over the pick and its companion, in rank
        # order.
        self.assertEqual(
            [d.name for d in models.search.await_args.kwargs["documents"]],
            [self.norcal_eoc.citation_name(), self.norcal_sbc.citation_name()],
        )
        section = get_plan_document_context_for_denial(denial)
        self.assertTrue(section.startswith(PLAN_DOCUMENT_GUARD))
        self.assertIn(self.norcal_eoc.citation_name(), section)
        self.assertIn(self.norcal_sbc.citation_name(), section)
        self.assertNotIn(self.deductible_hmo_eoc.citation_name(), section)
        self.assertIn(SUMMARY, section)

    def test_a_declined_tie_break_stores_the_rows_and_selects_nothing(self):
        denial = self.denial(TIE_BREAK_LETTER)
        found, models = self.refresh(denial, tie_break_answer="0")
        self.assertFalse(found)
        rows = self.rows(denial)
        self.assertEqual(len(rows), 4)
        self.assertFalse(any(r.selected for r in rows))
        self.assertEqual({r.method for r in rows}, {PlanDocumentMatch.METHOD_RULES})
        models.infer.assert_awaited_once()
        models.search.assert_not_awaited()
        models.summarize.assert_not_awaited()
        self.assertEqual(get_plan_document_context_for_denial(denial), "")

    def test_no_answer_from_the_model_stores_nothing(self):
        denial = self.denial(TIE_BREAK_LETTER)
        found, models = self.refresh(denial, tie_break_answer=None)
        self.assertFalse(found)
        models.infer.assert_awaited_once()
        models.search.assert_not_awaited()
        # Not a verdict: no rows, so nothing reads as "asked and declined".
        self.assertEqual(self.rows(denial), [])
        self.assertEqual(get_plan_document_context_for_denial(denial), "")

    def test_an_unanswered_tie_break_is_asked_again_on_the_next_refresh(self):
        denial = self.denial(TIE_BREAK_LETTER)
        _, first = self.refresh(denial, tie_break_answer=None)
        found, second = self.refresh(denial, tie_break_answer=None)
        self.assertFalse(found)
        first.infer.assert_awaited_once()
        second.infer.assert_awaited_once()
        self.assertEqual(self.rows(denial), [])

    def test_a_declined_tie_break_is_not_asked_again_while_the_hash_holds(self):
        denial = self.denial(TIE_BREAK_LETTER)
        self.refresh(denial, tie_break_answer="0")
        found, models = self.refresh(denial, tie_break_answer="1")
        self.assertFalse(found)
        models.infer.assert_not_awaited()

    def test_the_selection_survives_a_relevance_failure(self):
        denial = self.denial(SF_LETTER)
        found, _ = self.refresh(
            denial,
            summarize=AsyncMock(side_effect=RuntimeError("summary backend down")),
        )
        self.assertFalse(found)
        rows = self.rows(denial)
        self.assertEqual(
            [r.document_id for r in rows if r.selected],
            [self.norcal_eoc.id, self.norcal_sbc.id],
        )
        self.assertEqual(
            (rows[0].relevant_text, rows[0].summary, rows[0].relevance_key),
            ("", "", ""),
        )
        # Nothing to quote yet, so no section.
        self.assertEqual(get_plan_document_context_for_denial(denial), "")

    def test_the_next_refresh_fills_the_passages_after_a_relevance_failure(self):
        denial = self.denial(SF_LETTER)
        self.refresh(denial, summarize=AsyncMock(side_effect=RuntimeError("down")))
        ids = [r.id for r in self.rows(denial)]
        found, models = self.refresh(denial)
        self.assertTrue(found)
        rows = self.rows(denial)
        self.assertEqual([r.id for r in rows], ids)
        self.assertEqual(rows[0].summary, SUMMARY)
        self.assertNotEqual(rows[0].relevant_text, "")
        models.infer.assert_not_awaited()
        self.assertIn(PLAN_DOCUMENT_GUARD, get_plan_document_context_for_denial(denial))

    def test_no_carrier_stores_nothing(self):
        denial = self.denial(SF_LETTER, insurance_company="Acme Health")
        found, models = self.refresh(denial)
        self.assertFalse(found)
        self.assertEqual(self.rows(denial), [])
        models.infer.assert_not_awaited()

    def test_the_selected_rows_are_stored_whatever_max_stored_allows(self):
        denial = self.denial(SF_LETTER)
        with patch.object(plan_document_matcher, "MAX_STORED", 2):
            found, _ = self.refresh(denial)
        self.assertTrue(found)
        rows = self.rows(denial)
        self.assertEqual(
            [(r.document_id, r.rank, r.selected, r.score) for r in rows],
            [(self.norcal_eoc.id, 1, True, 49), (self.norcal_sbc.id, 2, True, 45)],
        )
        self.assertIn(PLAN_DOCUMENT_GUARD, get_plan_document_context_for_denial(denial))

    def test_the_tie_break_candidates_are_stored_beyond_max_stored(self):
        # The pick and both rivals are kept, re-ranked by score; the best of
        # the rest (the Senior Advantage EOC) no longer fits.
        denial = self.denial(TIE_BREAK_LETTER)
        with patch.object(plan_document_matcher, "MAX_STORED", 2):
            found, _ = self.refresh(denial, tie_break_answer="3")
        self.assertTrue(found)
        rows = self.rows(denial)
        self.assertEqual(
            [(r.document_id, r.rank, r.selected, r.score) for r in rows],
            [
                (self.deductible_hmo_eoc.id, 1, False, 44),
                (self.norcal_eoc.id, 2, True, 39),
                (self.norcal_sbc.id, 3, True, 35),
            ],
        )

    def test_the_best_of_the_rest_fill_up_to_max_stored(self):
        denial = self.denial(SF_LETTER)
        with patch.object(plan_document_matcher, "MAX_STORED", 3):
            self.refresh(denial)
        self.assertEqual(
            [r.document_id for r in self.rows(denial)],
            [self.norcal_eoc.id, self.norcal_sbc.id, self.deductible_hmo_eoc.id],
        )

    def test_a_short_budget_builds_the_passages_without_a_model(self):
        denial = self.denial(SF_LETTER)
        found, models = self.refresh(denial, deadline=time.monotonic() + 5)
        self.assertTrue(found)
        models.search.assert_not_awaited()
        models.summarize.assert_not_awaited()
        models.infer.assert_not_awaited()
        rows = self.rows(denial)
        self.assertEqual(
            [r.document_id for r in rows if r.selected],
            [self.norcal_eoc.id, self.norcal_sbc.id],
        )
        first = rows[0]
        # The fallback terms chose the passages; no model, so no summary.
        self.assertNotEqual(first.relevant_text, "")
        self.assertIn(f"[{self.norcal_eoc.citation_name()}, page", first.relevant_text)
        self.assertEqual(first.summary, "")
        self.assertEqual(first.relevance_key, current_hashes(denial)[1])
        section = get_plan_document_context_for_denial(denial)
        self.assertTrue(section.startswith(PLAN_DOCUMENT_GUARD))
        self.assertIn("Relevant passages", section)
        self.assertNotIn("Summary of these passages", section)

    def test_a_short_budget_rebuilds_changed_passages_without_a_model_too(self):
        denial = self.denial(SF_LETTER)
        self.refresh(denial)
        denial.procedure = "MRI of the lumbar spine"
        denial.save(update_fields=["procedure"])
        found, models = self.refresh(denial, deadline=time.monotonic() + 5)
        self.assertTrue(found)
        models.search.assert_not_awaited()
        models.summarize.assert_not_awaited()
        first = self.rows(denial)[0]
        self.assertEqual(first.relevance_key, current_hashes(denial)[1])
        self.assertNotEqual(first.relevant_text, "")
        self.assertEqual(first.summary, "")

    def test_a_short_budget_does_not_put_a_tie_to_the_model(self):
        denial = self.denial(TIE_BREAK_LETTER)
        found, models = self.refresh(
            denial, tie_break_answer="1", deadline=time.monotonic() + 5
        )
        self.assertFalse(found)
        models.infer.assert_not_awaited()
        models.search.assert_not_awaited()
        # Nothing stored, so a refresh with budget asks.
        self.assertEqual(self.rows(denial), [])
        self.assertEqual(get_plan_document_context_for_denial(denial), "")

    def test_a_tie_waits_for_a_refresh_with_budget(self):
        denial = self.denial(TIE_BREAK_LETTER)
        self.refresh(denial, tie_break_answer="1", deadline=time.monotonic() + 5)
        found, models = self.refresh(
            denial,
            tie_break_answer="1",
            deadline=time.monotonic() + SHORT_BUDGET_SECONDS + 60,
        )
        self.assertTrue(found)
        models.infer.assert_awaited_once()
        self.assertEqual(
            [r.document_id for r in self.rows(denial) if r.selected],
            [self.deductible_hmo_eoc.id],
        )

    def test_an_ample_budget_still_asks_the_models(self):
        denial = self.denial(SF_LETTER)
        found, models = self.refresh(
            denial, deadline=time.monotonic() + SHORT_BUDGET_SECONDS + 60
        )
        self.assertTrue(found)
        models.search.assert_awaited_once()
        models.summarize.assert_awaited_once()
        self.assertEqual(self.rows(denial)[0].summary, SUMMARY)


class ExtractionStepTests(_CorpusMixin, TransactionTestCase):
    """``DenialCreatorHelper.match_public_plan_documents``: the extraction
    step's outcome strings."""

    def setUp(self):
        self.build_corpus()

    def step(self, denial, **mocks):
        with (
            override_settings(PLAN_DOCUMENT_MATCHING_ENABLED=True),
            _no_models(**mocks) as models,
        ):
            outcome = asyncio.run(
                DenialCreatorHelper.match_public_plan_documents(denial.denial_id)
            )
        return outcome, models

    def test_the_sf_letter_is_found_and_readable_later_from_the_letter_alone(self):
        denial = self.denial(SF_LETTER)
        self.assertIsNone(denial.insurance_company_obj)
        self.assertFalse(denial.insurance_company)
        outcome, _ = self.step(denial)
        self.assertEqual(outcome, EXTRACTION_OUTCOME_FOUND)
        self.assertEqual(
            [r.document_id for r in self.rows(denial) if r.selected],
            [self.norcal_eoc.id, self.norcal_sbc.id],
        )
        section = get_plan_document_context_for_denial(denial)
        self.assertTrue(section.startswith(PLAN_DOCUMENT_GUARD))
        self.assertIn(self.norcal_eoc.citation_name(), section)
        self.assertIn(", page ", section)
        self.assertTrue(section.endswith(PLAN_DOCUMENT_END + "\n"))
        # The step resolved the carrier for itself and wrote none of the
        # row's carrier columns.
        denial.refresh_from_db()
        self.assertIsNone(denial.insurance_company_obj)

    def test_a_typed_insurer_that_disagrees_with_the_letter_finds_nothing(self):
        denial = self.denial(SF_LETTER, insurance_company="Acme Health")
        outcome, models = self.step(denial)
        self.assertEqual(outcome, EXTRACTION_OUTCOME_NOTHING_FOUND)
        self.assertEqual(self.rows(denial), [])
        models.infer.assert_not_awaited()
        models.search.assert_not_awaited()

    def test_a_generic_letter_finds_nothing_but_keeps_the_ranked_rows(self):
        denial = self.denial(GENERIC_LETTER)
        outcome, _ = self.step(denial)
        self.assertEqual(outcome, EXTRACTION_OUTCOME_NOTHING_FOUND)
        rows = self.rows(denial)
        self.assertTrue(rows)
        self.assertFalse(any(r.selected for r in rows))


class LogHygieneTests(_CorpusMixin, TransactionTestCase):
    def setUp(self):
        self.build_corpus()

    def test_a_match_logs_ids_and_counts_never_the_letter_the_document_or_a_city(self):
        denial = self.denial(SF_LETTER)
        with _captured_logs() as lines, _no_models():
            self.assertTrue(asyncio.run(arefresh_match(denial)))
        joined = "\n".join(lines)
        # The match did log beside the denial id (the wording is its own),
        # so the assertions below are about its lines.
        self.assertTrue(any(str(denial.denial_id) in line for line in lines))
        lowered = joined.lower()
        for secret in (
            SF_LETTER,
            "denied an MRI",
            DOCUMENT_SENTINEL,
            "Northern California",
        ):
            self.assertNotIn(secret.lower(), lowered)
        self.assertNotIn("san francisco", lowered)
        for keyword in self.norcal_eoc.match_keywords:
            self.assertNotIn(keyword, lowered)

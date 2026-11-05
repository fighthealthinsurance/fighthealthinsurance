"""Match a denial to the public plan documents we fetched.

The corpus is :class:`PublicPlanDocument`: Evidence of Coverage, Summary of
Benefits and Coverage and other plan documents the pipeline in
healthinsurance-llm-internal fetched and ``import_plan_documents`` loaded.
Given a denial, this decides which of them, if any, are the member's own
plan documents, and hands the appeal prompt the passages that matter.

How a match is made:

1. The carrier. Candidates come only from the one carrier the denial
   resolves to (``resolve_carrier``): the structured company on the row,
   else the insurer the person typed, else the carrier regexes over the
   letter, refused when the typed insurer disagrees. No carrier, no match,
   so a document is never attached to another insurer's denial.
2. Hard excludes. A document for another state, another line of business
   the person confirmed, the wrong kind of coverage (dental against a
   medical claim), another plan of the same carrier, or whose
   ``negative_regex`` hits the letter is out (``score_candidate``).
3. Scoring. Identifying signals (the same plan, a plan id prefix, the
   employer, the document's regex or keywords in the letter) carry the
   match; state, line of business, plan year, document type and coverage
   kind only add to one. A document is selectable only above ``MIN_SCORE``
   AND with at least one identifying signal, so a lone document never
   matches every letter of its carrier.
4. Selection (``select``). The best candidate wins; documents that do not
   conflict with it (the same plan's SBC beside its EOC) ride along, up to
   ``MAX_SELECTED``. A conflicting candidate within ``MARGIN`` points makes
   the match ambiguous: one of our own models may break the tie
   (``tie_break``), and with no answer nothing is selected. Fail closed.
5. Relevance (``build_relevance``). The selected documents go through
   MLPlanDocHelper's search-term and passage selection, within
   ``PASSAGE_BUDGET_CHARS``, and are summarized; the passages and summary
   are stored on the rank-1 selected :class:`PlanDocumentMatch` row.

Every candidate that survived the hard excludes is stored, ranked, with its
signals, so staff can see why a document was or was not chosen. Rows carry
an ``inputs_hash`` over what the selection depends on (the letter, the
carrier, and only the facts this carrier's corpus keys on); a hash that no
longer matches the denial means the rows are stale and never read, and a
refresh recomputes them. Procedure and diagnosis only change the passages,
so they live in a separate ``relevance_key`` that rebuilds the text and
keeps the selection.

Privacy: the letter excerpt a tie-break or search-term prompt carries goes
to our own models only (``infer_with_fallback``'s default, internal
general-purpose models; never pass ``models=``). Logs carry ids, scores and
counts, never a keyword phrase, an employer, the letter or document text:
a city beside a denial id is location data.

The flag ``PLAN_DOCUMENT_MATCHING_ENABLED`` gates the extraction step, the
appeal-time refresh and the prompt section; the importer and admin work
without it.
"""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from channels.db import database_sync_to_async
from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max
from loguru import logger

from fighthealthinsurance.context_utils import truncate_at_boundary
from fighthealthinsurance.document_grep import whole_phrase_pattern
from fighthealthinsurance.medical_code_extractor import collect_denial_text
from fighthealthinsurance.ml.ml_inference import infer_with_fallback
from fighthealthinsurance.ml.ml_plan_doc_helper import (
    MLPlanDocHelper,
    PlanDocumentText,
)
from fighthealthinsurance.models import (
    Denial,
    InsuranceCompany,
    LineOfBusiness,
    PlanDocumentMatch,
    PlanDocuments,
    PublicPlanDocument,
)
from fighthealthinsurance.pa_requirements import infer_line_of_business
from fighthealthinsurance.payer_policy_helper import resolve_company_from_text

#: The total a candidate needs to be selectable, with an identifying signal.
MIN_SCORE = 35
#: The lead a winner needs over a candidate that conflicts with it.
MARGIN = 15
#: The most documents quoted for one denial (an EOC and its SBC, say).
MAX_SELECTED = 3
#: Candidate rows kept per denial, selected or not.
MAX_STORED = 10
#: The tie-break model call: internal models only, a number back.
TIE_BREAK_TIMEOUT = 20
TIE_BREAK_MODELS = 2
#: The passages selected from the matched documents, and their summary.
PASSAGE_BUDGET_CHARS = 4500
SUMMARY_MAX_CHARS = 1500
#: The rendered prompt section.
CONTEXT_MAX_CHARS = 6000

_SPECIFIC_LOBS = frozenset(
    v
    for v in LineOfBusiness.values
    if v not in (LineOfBusiness.ALL, LineOfBusiness.OTHER)
)

# What a plan source the person confirmed (fixtures/plan_source.yaml) says
# about the line of business, keyed the way shared_plan_documents normalises
# the names. "Medicare Regular", "Veterans Affairs", "Other" and "Don't
# know" say nothing.
_LOB_FOR_PLAN_SOURCE: Dict[str, str] = {
    "medicare advantage": LineOfBusiness.MEDICARE_ADVANTAGE,
    "medicaid": LineOfBusiness.MEDICAID,
    "state marketplace / affordable care act": LineOfBusiness.EXCHANGE,
    "employer -- state government": LineOfBusiness.COMMERCIAL,
    "employer -- federal government": LineOfBusiness.COMMERCIAL,
    "employer -- other government": LineOfBusiness.COMMERCIAL,
    "employer -- private": LineOfBusiness.COMMERCIAL,
    "union": LineOfBusiness.COMMERCIAL,
    "other group": LineOfBusiness.COMMERCIAL,
}

# Points for the kind of document, so an EOC outranks the SBC of the same
# plan and sorts first among the selected.
_DOCUMENT_TYPE_POINTS: Dict[str, int] = {
    PublicPlanDocument.DOCUMENT_TYPE_EVIDENCE_OF_COVERAGE: 6,
    PublicPlanDocument.DOCUMENT_TYPE_CERTIFICATE_OF_COVERAGE: 5,
    PublicPlanDocument.DOCUMENT_TYPE_SUMMARY_PLAN_DESCRIPTION: 5,
    PublicPlanDocument.DOCUMENT_TYPE_MEMBER_HANDBOOK: 3,
    PublicPlanDocument.DOCUMENT_TYPE_SUMMARY_OF_BENEFITS: 2,
    PublicPlanDocument.DOCUMENT_TYPE_FORMULARY: 2,
    PublicPlanDocument.DOCUMENT_TYPE_MEDICAL_POLICY: 1,
    PublicPlanDocument.DOCUMENT_TYPE_OTHER: 0,
}
_DOCUMENT_TYPE_LABELS = dict(PublicPlanDocument.DOCUMENT_TYPE_CHOICES)

# A dental claim, where the row does not say so.
_DENTAL_RE = re.compile(r"\bdental\b|\bdentist\b|\borthodont", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(20\d\d)\b")
_NUMBER_ANSWER_RE = re.compile(r"\s*(\d+)\s*")

#: The signals that identify the plan, as opposed to agreeing with it.
IDENTIFYING_SIGNALS = (
    "same_plan",
    "plan_id_prefix",
    "employer",
    "match_regex",
    "keyword",
)

PLAN_DOCUMENT_GUARD = (
    "PLAN DOCUMENT EXCERPTS (matched automatically from public documents): "
    "These passages come from published plan documents that appear to be this "
    "member's plan, matched automatically by insurer, region, plan and year. "
    "The match may be wrong and the plan year or product may differ. They are "
    "NOT documents the patient supplied: the PLAN DETAILS the patient gave take "
    'precedence. Quote only the passages below, attribute them as "the '
    'published <document title>" with the page, and do not assert any term of '
    "the plan that is not quoted here; if the appeal turns on a provision not "
    "quoted, say the member's plan documents should be checked."
)
PLAN_DOCUMENT_END = "END OF PLAN DOCUMENT EXCERPTS."


def enabled() -> bool:
    """Whether denials are matched to the corpus and the prompt gets the
    section. The staff Experiments page asks here too (experiments.py)."""
    return bool(getattr(settings, "PLAN_DOCUMENT_MATCHING_ENABLED", False))


# ---- The denial's side ------------------------------------------------------


@dataclass(frozen=True)
class DenialFacts:
    """What the matcher knows about a denial, read once."""

    company_id: int
    plan_id: Optional[int]
    state: str
    lob: Optional[str]
    #: True when the line of business comes from a plan source the person
    #: (or a confident triage) set; a line inferred from the letter's words
    #: is a hint, not grounds to exclude.
    lob_is_hard: bool
    dental: bool
    employer_name: str
    plan_id_text: str
    year: Optional[int]
    #: The letter and the plan-naming fields, searched for keywords.
    blob: str
    letter_hash: str
    procedure: str
    diagnosis: str


def _state_code(value: Optional[str]) -> str:
    """A two-letter state code out of a code or a state's name, or ""."""
    given = " ".join((value or "").split())
    if not given:
        return ""
    if len(given) == 2 and given.isalpha():
        return given.upper()
    from fighthealthinsurance.state_help import get_states_sorted_by_name

    lowered = given.lower()
    for known in get_states_sorted_by_name():
        if lowered in (known.abbreviation.lower(), known.name.lower()):
            return known.abbreviation.upper() if len(known.abbreviation) == 2 else ""
    return ""


def _lob_value(lob: Any) -> Optional[str]:
    if lob is None:
        return None
    return str(getattr(lob, "value", lob))


def _names_disagree(named: Optional[str], theirs: Optional[str]) -> bool:
    # The one rule for "does the insurer the person typed contradict a
    # carrier": DenialCreatorHelper's, imported here so the two never drift.
    from fighthealthinsurance.common_view_logic import DenialCreatorHelper

    return DenialCreatorHelper._names_disagree(named, theirs)


def _carrier_from_letter(text: str) -> Optional[InsuranceCompany]:
    """The one carrier whose regex finds itself in the letter, or None when
    none or several do (the regex fallback of
    DenialCreatorHelper._match_insurance_company, made strict)."""
    if not text:
        return None
    hits: List[InsuranceCompany] = []
    # .only() must keep both regex columns: RegexField reads straight from
    # the instance dict and cannot load a deferred column.
    for company in InsuranceCompany.objects.exclude(regex="").only(
        "id", "name", "regex", "negative_regex"
    ):
        try:
            if not company.regex.pattern or not company.regex.search(text):
                continue
            if company.negative_regex.pattern and company.negative_regex.search(text):
                continue
        except Exception:
            logger.opt(exception=True).debug(
                f"Error applying regex for company {company.id}"
            )
            continue
        hits.append(company)
    if len(hits) != 1:
        if hits:
            logger.debug(
                f"{len(hits)} carriers match the letter; none resolved for "
                "plan documents"
            )
        return None
    return hits[0]


def resolve_carrier(denial: Denial) -> Optional[InsuranceCompany]:
    """The one carrier a denial's plan documents may come from, or None.

    The structured company on the row; else the insurer the person typed,
    resolved the strict way (name, alt name, regex on the name); else the
    carrier regexes over the letter, refused when the typed insurer names
    someone else (their words outrank the letter, as everywhere the
    extractors write a carrier). Sync: call it off the event loop.
    """
    company: Optional[InsuranceCompany] = denial.insurance_company_obj
    typed = (denial.insurance_company or "").strip()
    if company is None and typed:
        company = resolve_company_from_text(typed)
    if company is None:
        company = _carrier_from_letter(denial.denial_text or "")
        if company is not None and typed and _names_disagree(typed, company.name):
            logger.debug(
                f"Denial {denial.denial_id}: the typed insurer disagrees with "
                f"carrier {company.id} found in the letter; no plan documents"
            )
            return None
    return company


def denial_facts(denial: Denial, company: InsuranceCompany) -> DenialFacts:
    """Read the denial once (sync ORM) into the facts the scorer uses."""
    source_names = {
        " ".join(name.split()).lower()
        for name in denial.plan_source.values_list("name", flat=True)
    }
    hard_lobs = {
        _LOB_FOR_PLAN_SOURCE[name]
        for name in source_names
        if name in _LOB_FOR_PLAN_SOURCE
    }
    lob: Optional[str]
    if len(hard_lobs) == 1:
        lob, lob_is_hard = _lob_value(hard_lobs.pop()), True
    else:
        lob, lob_is_hard = _lob_value(infer_line_of_business(denial)), False
    denial_text = denial.denial_text or ""
    year: Optional[int] = None
    if denial.denial_date:
        year = denial.denial_date.year
    else:
        found = _YEAR_RE.search(denial.date_of_service or "")
        if found:
            year = int(found.group(1))
    return DenialFacts(
        company_id=company.id,
        plan_id=denial.insurance_plan_obj_id,
        state=_state_code(denial.state) or _state_code(denial.your_state),
        lob=lob,
        lob_is_hard=lob_is_hard,
        dental=bool(denial.dental_claim) or bool(_DENTAL_RE.search(denial_text)),
        employer_name=(denial.employer_name or "").strip(),
        plan_id_text=(denial.plan_id or "").strip(),
        year=year,
        blob=collect_denial_text(
            denial,
            "denial_text",
            "insurance_company",
            "employer_name",
            "plan_id",
            "plan_context",
        ),
        letter_hash=hashlib.sha256(denial_text.encode("utf-8")).hexdigest()[:16],
        procedure=(denial.value_or_candidate("procedure") or "").strip(),
        diagnosis=(denial.value_or_candidate("diagnosis") or "").strip(),
    )


# ---- The corpus's side -------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """A document's matching data, as plain values the async code may hold."""

    id: int
    title: str
    citation_name: str
    document_type: str
    coverage_kind: str
    line_of_business: str
    state: str
    region: str
    employer_name: str
    plan_year: Optional[int]
    plan_id_prefixes: Tuple[str, ...]
    match_keywords: Tuple[str, ...]
    match_regex: Optional["re.Pattern[str]"]
    negative_regex: Optional["re.Pattern[str]"]
    insurance_plan_id: Optional[int]
    plan_name: str
    source_url: str


def _pattern_or_none(value: Any) -> Optional["re.Pattern[str]"]:
    pattern = getattr(value, "pattern", None)
    return value if pattern else None


def _candidate(doc: PublicPlanDocument, plan_name: str) -> Candidate:
    return Candidate(
        id=doc.id,
        title=doc.title,
        citation_name=doc.citation_name(),
        document_type=doc.document_type,
        coverage_kind=doc.coverage_kind,
        line_of_business=str(doc.line_of_business),
        state=(doc.state or "").upper(),
        region=(doc.region or "").strip(),
        employer_name=(doc.employer_name or "").strip(),
        plan_year=doc.plan_year,
        plan_id_prefixes=tuple(
            str(p).strip().upper()
            for p in (doc.plan_id_prefixes or [])
            if str(p).strip()
        ),
        match_keywords=tuple(
            str(k).strip().lower() for k in (doc.match_keywords or []) if str(k).strip()
        ),
        match_regex=_pattern_or_none(doc.match_regex),
        negative_regex=_pattern_or_none(doc.negative_regex),
        insurance_plan_id=doc.insurance_plan_id,
        plan_name=plan_name,
        source_url=doc.source_url or "",
    )


def _candidates(company: InsuranceCompany) -> List[Candidate]:
    # defer("text"), never an only() that leaves out the regex columns.
    rows = PublicPlanDocument.objects.filter(
        insurance_company=company, is_active=True
    ).defer("text")
    # Plan names through values(), not select_related: InsurancePlan's regex
    # columns are NOT NULL and a left join on a document without a plan
    # would hand NULL to RegexField.
    plan_names = dict(
        rows.filter(insurance_plan__isnull=False).values_list(
            "id", "insurance_plan__plan_name"
        )
    )
    return [_candidate(doc, plan_names.get(doc.id) or "") for doc in rows]


def corpus_stamp(company_id: int) -> Tuple[int, str]:
    """How many active documents the carrier has and when one last changed:
    part of the hash, so a re-import or a deactivation re-matches."""
    agg = PublicPlanDocument.objects.filter(
        insurance_company_id=company_id, is_active=True
    ).aggregate(n=Count("id"), latest=Max("updated_at"))
    latest = agg["latest"]
    return int(agg["n"] or 0), latest.isoformat() if latest else ""


# ---- Scoring and selection (pure) -------------------------------------------


@dataclass(frozen=True)
class Scored:
    candidate: Candidate
    score: float
    signals: Dict[str, Any]
    identifying: int


def _type_priority(c: Candidate) -> int:
    return -_DOCUMENT_TYPE_POINTS.get(c.document_type, 0)


def score_candidate(c: Candidate, facts: DenialFacts) -> Optional[Scored]:
    """A candidate's score for the denial, or None when a hard exclude
    applies. Pure: no ORM, no model."""
    doc_lob_specific = c.line_of_business in _SPECIFIC_LOBS
    # E3 another state.
    if c.state and facts.state and c.state != facts.state:
        return None
    # E4 another line of business the person confirmed.
    if (
        doc_lob_specific
        and facts.lob
        and facts.lob_is_hard
        and c.line_of_business != facts.lob
    ):
        return None
    # E5 the wrong kind of coverage.
    is_dental_doc = c.coverage_kind == PublicPlanDocument.COVERAGE_KIND_DENTAL
    if facts.dental != is_dental_doc:
        return None
    # E6 the document rules itself out.
    if c.negative_regex is not None and c.negative_regex.search(facts.blob):
        return None
    # E7 another plan of the same carrier.
    if c.insurance_plan_id and facts.plan_id and c.insurance_plan_id != facts.plan_id:
        return None

    signals: Dict[str, Any] = {}
    score = 0.0
    if c.insurance_plan_id and facts.plan_id and c.insurance_plan_id == facts.plan_id:
        signals["same_plan"] = 40
    plan_id_text = facts.plan_id_text.upper()
    if plan_id_text and any(plan_id_text.startswith(p) for p in c.plan_id_prefixes):
        signals["plan_id_prefix"] = 30
    if c.employer_name:
        pattern = whole_phrase_pattern(c.employer_name)
        hit = bool(facts.employer_name) and bool(pattern.search(facts.employer_name))
        if not hit and (len(c.employer_name.split()) >= 2 or len(c.employer_name) >= 6):
            hit = bool(pattern.search(facts.blob))
        if hit:
            signals["employer"] = 30
    if c.match_regex is not None and c.match_regex.search(facts.blob):
        signals["match_regex"] = 25
    hits = sum(
        1 for k in c.match_keywords if whole_phrase_pattern(k).search(facts.blob)
    )
    if hits:
        # The count, never the phrases: the signals are logged beside the
        # denial id, and a phrase is often a city.
        signals["keyword"] = min(hits * 10, 30)
        signals["keyword_hits"] = hits
    if c.state and facts.state and c.state == facts.state:
        signals["state"] = 10
    if doc_lob_specific and facts.lob:
        if c.line_of_business == facts.lob:
            signals["line_of_business"] = 10
        elif not facts.lob_is_hard:
            signals["line_of_business"] = -10
    if c.plan_year and facts.year:
        if c.plan_year == facts.year:
            signals["plan_year"] = 10
        elif abs(c.plan_year - facts.year) == 1:
            signals["plan_year"] = 5
    type_points = _DOCUMENT_TYPE_POINTS.get(c.document_type, 0)
    if type_points:
        signals["document_type"] = type_points
    if is_dental_doc == facts.dental:
        signals["coverage_kind"] = 3
    score = float(sum(v for k, v in signals.items() if k != "keyword_hits"))
    identifying = sum(1 for k in IDENTIFYING_SIGNALS if k in signals)
    signals["identifying"] = identifying
    return Scored(candidate=c, score=score, signals=signals, identifying=identifying)


def conflicts(a: Candidate, b: Candidate) -> bool:
    """Whether two documents cannot both be the member's: another plan,
    state, region, year or employer, or another specific line of business."""
    if (
        a.insurance_plan_id
        and b.insurance_plan_id
        and a.insurance_plan_id != b.insurance_plan_id
    ):
        return True
    if a.state and b.state and a.state != b.state:
        return True
    if a.region and b.region and a.region.lower() != b.region.lower():
        return True
    if a.plan_year and b.plan_year and a.plan_year != b.plan_year:
        return True
    if (
        a.employer_name
        and b.employer_name
        and a.employer_name.lower() != b.employer_name.lower()
    ):
        return True
    if (
        a.line_of_business in _SPECIFIC_LOBS
        and b.line_of_business in _SPECIFIC_LOBS
        and a.line_of_business != b.line_of_business
    ):
        return True
    return False


def ranked(scored: Sequence[Scored]) -> List[Scored]:
    """Best first: score, then identifying signals, then the kind of document."""
    return sorted(
        scored,
        key=lambda s: (
            -s.score,
            -s.identifying,
            _type_priority(s.candidate),
            s.candidate.id,
        ),
    )


def select(scored: Sequence[Scored]) -> Tuple[List[Scored], List[Scored]]:
    """The documents to quote, and the rivals that make the choice ambiguous.

    Returns ``(selected, rivals)``. With rivals the rules select nothing:
    the caller may ask a model to choose among the top candidate and its
    rivals, and selects nothing without an answer.
    """
    eligible = [
        s for s in ranked(scored) if s.score >= MIN_SCORE and s.identifying >= 1
    ]
    if not eligible:
        return [], []
    top = eligible[0]
    selected = [top]
    rivals: List[Scored] = []
    for s in eligible[1:]:
        if any(conflicts(s.candidate, chosen.candidate) for chosen in selected):
            if top.score - s.score < MARGIN:
                rivals.append(s)
            continue
        if len(selected) < MAX_SELECTED:
            selected.append(s)
    if rivals:
        return [], rivals
    return selected, []


def companions(pick: Scored, pool: Sequence[Scored]) -> List[Scored]:
    """``pick`` and the pool's documents that do not conflict with it."""
    chosen = [pick]
    for s in ranked(pool):
        if s is pick or len(chosen) >= MAX_SELECTED:
            continue
        if not any(conflicts(s.candidate, c.candidate) for c in chosen):
            chosen.append(s)
    return chosen


# ---- The sync step --------------------------------------------------------------


@dataclass
class MatchOutcome:
    company: InsuranceCompany
    facts: DenialFacts
    inputs_hash: str
    relevance_key: str
    scored: List[Scored]
    selected: List[Scored]
    rivals: List[Scored]
    #: The top candidate and its rivals, for the tie-break.
    tie_break_candidates: List[Scored]
    #: Document texts by id, for the documents that may be quoted.
    texts: Dict[int, PlanDocumentText]


def _inputs_hash(
    facts: DenialFacts, stamp: Tuple[int, str], candidates: Sequence[Candidate]
) -> str:
    """The selection's inputs, hashed; only the facts this carrier's corpus
    keys on, so a plan id or plan written later by a sibling extraction
    step does not stale the rows of a corpus that never looks at it."""
    parts: Dict[str, Any] = {
        "letter": facts.letter_hash,
        "company": facts.company_id,
        "state": facts.state,
        "dental": facts.dental,
        "corpus_n": stamp[0],
        "corpus_latest": stamp[1],
    }
    if any(c.insurance_plan_id for c in candidates):
        parts["plan"] = facts.plan_id
    if any(c.plan_id_prefixes for c in candidates):
        parts["plan_id_text"] = facts.plan_id_text.upper()
    if any(c.employer_name for c in candidates):
        parts["employer"] = facts.employer_name.lower()
    if any(c.line_of_business in _SPECIFIC_LOBS for c in candidates):
        parts["lob"] = [facts.lob, facts.lob_is_hard]
    return hashlib.sha256(
        json.dumps(parts, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _relevance_key(facts: DenialFacts) -> str:
    return hashlib.sha256(
        f"{facts.procedure}|{facts.diagnosis}".encode("utf-8")
    ).hexdigest()[:16]


def _prepare(
    denial: Denial, company: Optional[InsuranceCompany] = None
) -> Optional[Tuple[InsuranceCompany, DenialFacts, List[Candidate], str, str]]:
    if not isinstance(denial, Denial) or denial.pk is None:
        return None
    company = company or resolve_carrier(denial)
    if company is None:
        return None
    facts = denial_facts(denial, company)
    candidates = _candidates(company)
    stamp = corpus_stamp(company.id)
    return (
        company,
        facts,
        candidates,
        _inputs_hash(facts, stamp, candidates),
        _relevance_key(facts),
    )


def current_hashes(denial: Denial) -> Optional[Tuple[str, str]]:
    """``(inputs_hash, relevance_key)`` the denial's rows must carry to be
    current, or None when no carrier resolves. Sync."""
    prepared = _prepare(denial)
    if prepared is None:
        return None
    return prepared[3], prepared[4]


def match_documents(
    denial: Denial, company: Optional[InsuranceCompany] = None
) -> Optional[MatchOutcome]:
    """Score the carrier's documents for the denial. Sync (ORM); the
    outcome holds plain data the async steps can use."""
    prepared = _prepare(denial, company)
    if prepared is None:
        return None
    company, facts, candidates, inputs_hash, relevance_key = prepared
    scored = [
        s for s in (score_candidate(c, facts) for c in candidates) if s is not None
    ]
    selected, rivals = select(scored)
    tie_break_candidates: List[Scored] = []
    if rivals:
        top = [
            s for s in ranked(scored) if s.score >= MIN_SCORE and s.identifying >= 1
        ][0]
        tie_break_candidates = [top] + rivals
    quotable = {s.candidate.id for s in selected} | {
        s.candidate.id for s in tie_break_candidates
    }
    names = {s.candidate.id: s.candidate.citation_name for s in scored}
    texts = {
        doc_id: PlanDocumentText(name=names[doc_id], text=text)
        for doc_id, text in PublicPlanDocument.objects.filter(
            id__in=quotable
        ).values_list("id", "text")
        if text
    }
    logger.debug(
        f"Plan documents for denial {denial.denial_id}: carrier {company.id}, "
        f"{len(candidates)} candidates, {len(scored)} scored, "
        f"{len(selected)} selected, {len(rivals)} rivals"
    )
    return MatchOutcome(
        company=company,
        facts=facts,
        inputs_hash=inputs_hash,
        relevance_key=relevance_key,
        scored=ranked(scored),
        selected=selected,
        rivals=rivals,
        tie_break_candidates=tie_break_candidates,
        texts=texts,
    )


# ---- The model steps ------------------------------------------------------------


def _tie_break_prompt(denial_text: str, candidates: Sequence[Scored]) -> str:
    lines = []
    for number, s in enumerate(candidates, start=1):
        c = s.candidate
        lines.append(
            f"{number}. {c.title}; plan: {c.plan_name or '-'}; region: "
            f"{c.region or '-'}; state: {c.state or 'national'}; year: "
            f"{c.plan_year or '-'}; line of business: {c.line_of_business}; "
            f"employer: {c.employer_name or '-'}"
        )
    excerpt = truncate_at_boundary(denial_text, 2000)
    return (
        "These plan documents all belong to the insurer named in a denial "
        "letter, and the letter can belong to at most one of them.\n\n"
        + "\n".join(lines)
        + f"\n\nDenial letter excerpt:\n{excerpt}\n\n"
        "Answer with the number of the plan document that is this member's "
        "plan, or 0 if the letter does not say. Answer with the number only."
    )


def tie_break_answer_valid(text: str, count: int) -> bool:
    """Whether a tie-break answer is a number from 0 to ``count``."""
    match = _NUMBER_ANSWER_RE.fullmatch(text or "")
    if match is None:
        return False
    return 0 <= int(match.group(1)) <= count


async def tie_break(denial_text: str, candidates: Sequence[Scored]) -> Optional[Scored]:
    """One of our own models picks among candidates the rules cannot
    separate, or 0 for none. Internal models only: ``models=`` is never
    passed, so the letter excerpt stays with us."""
    if len(candidates) < 2:
        return None
    count = len(candidates)
    answer = await infer_with_fallback(
        system_prompts=[
            "You match health insurance denial letters to the plan document "
            "they belong to. Answer with a single number."
        ],
        prompt=_tie_break_prompt(denial_text, candidates),
        temperature=0.0,
        timeout=TIE_BREAK_TIMEOUT,
        model_count=TIE_BREAK_MODELS,
        label="plan document tie-break",
        validator=lambda text: tie_break_answer_valid(text, count),
        task="entity_extraction",
    )
    if not answer:
        return None
    pick = int(answer.strip())
    if pick < 1:
        return None
    return candidates[pick - 1]


async def build_relevance(
    denial: Denial,
    documents: Sequence[PlanDocumentText],
    *,
    use_model: bool = True,
) -> Tuple[str, Optional[str]]:
    """The passages of ``documents`` that matter to the denial, within
    ``PASSAGE_BUDGET_CHARS``, and their summary (None without a model).

    Documents that fit go whole; otherwise MLPlanDocHelper's search strings
    (a model's, or the fallback terms when ``use_model`` is False) choose
    the passages, and the summary is written from exactly the text that
    will be quoted.
    """
    if not documents:
        return "", None
    denial_text = denial.denial_text or ""
    procedure = denial.value_or_candidate("procedure")
    diagnosis = denial.value_or_candidate("diagnosis")
    whole = MLPlanDocHelper.whole_text(documents)
    if len(whole) <= PASSAGE_BUDGET_CHARS:
        relevant = whole
    else:
        if use_model:
            terms = await MLPlanDocHelper.generate_search_terms(
                denial_text,
                procedure=procedure,
                diagnosis=diagnosis,
                documents=documents,
            )
        else:
            terms = MLPlanDocHelper._extract_fallback_terms(
                denial_text, procedure, diagnosis
            )
        relevant = MLPlanDocHelper.relevant_passages(
            documents, terms, budget_chars=PASSAGE_BUDGET_CHARS
        )
    if not relevant:
        return "", None
    summary: Optional[str] = None
    if use_model:
        summary = await MLPlanDocHelper.summarize_relevant_sections(denial, relevant)
        if summary:
            summary = truncate_at_boundary(summary, SUMMARY_MAX_CHARS)
    return relevant, summary


# ---- Storing ---------------------------------------------------------------------


def _store_rows(
    denial: Denial, outcome: MatchOutcome, selected: Sequence[Scored], method: str
) -> bool:
    """Replace the denial's rows with the outcome's, ranked. Only while the
    row still holds the letter the match was made from; False otherwise."""
    selected_ids = {s.candidate.id for s in selected}
    with transaction.atomic():
        if not Denial.objects.filter(
            denial_id=denial.denial_id, denial_text=denial.denial_text
        ).exists():
            return False
        PlanDocumentMatch.objects.filter(denial_id=denial.denial_id).delete()
        rows = [
            PlanDocumentMatch(
                denial_id=denial.denial_id,
                document_id=s.candidate.id,
                company_id=outcome.company.id,
                score=s.score,
                signals=s.signals,
                rank=rank,
                selected=s.candidate.id in selected_ids,
                method=method,
                inputs_hash=outcome.inputs_hash,
            )
            for rank, s in enumerate(outcome.scored[:MAX_STORED], start=1)
        ]
        PlanDocumentMatch.objects.bulk_create(rows)
    return True


def _store_relevance(
    denial: Denial,
    outcome: MatchOutcome,
    document_id: int,
    relevant: str,
    summary: Optional[str],
) -> None:
    PlanDocumentMatch.objects.filter(
        denial_id=denial.denial_id,
        document_id=document_id,
        inputs_hash=outcome.inputs_hash,
    ).update(
        relevant_text=relevant[: PASSAGE_BUDGET_CHARS * 2],
        summary=summary or "",
        relevance_key=outcome.relevance_key,
    )


def _first_selected(rows: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    chosen = [r for r in rows if r["selected"]]
    if not chosen:
        return None
    return min(chosen, key=lambda r: r["rank"])


async def arefresh_match(
    denial: Denial, *, company: Optional[InsuranceCompany] = None
) -> bool:
    """Bring the denial's match rows up to date; True when documents are
    selected for it.

    Rows whose hash is current are kept (their text rebuilt when the
    procedure or diagnosis changed); otherwise the documents are scored
    again, a model breaks a tie when there is one, the rows are stored
    before any text is built, so a timeout keeps the selection, and the
    rank-1 selected row then gets its passages and summary.
    """
    try:
        outcome = await database_sync_to_async(match_documents)(denial, company)
        if outcome is None:
            return False
        existing: List[Dict[str, Any]] = [
            dict(row)
            async for row in PlanDocumentMatch.objects.filter(
                denial_id=denial.denial_id, inputs_hash=outcome.inputs_hash
            ).values("id", "document_id", "selected", "rank", "relevance_key")
        ]
        if existing:
            first = _first_selected(existing)
            if first is None:
                return False
            if first["relevance_key"] != outcome.relevance_key:
                selected_ids = [
                    r["document_id"]
                    for r in sorted(existing, key=lambda r: r["rank"])
                    if r["selected"]
                ]
                texts = await database_sync_to_async(_texts_for)(selected_ids)
                if texts:
                    relevant, summary = await build_relevance(denial, texts)
                    await database_sync_to_async(_store_relevance)(
                        denial, outcome, first["document_id"], relevant, summary
                    )
            return True
        selected = list(outcome.selected)
        method = PlanDocumentMatch.METHOD_RULES
        if not selected and outcome.tie_break_candidates:
            pick = await tie_break(
                denial.denial_text or "", outcome.tie_break_candidates
            )
            if pick is not None:
                selected = companions(pick, outcome.tie_break_candidates)
                method = PlanDocumentMatch.METHOD_MODEL
        stored = await database_sync_to_async(_store_rows)(
            denial, outcome, selected, method
        )
        if not stored or not selected:
            return False
        documents = [
            outcome.texts[s.candidate.id]
            for s in selected
            if s.candidate.id in outcome.texts
        ]
        relevant, summary = await build_relevance(denial, documents)
        await database_sync_to_async(_store_relevance)(
            denial, outcome, selected[0].candidate.id, relevant, summary
        )
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.opt(exception=True).warning(
            f"Plan document match failed for denial "
            f"{getattr(denial, 'denial_id', '?')}: {type(e).__name__}"
        )
        return False


def _texts_for(document_ids: Sequence[int]) -> List[PlanDocumentText]:
    by_id = {
        doc.id: doc
        for doc in PublicPlanDocument.objects.filter(
            id__in=list(document_ids), is_active=True
        )
    }
    return [
        PlanDocumentText(name=by_id[i].citation_name(), text=by_id[i].text)
        for i in document_ids
        if i in by_id and by_id[i].text
    ]


# ---- Reading, for the prompt ----------------------------------------------------


def format_plan_document_context(rows: Sequence[PlanDocumentMatch]) -> str:
    """The prompt section for the selected rows (rank order), within
    ``CONTEXT_MAX_CHARS``: the guard, the documents, the passages, then the
    summary, closed by a marker so what follows in the prompt starts
    outside the quoted block."""
    if not rows:
        return ""
    lines = [PLAN_DOCUMENT_GUARD, "Matched document(s):"]
    for row in rows:
        doc = row.document
        where = ", ".join(
            part
            for part in (
                _DOCUMENT_TYPE_LABELS.get(doc.document_type, doc.document_type),
                doc.state or "national",
                doc.region,
                doc.source_url,
            )
            if part
        )
        lines.append(f"- {doc.citation_name()} -- {where}")
    first = rows[0]
    if first.relevant_text:
        lines.append("Relevant passages (page-labelled):")
        lines.append(
            truncate_at_boundary(first.relevant_text, PASSAGE_BUDGET_CHARS, ellipsis="")
        )
    if first.summary:
        lines.append(
            "Summary of these passages (written by a model; only the passages "
            "above may be quoted):"
        )
        lines.append(
            truncate_at_boundary(first.summary, SUMMARY_MAX_CHARS, ellipsis="")
        )
    body = "\n".join(lines)
    body = truncate_at_boundary(
        body, CONTEXT_MAX_CHARS - len(PLAN_DOCUMENT_END) - 2, ellipsis=""
    )
    return f"{body}\n{PLAN_DOCUMENT_END}\n"


def get_plan_document_context_for_denial(denial: Any) -> str:
    """The prompt section for a denial's current, selected match rows, or
    "". Sync (ORM); make_appeals calls it on its worker thread. Nothing
    when the person uploaded plan documents of their own: those govern."""
    if not isinstance(denial, Denial) or denial.pk is None:
        return ""
    if PlanDocuments.objects.filter(denial_id=denial.pk).exists():
        return ""
    hashes = current_hashes(denial)
    if hashes is None:
        return ""
    rows = list(
        PlanDocumentMatch.objects.filter(
            denial_id=denial.pk,
            selected=True,
            inputs_hash=hashes[0],
            document__is_active=True,
        )
        .select_related("document")
        .defer("document__text")
        .order_by("rank")
    )
    if not rows or not rows[0].relevant_text:
        # Selected, but the passages are not built yet (a refresh timed out
        # before the text stage): nothing to quote, so no section. The next
        # refresh rebuilds the text and keeps the selection.
        return ""
    return format_plan_document_context(rows)

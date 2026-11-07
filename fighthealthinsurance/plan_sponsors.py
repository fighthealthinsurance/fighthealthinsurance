"""The companies behind shared plan documents.

A plan through work names its sponsor, the employer: a Summary Plan
Description has to give the sponsor's name and its Employer Identification
Number (29 CFR 2520.102-3(b) and (d)), and its title is often the company's
name, as in "Crumpler Cooperative Welfare Benefit Plan". identify_sponsor
reads both from a document's text with fixed patterns, no model, and only
when the text reads like an employer's plan: an insurer's medical policy
names an insurer, not an employer, and an insurer named as the sponsor is
left out (the InsuranceCompany catalog).

link_sponsor finds the company on the running list (PlanSponsor) by its
EIN, then by its name with the punctuation and "LLC", "Inc." and the like
taken off (never one with a different EIN), and adds it when it isn't
there. The share page's goal counts the
companies on the list, each once (shared_plan_documents.plan_shares).
Sharers never have to name a company: a document that names none stays
unlinked, and staff can link it in the admin.
"""

import re
import uuid
from typing import NamedTuple, Optional, Union

from django.db import IntegrityError, transaction

from fighthealthinsurance.models import InsuranceCompany, PlanSponsor, PolicyDocument

#: How much of a document is looked at: the sponsor and its EIN come in the
#: first pages, or in the plan information section near the end.
HEAD_CHARS = 60_000
TAIL_CHARS = 40_000
#: How far past the sponsor's name its EIN may sit to be taken as its own:
#: an EIN elsewhere may be another organization's.
EIN_WINDOW_CHARS = 400

_SP = r"[ \t]"
_EIN = r"(?P<ein>\d{2}-\d{7})(?!\d)"
# The EIN after its label, on the same line or the next.
_EIN_RULE = re.compile(
    rf"\b(?:employer{_SP}+identification{_SP}+(?:number|no\.?|#)|e\.?i\.?n\.?"
    rf"|(?:plan{_SP}+)?sponsor'?s?{_SP}+(?:ein|tax{_SP}+id(?:entification)?"
    rf"(?:{_SP}+(?:number|no\.?))?)|federal{_SP}+tax{_SP}+id(?:entification)?"
    rf"(?:{_SP}+(?:number|no\.?))?)"
    rf"[^\n\d]{{0,40}}\n?[^\n\d]{{0,20}}{_EIN}",
    re.IGNORECASE,
)
_NAME_CHARS = r"[A-Z0-9][A-Za-z0-9&.,'()/ -]{1,90}?"
# "Plan Sponsor: Crumpler Cooperative" or the name on the next line.
_SPONSOR_RULE = re.compile(
    rf"\b(?:plan{_SP}+sponsor|name{_SP}+of{_SP}+(?:the{_SP}+)?(?:plan{_SP}+)?"
    rf"sponsor|employer{_SP}*/{_SP}*plan{_SP}+sponsor|sponsoring{_SP}+employer)"
    # The rest of a header such as "Plan Sponsor Name and Address", read
    # whole so none of it is taken for the name.
    rf"(?:{_SP}*(?:[/,&]|and)?{_SP}*(?:name|address)\b)*"
    rf"{_SP}*(?::|is\b)?{_SP}*\n?{_SP}*(?P<name>{_NAME_CHARS})(?=\n|,{_SP}*\d|{_SP}{{2,}}|$)",
    re.IGNORECASE | re.MULTILINE,
)
# "Crumpler Cooperative Welfare Benefit Plan", "... Health and Welfare Plan",
# "... Employee Benefit Plan", "... Group Health Plan".
_PLAN_TITLE_RULE = re.compile(
    rf"(?P<name>[A-Z][A-Za-z0-9&.,'-]*(?:{_SP}+[A-Z0-9&][A-Za-z0-9&.,'-]*){{0,7}}?)"
    rf"{_SP}+(?:Health{_SP}+(?:and|&){_SP}+)?(?:Welfare{_SP}+Benefits?|Employee{_SP}+Benefits?"
    rf"|Welfare|Group{_SP}+Health|Health{_SP}+Benefits?|Medical){_SP}+Plans?\b"
)
# A text reads like an employer's plan with any of these.
_EMPLOYER_SIGNAL = re.compile(
    r"\b(?:plan\s+sponsor|employer\s+identification\s+number|summary\s+plan"
    r"\s+description|welfare\s+benefits?\s+plan|employee\s+benefits?\s+plan"
    r"|erisa|employee\s+retirement\s+income\s+security\s+act|plan\s+administrator"
    r"|group\s+health\s+plan|sponsoring\s+employer)\b",
    re.IGNORECASE,
)
# Words that can open a "... Welfare Plan" title without being a company.
_NOT_A_NAME = {
    "the",
    "this",
    "your",
    "our",
    "a",
    "an",
    "each",
    "any",
    "summary",
    "under",
    "of",
    "for",
    "and",
}
# Taken off the end of a name to match it: the legal form and the like.
_SUFFIXES = {
    "llc",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "ltd",
    "limited",
    "lp",
    "llp",
    "plc",
    "pc",
    "pllc",
    "na",
    "the",
}


class Sponsor(NamedTuple):
    name: str
    ein: str


def normalize_name(name: str) -> str:
    """A company name to match on: lower case, no punctuation, and no legal
    form ("Crumpler Cooperative, LLC" and "Crumpler Cooperative" match)."""
    lowered = name.lower().replace(".", "").replace("&", " & ")
    words = re.sub(r"[^a-z0-9& ]+", " ", lowered).split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def _clean_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", raw).strip(" ,.;:-")
    # A trailing address or EIN on the same line is not part of the name.
    name = re.split(
        r",\s*\d|\s+\d{2}-\d{7}|\s+(?:EIN|Employer Identification)\b", name
    )[0]
    return name.strip(" ,.;:-")


def _is_insurer(normalized: str) -> bool:
    if not normalized:
        return False
    for name, alt_names in InsuranceCompany.objects.values_list("name", "alt_names"):
        for known in [name, *(alt_names or "").splitlines()]:
            known_normalized = normalize_name(known)
            if known_normalized and (
                normalized == known_normalized
                or normalized.startswith(known_normalized + " ")
            ):
                return True
    return False


def _good_name(raw: str) -> Optional[str]:
    name = _clean_name(raw)
    normalized = normalize_name(name)
    if (
        len(normalized) < 2
        or normalized.split()[0] in _NOT_A_NAME
        or not re.search(r"[a-z]", normalized)
        or _is_insurer(normalized)
    ):
        return None
    return name


def identify_sponsor(text: str) -> Optional[Sponsor]:
    """The employer a plan document names as its sponsor, or None when the
    text doesn't read like an employer's plan or names no company."""
    if not text:
        return None
    part = text[:HEAD_CHARS] + "\n" + text[-TAIL_CHARS:]
    if not _EMPLOYER_SIGNAL.search(part):
        return None
    for match in _SPONSOR_RULE.finditer(part):
        name = _good_name(match.group("name"))
        if name:
            # Only an EIN right after the sponsor's own lines is its EIN.
            near = _EIN_RULE.search(part, match.start(), match.end() + EIN_WINDOW_CHARS)
            return Sponsor(name=name[:300], ein=near.group("ein") if near else "")
    # A plan title names the company but not whose EIN is where.
    for match in _PLAN_TITLE_RULE.finditer(part):
        name = _good_name(match.group("name"))
        if name:
            return Sponsor(name=name[:300], ein="")
    return None


def match_sponsor(sponsor: Sponsor) -> Optional[PlanSponsor]:
    """The company already on the list for ``sponsor``, changing nothing:
    the one with its EIN; failing that one with its name, but never one
    whose EIN is a different number. Two employers can share a name; their
    EINs tell them apart."""
    normalized = normalize_name(sponsor.name)
    if sponsor.ein:
        company = PlanSponsor.objects.filter(ein=sponsor.ein).first()
        if company is not None:
            return company
        return (
            PlanSponsor.objects.filter(normalized_name=normalized, ein__isnull=True)
            .order_by("pk")
            .first()
        )
    return PlanSponsor.objects.filter(normalized_name=normalized).order_by("pk").first()


def link_sponsor(doc_id: Union[str, uuid.UUID], sponsor: Sponsor) -> PlanSponsor:
    """The company on the running list for ``sponsor`` (match_sponsor),
    added when it isn't there, linked to the document ``doc_id`` if that
    has no company yet."""
    with transaction.atomic():
        company = match_sponsor(sponsor)
        if company is not None and sponsor.ein and not company.ein:
            company.ein = sponsor.ein
            try:
                with transaction.atomic():
                    company.save(update_fields=["ein"])
            except IntegrityError:
                company.refresh_from_db()
        if company is None:
            try:
                with transaction.atomic():
                    company = PlanSponsor.objects.create(
                        name=sponsor.name,
                        normalized_name=normalize_name(sponsor.name),
                        ein=sponsor.ein or None,
                        first_document_id=doc_id,
                    )
            except IntegrityError:
                # Added at the same moment by another reading.
                company = match_sponsor(sponsor)
                if company is None:
                    raise
        PolicyDocument.objects.filter(pk=doc_id, plan_sponsor__isnull=True).update(
            plan_sponsor=company
        )
    return company

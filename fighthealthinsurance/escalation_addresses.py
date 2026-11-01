"""
Address book for the escalation packet feature.

Given a Denial, this module computes the list of regulator / executive
recipients the user could escalate to in parallel with their appeal:

- The state Department of Insurance / insurance commissioner — pulled
  from `state_help.py` using the denial's `your_state` abbreviation.
- The plan's medical director — a generic placeholder addressed to the
  insurer's medical director in care of the insurer (the user fills in
  the mailing address from the denial letter).
- DOL EBSA — for plans likely to be ERISA-covered (self-funded employer
  plans, including those administered by a TPA).

The model `Regulator` already exists in the database for matching denial
text; this module is the complementary "where do we send a letter"
address book that combines per-state DOI data and national resources.
"""

import bisect
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from fighthealthinsurance.models import RegulatorEscalation
from fighthealthinsurance.regulator_routing import letter_names_regulator
from fighthealthinsurance.state_help import (
    StateHelp,
    get_state_help_by_abbreviation,
)

RECIPIENT_DOI = RegulatorEscalation.RECIPIENT_DOI
RECIPIENT_MEDICAL_DIRECTOR = RegulatorEscalation.RECIPIENT_MEDICAL_DIRECTOR
RECIPIENT_DOL_EBSA = RegulatorEscalation.RECIPIENT_DOL_EBSA

# National address for the DOL EBSA — the federal agency that enforces
# ERISA for self-funded employer health plans. Patients can request
# enforcement assistance from EBSA when a fiduciary appears to have
# violated ERISA's claims and appeals procedures (29 C.F.R. § 2560.503-1).
DOL_EBSA_NAME = "U.S. Department of Labor, Employee Benefits Security Administration"
DOL_EBSA_ADDRESS = (
    "Employee Benefits Security Administration\n"
    "U.S. Department of Labor\n"
    "200 Constitution Ave NW\n"
    "Washington, DC 20210"
)
DOL_EBSA_PHONE = "1-866-444-3272"
DOL_EBSA_URL = "https://www.dol.gov/agencies/ebsa"


def sanitize_http_url(value: Optional[str]) -> str:
    """Return *value* only if it is an http(s) URL, else an empty string.

    Recipient URLs end up in ``<a href>`` attributes (server templates and
    the escalation packet's client-side rendering), so unexpected schemes
    like ``javascript:`` must never propagate into a clickable link.
    """
    url = (value or "").strip()
    if url.lower().startswith(("http://", "https://")):
        return url
    return ""


@dataclass
class EscalationRecipient:
    """A single recipient on an escalation packet."""

    recipient_type: str  # one of RegulatorEscalation.RECIPIENT_*
    name: str
    address: str = ""
    phone: str = ""
    url: str = ""
    # Plain English description shown to the user describing why we suggest
    # this recipient — used in the UI checklist and as light prompt context.
    rationale: str = ""
    # Free-form metadata the prompt builder can use (e.g. the state name).
    extra: dict = field(default_factory=dict)


def _doi_recipient(state: StateHelp) -> Optional[EscalationRecipient]:
    """Build a DOI recipient from a StateHelp object, if it has one."""
    dept = state.insurance_department
    if not dept or not dept.name:
        return None
    rationale = (
        f"State insurance regulators investigate complaints against "
        f"insurers and can require the insurer to respond. "
        f"{dept.name} also oversees the external review process in "
        f"{state.name}."
    )
    return EscalationRecipient(
        recipient_type=RECIPIENT_DOI,
        name=dept.name,
        address=dept.complaint_url or dept.url or "",
        phone=dept.consumer_line or dept.phone or "",
        url=sanitize_http_url(dept.url),
        rationale=rationale,
        extra={
            "state_name": state.name,
            "state_abbreviation": state.abbreviation,
            "external_review_available": bool(
                state.external_review and state.external_review.available
            ),
            "external_review_url": (
                state.external_review.info_url if state.external_review else None
            ),
        },
    )


def _medical_director_recipient(
    insurance_company: Optional[str],
    insurance_company_obj: Any = None,
) -> EscalationRecipient:
    """Build a 'Medical Director' recipient for the plan.

    When the denial matched a known ``InsuranceCompany`` row we use its
    appeal-routing info (mailing address, member services / appeals phone,
    website) so the user gets a real number to call instead of a blank.
    """
    company = (
        insurance_company
        if insurance_company and insurance_company != "UNKNOWN"
        else "Your Health Plan"
    )
    # The plan's address isn't always known; the letter renders with an
    # obvious bracketed placeholder for the user to fill from the denial
    # letter when we don't have it on file.
    address = "[Insurance company mailing address from your denial letter]"
    phone = ""
    url = ""
    if insurance_company_obj is not None:
        known_address = getattr(insurance_company_obj, "appeal_address", "") or ""
        if known_address.strip():
            address = f"Medical Director\n{known_address.strip()}"
        phone = getattr(insurance_company_obj, "appeal_phone_number", "") or ""
        url = sanitize_http_url(
            getattr(insurance_company_obj, "member_services_url", "")
            or getattr(insurance_company_obj, "website", "")
        )
    return EscalationRecipient(
        recipient_type=RECIPIENT_MEDICAL_DIRECTOR,
        name=f"Medical Director, {company}",
        address=address,
        phone=phone,
        url=url,
        rationale=(
            "The plan's medical director is the physician responsible for "
            "coverage decisions. A direct, peer-to-peer-style letter to the "
            "medical director can prompt a faster clinical re-review."
        ),
        extra={"insurance_company": company},
    )


def _dol_ebsa_recipient() -> EscalationRecipient:
    """Build the DOL EBSA recipient for ERISA-covered plans."""
    return EscalationRecipient(
        recipient_type=RECIPIENT_DOL_EBSA,
        name=DOL_EBSA_NAME,
        address=DOL_EBSA_ADDRESS,
        phone=DOL_EBSA_PHONE,
        url=DOL_EBSA_URL,
        rationale=(
            "ERISA self-funded employer plans are regulated federally by "
            "DOL EBSA. EBSA investigates fiduciary breaches and violations "
            "of the ERISA claims-and-appeals procedures (29 C.F.R. "
            "§ 2560.503-1)."
        ),
    )


_ERISA_PLAN_SOURCE_KEYWORDS = ("erisa", "self-funded", "self funded")


def _is_erisa_likely(denial: Any) -> bool:
    """Heuristic: is this denial likely to be from an ERISA plan?

    True if any of: the denial is linked to the ERISA Regulator row, the
    structured insurance company is flagged as a TPA, or the plan source
    M2M contains an ERISA-tagged source.
    """
    regulator = getattr(denial, "regulator", None)
    if regulator is not None and (regulator.alt_name or "").upper() == "ERISA":
        return True

    ic = getattr(denial, "insurance_company_obj", None)
    if ic is not None and getattr(ic, "is_tpa", False):
        return True

    plan_source_manager = getattr(denial, "plan_source", None)
    if plan_source_manager is None or not hasattr(plan_source_manager, "all"):
        return False
    for source in plan_source_manager.all():
        label = (getattr(source, "name", "") or "").lower()
        if any(k in label for k in _ERISA_PLAN_SOURCE_KEYWORDS):
            return True
    return False


def get_recipients_for_denial(denial: Any) -> List[EscalationRecipient]:
    """
    Build the list of escalation recipients for a denial.

    Always returns at least the medical director recipient. Adds the DOI
    recipient if the denial has a usable state, and the DOL EBSA recipient
    if the plan looks ERISA-covered.
    """
    recipients: List[EscalationRecipient] = []

    state_abbr = (
        getattr(denial, "your_state", None) or getattr(denial, "state", None) or ""
    )
    if state_abbr:
        state = get_state_help_by_abbreviation(state_abbr)
        if state is not None:
            doi = _doi_recipient(state)
            if doi is not None:
                recipients.append(doi)

    insurance_company = getattr(denial, "insurance_company", None)
    ic_obj = getattr(denial, "insurance_company_obj", None)
    if not insurance_company and ic_obj is not None:
        insurance_company = getattr(ic_obj, "name", None)
    recipients.append(_medical_director_recipient(insurance_company, ic_obj))

    if _is_erisa_likely(denial):
        recipients.append(_dol_ebsa_recipient())

    return recipients


# Wording in a denial letter that says the insurer has upheld itself once:
# an appeal was decided against the person. Most plans have more than one
# internal level, so on its own this is still a job for the next appeal,
# not a regulator; it is only named back as a supporting reason.
_APPEAL_UPHELD_RE = re.compile(
    r"("
    r"\b(we|has|have|is|are|was|were|been)\s+(also\s+)?uph[eo]ld\b"
    r"|\buph[eo]ld\s+(our|the|its|this|that)\b"
    # "Our original decision" alone is in every first denial's rights notice
    # ("you have the right to appeal our initial determination"), so it
    # counts only with a verb saying it was kept.
    r"|\b(maintain|affirm|reaffirm|uph[eo]ld|stand\s+by)(s|ed|ing)?\s+"
    r"(our|the|its)\s+(original|initial|previous|prior|earlier)\s+"
    r"(decision|determination|denial)\b"
    r"|\bour (original|initial|previous|prior|earlier) "
    r"(decision|determination|denial)\b[^.;:!?\n]{0,60}?"
    r"\b(stands|remains|(is|was|has\s+been|have\s+been)\s+"
    r"(upheld|maintained|affirmed|reaffirmed|unchanged))\b"
    r")",
    re.IGNORECASE | re.UNICODE | re.DOTALL,
)
# Wording that says the internal appeals are done: the insurer's final
# word or appeals exhausted. This is the point where the regulator (and
# external review) is the next step. Deliberately not "external review" or
# "independent review": a first-level denial carries those as a required
# notice of rights, so they say nothing about where this person is in the
# process.
_INTERNAL_APPEALS_DONE_RE = re.compile(
    r"("
    # "Our final decision at this level" is the last word of one level
    # while the letter offers the next, so it does not count.
    r"\bfinal (adverse|internal)? ?(benefit )?(determination|decision)\b"
    r"(?!\s+(at|for|on|of)\s+(this|the\s+first|the\s+current)\s+level\b)"
    r"|\bexhausted\b.{0,40}\bappeal"
    # Appeals before "exhausted", with no negation between them ("your
    # appeals have not been exhausted").
    r"|\bappeals?\b(?:(?!\b(?:not|never|yet)\b|n['\u2019]t\b)[^.;:!?\n]){0,40}"
    r"\bexhausted\b"
    r")",
    re.IGNORECASE | re.UNICODE | re.DOTALL,
)
# A second- or third-level appeal named. On its own that is only a level: a
# rights notice ("you have the right to request a third-level appeal") and
# an acknowledgement ("we received your second level appeal and it is under
# review") name one too, while appeals remain or are pending. So it counts
# only in a sentence that also states a decision on it and says nothing of
# filing, rights or a review still under way (_later_level_decided).
_LATER_LEVEL_RE = re.compile(
    r"\b(second|2nd|third|3rd|level[- ](2|two|ii|3|three|iii))[- ]level\b"
    r"|\blevel[- ](2|two|ii|3|three|iii)\b.{0,20}\bappeal",
    re.IGNORECASE | re.UNICODE | re.DOTALL,
)
_DECISION_RE = re.compile(
    r"\b(uph[eo]ld|denied|decided|decision|determination|stands|unchanged"
    r"|affirmed|reaffirmed|maintained|overturn(ed)?)\b",
    re.IGNORECASE,
)
_PENDING_OR_RIGHTS_RE = re.compile(
    r"\b(right to|you can|to request|request (a|an)|to file|to submit"
    r"|under review|being reviewed|being processed|processing|pending"
    r"|in progress|are reviewing|is reviewing"
    # A decision still to come, not "we will mail you the decision".
    r"|will (be )?(review|decid|consider|issue|make|notify|respond)\w*"
    r"|decision will)\b",
    re.IGNORECASE,
)
# The same words in a clause about what could happen, not what did: the
# rights notice on a first denial says "If our decision is upheld you may
# request an external review", and "once you have exhausted your internal
# appeals". Any of these in the clause before the match disqualifies it.
_CONDITIONAL_CLAUSE_RE = re.compile(
    r"\b(if|should|unless|in the event|may|might|would|will|could"
    r"|once(?!\s+again\b)|(when|after|until)\s+(you|your))\b",
    re.IGNORECASE,
)
_CLAUSE_BREAK_RE = re.compile(r"[.;:!?\n]")
# A whole sentence, for reading a later-level appeal with its decision:
# "Your second-level appeal has concluded; the decision is upheld."
_SENTENCE_BREAK_RE = re.compile(r"[.!?\n]")
# A negation just before the match: "this is not a final decision", "we
# cannot affirm our original decision", "we can't uphold the denial".
# A few small words may sit between: "this is not yet a final decision",
# "this is not considered a final decision".
_NEGATED_RE = re.compile(
    r"\b(not|never|no|cannot|\w+n['\u2019]t)\s+"
    r"(?:(?:a|an|the|our|its|this|that|yet|be|been|considered|deemed)\s+){0,3}$",
    re.IGNORECASE,
)
# How far back a clause is read for a conditional. Longer than any clause
# a letter is likely to hold, short enough that the sentence before cannot
# lend its "if" to this one when the break between them is unpunctuated.
_CLAUSE_LOOKBACK_CHARS = 200


def _own_voice_matches(pattern: re.Pattern, denial_text: str):
    """Each match of *pattern* the letter states as so, not in a
    conditional or negated clause."""
    for match in pattern.finditer(denial_text):
        before = denial_text[
            max(0, match.start() - _CLAUSE_LOOKBACK_CHARS) : match.start()
        ]
        clause = _CLAUSE_BREAK_RE.split(before)[-1]
        if _CONDITIONAL_CLAUSE_RE.search(clause) or _NEGATED_RE.search(clause):
            continue
        yield match


def _says_in_own_voice(pattern: re.Pattern, denial_text: str) -> bool:
    """Whether *pattern* matches somewhere the letter states it as done."""
    return next(_own_voice_matches(pattern, denial_text), None) is not None


def _later_level_decided(denial_text: str) -> bool:
    """Whether the letter states a decision on a second- or third-level
    appeal (see _LATER_LEVEL_RE).

    The decision may sit anywhere in the sentence ("has concluded; the
    decision is upheld"), but only the clause naming the appeal can say it
    is pending or still open, so a rights notice in a later clause ("…;
    you can request an external review") does not cancel the decision.
    """
    sentence_breaks = [m.start() for m in _SENTENCE_BREAK_RE.finditer(denial_text)]
    clause_breaks = [m.start() for m in _CLAUSE_BREAK_RE.finditer(denial_text)]

    def bounds(breaks: List[int], start: int, end: int) -> tuple[int, int]:
        before = bisect.bisect_left(breaks, start)
        after = bisect.bisect_left(breaks, end)
        return (
            breaks[before - 1] + 1 if before else 0,
            breaks[after] if after < len(breaks) else len(denial_text),
        )

    for match in _own_voice_matches(_LATER_LEVEL_RE, denial_text):
        s_start, s_end = bounds(sentence_breaks, match.start(), match.end())
        c_start, c_end = bounds(clause_breaks, match.start(), match.end())
        if _DECISION_RE.search(
            denial_text[s_start:s_end]
        ) and not _PENDING_OR_RIGHTS_RE.search(denial_text[c_start:c_end]):
            return True
    return False


def _appeal_already_decided(denial_text: str) -> bool:
    """Whether the letter says an appeal on this denial was upheld once."""
    return _says_in_own_voice(_APPEAL_UPHELD_RE, denial_text)


def _internal_appeals_done(denial_text: str) -> bool:
    """Whether the letter says the internal appeals are finished (a final
    determination, appeals exhausted, a second- or third-level decision)."""
    return _says_in_own_voice(
        _INTERNAL_APPEALS_DONE_RE, denial_text
    ) or _later_level_decided(denial_text)


# Denial types where the denial itself is the kind of thing a regulator
# looks into, by name (fixture rows are matched by name, never by pk, as
# ml/denial_triage.py does). "Limit for filing expired" is left out: that
# is usually the provider's timely-filing problem, not the insurer's.
# "Preventive Care" is left out too: its fixture regex tags the kind of care
# ("prep", "screening", "STD", "vaccine"...), not anything the plan did wrong,
# so a plain first-level medical-necessity denial of PrEP carried it. The
# ACA preventive-care argument belongs in the appeal (the type's appeal_text
# already puts it there).
_REGULATOR_POLICED_DENIAL_TYPES: dict[str, str] = {
    "Pre-Existing Condition": (
        "Your denial cites a pre-existing condition. Health plans generally "
        "may not deny coverage on that basis, and regulators look into "
        "denials that do."
    ),
    "In-Network Treated as Out of Network": (
        "An in-network provider was billed as out of network. Network "
        "adequacy and out-of-network billing are things regulators police."
    ),
    "Provider Bill (possible balance billing)": (
        "This looks like a balance bill. Surprise and balance billing is "
        "regulated, and regulators take complaints about it."
    ),
}


def regulator_escalation_reasons(denial: Any) -> List[str]:
    """Plain-English reasons a regulator should hear about this denial now.

    A regulator is brought in at two points only: once the internal appeals
    are done (the insurer's final word, a second- or third-level appeal
    decided), or when the care is time-critical and a regulator complaint
    should go out in parallel with the appeal. Each is read from the letter
    and form by the rules here, or from Jev's stored triage answers when
    triage is on, consented to, current and confident. Without one of those this is
    empty, whatever else the letter says, and the appeal pages show the
    regulator-letter callout at its normal size. With one, the other signals
    (a regulator the letter names, an ERISA plan, a denial type regulators
    police, an appeal already upheld once) are added as supporting reasons.

    Each reason is a sentence for the person, in the same restrained voice
    as the recipient rationales above. Read from the row as it is: no model
    call here (the triage was stored earlier), no extra lookups beyond the
    denial's own relations.
    """
    denial_text = getattr(denial, "denial_text", "") or ""
    reasons: List[str] = []

    # Two ways to see each trigger: the rules here, read from the letter and
    # the form, and Jev's triage answers (ml/denial_triage.regulator_signals)
    # when a usable, confident triage is stored. Either one is enough.
    from fighthealthinsurance.ml.denial_triage import regulator_signals

    jev = regulator_signals(denial)

    internal_done = _internal_appeals_done(denial_text) or jev.appeals_done
    if internal_done:
        reasons.append(
            "Your letter reads like the insurer's final decision on your "
            "internal appeals. That is when a regulator complaint helps most."
        )

    if getattr(denial, "urgent", False):
        reasons.append(
            "You marked this care as urgent. A regulator complaint can go out "
            "in parallel with your appeal instead of waiting on it."
        )
    elif jev.urgent:
        reasons.append(
            "Your letter reads like this care is time-critical. A regulator "
            "complaint can go out in parallel with your appeal instead of "
            "waiting on it."
        )

    if not reasons:
        return []

    if not internal_done and _appeal_already_decided(denial_text):
        reasons.append(
            "Your letter reads like an appeal that has already been denied once."
        )

    regulator = getattr(denial, "regulator", None)
    regulator_name = (getattr(regulator, "name", "") or "").strip()
    # Only a regulator the letter names: one the model inferred (no regex
    # match) is a guess about the plan, not a sign from the letter.
    if regulator_name and letter_names_regulator(denial):
        reasons.append(
            f"Your denial letter names {regulator_name}, a regulator that "
            "oversees this kind of plan and takes consumer complaints."
        )

    if _is_erisa_likely(denial):
        reasons.append(
            "This looks like an employer self-funded (ERISA) plan, so the U.S. "
            "Department of Labor also oversees how the appeal is handled."
        )

    denial_type_manager = getattr(denial, "denial_type", None)
    if denial_type_manager is not None and hasattr(denial_type_manager, "all"):
        for denial_type in denial_type_manager.all():
            reason = _REGULATOR_POLICED_DENIAL_TYPES.get(
                getattr(denial_type, "name", "") or ""
            )
            if reason is not None and reason not in reasons:
                reasons.append(reason)

    return reasons

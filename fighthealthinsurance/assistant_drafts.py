"""Letters drafted in the background for an AI assistant to bring back.

The chat path of the MCP server. An assistant starts a draft, the person
agrees to the terms on our site, and AssistantAppealWorkflow
(workflows/assistant_appeal.py) reads the letter, asks our questions, waits
for the answers, and runs the same generation the site uses. The assistant
collects the letters with a random id it holds; only the id's digest is
stored. Nothing here returns letter text to anyone but through
collect_letters, and nothing is kept past expires_at.
"""

import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from loguru import logger
from prometheus_client import Counter

from fighthealthinsurance.denial_context import (
    RESERVED_QA_KEYS,
    _question_text,
    load_qa,
    merge_qa,
    qa_key_for_question,
    question_field_name,
    question_text_for_field,
)
from fighthealthinsurance.models import AssistantDraft, Denial
from fighthealthinsurance.utils import is_real_appeal, strip_invisible_controls

WAITING = "waiting_for_agreement"
READING = "reading"
QUESTIONS = "questions"
DRAFTING = "drafting"
READY = "ready"
ON_SITE = "on_site"
STOPPED = "stopped"
EXPIRED = "expired"
SITE_ONLY = "site_only"
STATUSES = frozenset(
    (WAITING, READING, QUESTIONS, DRAFTING, READY, ON_SITE, STOPPED, EXPIRED, SITE_ONLY)
)

# A draft nobody agrees to goes with its handoff link; an agreed one lives a day.
UNAGREED_TTL = timedelta(hours=2)
DRAFT_TTL = timedelta(hours=24)

MAX_LETTERS = 3
LETTER_MAX_CHARS = 6_000
QUESTION_MAX_CHARS = 300
ANSWER_MAX_CHARS = 1_000
MAX_ANSWERS = 60
MAX_CHOICES = 6
CHOICE_MAX_CHARS = 60
FIELD_MAX_CHARS = 80

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
# What a letter lists for the assistant to fill in (placeholders_in), less
# its citations: {{FIRST_NAME}}, {diagnosis}, a bracket that starts with a
# capital ([Your Name], [DOB: MM/DD/YYYY]) and $your_name_here.
_PLACEHOLDER = re.compile(
    r"\{\{?[A-Za-z_][A-Za-z0-9_ ]*\}\}?|\[[A-Z][^\]\n]{2,40}\]|\$[a-z][a-z_]{2,}\b"
)
# A long bracket that tells the reader what to put there ([Insert Reference
# Number from Denial Letter]) is a fill-in, past _PLACEHOLDER's 41
# characters too: the words are the instruction words of the site's own
# check (letter_placeholders.json), which blocks such a letter at fax time.
_LONG_INSTRUCTION = re.compile(
    r"\[(?=[^\[\]\n]{42,200}\])(?:Insert|Enter|Add|Fill in|Your|Attach|Include"
    r"|Specify|Describe|Explain|Provide|Quote|Mention|List)\b[^\[\]\n]*\]"
)
# What a quotation's note says was left out: [Internal citations omitted].
_OMITTED = r"(?:internal )?(?:citations?|quotation marks|quotations?|footnotes?)"
# The note a quotation carries, any case: [Emphasis added], [Sic].
_QUOTATION_NOTE = (
    r"emphasis (?:added|ours|in (?:the )?original|omitted|supplied|mine)"
    rf"|{_OMITTED}(?:,? and {_OMITTED})* omitted"
    r"|(?:alterations?|brackets) in (?:the )?original"
    r"|cleaned up"
    r"|sic"
)
# Each rule below must match the whole of a bracket's inside, never a part of
# it: a fill-in that mentions a regulation or a year ([USC Specialist's
# Name], [Month, 2018], [Current dose, e.g. 0.125 mg]) stays listed.
#
# [Id.], [Id. at 5], [Ibid., p. 12], [Ibid], and one or more quotation notes:
# [Emphasis added; citations omitted]. A reference number or a list of them
# ([1], [3, 4], [2-5]) needs no rule: _PLACEHOLDER never finds one, as it
# starts with no capital.
_WHOLE_CITATION = re.compile(
    # A locator is a number: [Id. at PAGE_NUMBER] is still to be filled in.
    r"(?:(?:Id|Ibid)\.|Ibid)(?:,?\s*(?:at|pp?\.)\s*\d[\d\-–, ]*)?"
    rf"|(?i:(?:{_QUOTATION_NOTE})(?:(?:\s*[,;]\s*|\s+)(?:and\s+)?(?:{_QUOTATION_NOTE}))*)"
    r"\.?"
)
# A regulation or statute, the whole bracket: [See 42 CFR 438.210], [Title 42
# U.S.C. 300gg-19], [CMS NCD 220.2], [Medicare LCD L33822], [Pub. L.
# 111-148], [ERISA § 503], [Section 438.210], [ACA Section 2719].
_REGULATION = re.compile(
    r"(?:(?:See(?: also)?|Cf\.|Under|Per|Pursuant to)\s+)?(?:Title\s+)?(?:"
    r"\d+\s+(?:CFR|C\.F\.R\.|U\.S\.C\.|USC)\s*§*\s*\d[\w.\-]*(?:\(\w+\))*"
    r"(?:\s*(?:,|and)\s*\d[\w.\-]*(?:\(\w+\))*)*"
    r"|(?:CMS\s+|Medicare\s+)?(?:NCD|LCD)\s+L?\d+(?:\.\d+)*"
    r"|Pub\.\s?L\.\s?(?:No\.\s?)?\d+-\d+"
    r"|(?:[A-Z][A-Za-z]{1,10}\s+)?§{1,2}\s*\d[\w.\-]*(?:\(\w+\))*"
    r"(?:\s*(?:,|and)\s*\d[\w.\-]*(?:\(\w+\))*)*"
    r"|(?:[A-Z]{2,6}\s+)?Sec(?:tion|\.)\s?\d+(?:\.\d+)*(?:\(\w+\))*"
    r")"
)
# A reference marker with a capital: [Reference 1], [Refs. 2-4], [References
# 1, 3 and 5].
_REFERENCE_MARK = re.compile(
    r"Ref(?:erence)?s?\.?\s*\d+(?:\s*(?:,|–|-|and|,\s*and)\s*\d+)*"
)
# Authors, then et al. or a year (or both), maybe a page, the whole bracket:
# [Smith et al.], [Smith 2020a], [Smith and Jones, 2019], [Smith et al., 2020,
# p. 3], [American Diabetes Association (2023)]. An author is a capitalised
# word of letters, so [XX-XX-2026] and [MEMBER_ID_2026] are fill-ins.
# _FILL_IN_WORDS keeps dates and prompts out where there is no et al.:
# [Month, 2018], [Late 2018], [Plan Year 2026], [Your Name, 2026].
# An author's word: letters of any script, with apostrophes, stops and
# hyphens (O'Brien, García, Smith-Jones), never a digit or an underscore.
_AUTHOR_WORD = r"[^\W\d_](?:[^\W\d_]|['’.\-])*"
_ONE_SOURCE = (
    rf"{_AUTHOR_WORD}(?:,?\s+(?:{_AUTHOR_WORD}|and|&|of|for|the))*"
    r"(?:,?\s+et\s+al\.?)?(?:,?\s+\(?(?:19|20)\d\d[a-z]?\)?)?"
    r"(?:,?\s+pp?\.\s*\d[\d\-–]*)?"
)
# One source or several: [Smith et al., 2020; Jones et al., 2021].
_AUTHORS = re.compile(rf"{_ONE_SOURCE}(?:\s*;\s*{_ONE_SOURCE})*")
_AUTHOR_JOINERS = frozenset(("and", "of", "for", "the", "et", "al", "al."))
_HAS_YEAR = re.compile(r"\s\(?(?:19|20)\d\d[a-z]?\)?(?:,?\s+pp?\.\s*\d[\d\-–]*)?$")
_FILL_IN_WORDS = frozenset(
    "jan january feb february mar march apr april may jun june jul july aug "
    "august sep sept september oct october nov november dec december month "
    "day year date dd mm yy yyyy late early mid insert your name signature "
    "spring summer fall autumn winter plan dob birth service approx around "
    "since before after eg".split()
)
# A question that ends with its options in brackets: "(inpatient/outpatient)".
_CHOICES = re.compile(r"\(([^()]+)\)\s*\?\s*$")
_YES_NO_STARTS = frozenset(
    "is are was were do does did has have had can could will would should".split()
)
_DIGEST_LABEL = b"fhi-assistant-draft:"

DRAFTS = Counter(
    "fhi_assistant_drafts_total",
    "Assistant drafts by stage reached",
    ["stage"],
)


class UnknownQuestion(ValueError):
    """An answer named a question this draft never asked."""


class NotWaitingForAnswers(ValueError):
    """The draft has its answers already, or has moved on without them."""


@dataclass(frozen=True)
class NewDraft:
    draft_id: str
    draft: AssistantDraft


def draft_in_chat_enabled() -> bool:
    """On only with every flag the chat path needs: the server, the handoff,
    the Temporal appeal worker that does the drafting, and the payload key."""
    return all(
        bool(getattr(settings, name, False))
        for name in (
            "MCP_DRAFT_IN_CHAT_ENABLED",
            "MCP_SERVER_ENABLED",
            "MCP_PREPARE_APPEAL_ENABLED",
            "TEMPORAL_ENABLED",
            "TEMPORAL_APPEAL_JOURNEY_ENABLED",
            # Encrypts what the chat path puts in Temporal history.
            "TEMPORAL_PAYLOAD_KEY",
        )
    )


def new_draft_id() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def is_draft_id(value: object) -> bool:
    return isinstance(value, str) and _ID_PATTERN.match(value) is not None


def _digest(draft_id: str) -> str:
    return hashlib.sha256(_DIGEST_LABEL + draft_id.encode("ascii")).hexdigest()


def create_draft(
    denial: Optional[Denial], procedure: str = "", condition: str = ""
) -> NewDraft:
    """A new draft, for this denial or, before the person agrees, for none.
    The id is returned once and never stored."""
    draft_id = new_draft_id()
    draft = AssistantDraft.objects.create(
        denial=denial,
        draft_id_digest=_digest(draft_id),
        procedure=(procedure or "")[:FIELD_MAX_CHARS],
        condition=(condition or "")[:FIELD_MAX_CHARS],
        expires_at=timezone.now() + UNAGREED_TTL,
    )
    DRAFTS.labels("created").inc()
    return NewDraft(draft_id=draft_id, draft=draft)


def find_draft(draft_id: object) -> Optional[AssistantDraft]:
    """The live draft an assistant's id names, or None."""
    if not is_draft_id(draft_id):
        return None
    return (
        AssistantDraft.objects.filter(
            draft_id_digest=_digest(str(draft_id)), expires_at__gt=timezone.now()
        )
        .select_related("denial")
        .first()
    )


def draft_for_denial(denial: Denial) -> Optional[AssistantDraft]:
    return denial.assistant_drafts.order_by("-created_at").first()


def set_status(draft: AssistantDraft, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown draft status {status!r}")
    draft.status = status
    draft.status_at = timezone.now()
    draft.save(update_fields=["status", "status_at"])
    DRAFTS.labels(status).inc()


def waiting_draft(pk: object) -> Optional[AssistantDraft]:
    """The live draft a chat link names, while it still waits for agreement."""
    if not isinstance(pk, int) or isinstance(pk, bool):
        return None
    return AssistantDraft.objects.filter(
        pk=pk,
        denial__isnull=True,
        status=WAITING,
        expires_at__gt=timezone.now(),
    ).first()


def agree(
    draft: AssistantDraft,
    denial: Denial,
    procedure: str = "",
    condition: str = "",
    reservation_id: Optional[int] = None,
) -> bool:
    """Tie a waiting draft to the denial the person just agreed for, once,
    with the procedure and condition the link carried (kept only in the
    sealed link until now) and the generation reserved for it. False when
    another request got there first or it moved on."""
    procedure = (procedure or "")[:FIELD_MAX_CHARS]
    condition = (condition or "")[:FIELD_MAX_CHARS]
    linked = AssistantDraft.objects.filter(
        pk=draft.pk,
        denial__isnull=True,
        status=WAITING,
        expires_at__gt=timezone.now(),
    ).update(
        denial=denial,
        procedure=procedure,
        condition=condition,
        spend_reservation_id=reservation_id,
    )
    if linked != 1:
        return False
    draft.denial = denial
    draft.procedure = procedure
    draft.condition = condition
    draft.spend_reservation_id = reservation_id
    mark_agreed(draft)
    return True


# Run endings with no letters; on_site is not one (see give_back_generation).
GIVES_BACK = frozenset((STOPPED, EXPIRED))


class ReleaseFailed(RuntimeError):
    """The reserved generation could not be given back; try again."""


def generation_delivered(denial: Optional[Denial]) -> bool:
    """Whether this denial got a generation that counts: the site's page
    took one (from the continue link, say), or letters are stored, a
    chosen copy included."""
    if denial is None:
        return False
    return (
        site_took_generation(denial)
        or bool(collect_letters(denial))
        or _chosen_letter_stored(denial)
    )


def _chosen_letter_stored(denial: Denial) -> bool:
    """Whether a real chosen letter is stored; collect_letters leaves these
    out because they copy a letter already shown."""
    from fighthealthinsurance.common_view_logic import (
        appeal_replay_queryset,
        deliverable_candidates,
    )

    rows = deliverable_candidates(appeal_replay_queryset(denial).filter(chosen=True))
    return any(
        is_real_appeal(text) for text in rows.values_list("appeal_text", flat=True)
    )


def give_back_generation(draft: AssistantDraft) -> bool:
    """Give back the generation reserved when the person agreed, once and to
    the day it was taken from (spend.release_generation). For a run that
    ended without letters. Kept when the site's page generated for this
    denial or letters are stored: an assistant denial's generation spends
    the assistant budget wherever it runs. False when there is none, it is
    kept, or it went back already; ReleaseFailed when the database would
    not take the release."""
    from fighthealthinsurance.ml import spend
    from fighthealthinsurance.models import SpendReservation

    if draft.spend_reservation_id is None:
        return False
    row = SpendReservation.objects.filter(pk=draft.spend_reservation_id).first()
    if row is None or row.released_at is not None:
        return False
    if generation_delivered(draft.denial):
        return False
    if spend.release_generation(spend.Reservation(id=row.pk, day=row.day)):
        return True
    if SpendReservation.objects.filter(pk=row.pk, released_at__isnull=True).exists():
        raise ReleaseFailed("the reserved generation was not given back")
    return False


def finish_on_site(draft: Optional[AssistantDraft]) -> None:
    """The person chose the site's own form: the assistant sees on_site."""
    if draft is not None:
        set_status(draft, ON_SITE)


def mark_agreed(draft: AssistantDraft) -> None:
    """The person accepted the terms: the draft now lives a day."""
    draft.expires_at = timezone.now() + DRAFT_TTL
    draft.status = READING
    draft.status_at = timezone.now()
    draft.save(update_fields=["expires_at", "status", "status_at"])
    DRAFTS.labels("agreed").inc()


def _choices(question: str) -> list[str]:
    match = _CHOICES.search(question)
    if match is None:
        return []
    options = [o.strip() for o in re.split(r"/|,|\bor\b", match.group(1))]
    options = [o for o in options if o]
    if not 2 <= len(options) <= MAX_CHOICES:
        return []
    if any(len(o) > CHOICE_MAX_CHARS for o in options):
        return []
    return options


def _kind(question: str, choices: list[str]) -> str:
    if choices:
        return "choice"
    first = question.split(" ", 1)[0].lower().strip("?:,.")
    # "Was it inpatient or outpatient?" is not answered with yes or no.
    if first in _YES_NO_STARTS and " or " not in question.lower():
        return "yes_no"
    return "text"


def clean_questions(rows: Any) -> list[dict[str, Any]]:
    """The questions the assistant may ask, from generated_questions rows.

    Each is {name, kind, label, choices}: the name is the one the site's
    form uses, the label is cut at QUESTION_MAX_CHARS, and a question with
    a URL or a key the review step owns is left out. The suggested answer
    stays out too: assistants answer with whatever hint they are given.
    Invisible controls come out of the label, never out of the name.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows or []:
        question = _question_text(row)
        if question is None or question in RESERVED_QA_KEYS:
            continue
        label = strip_invisible_controls(question).strip()
        if not label or _URL.search(label):
            continue
        name = question_field_name(question)
        if name in seen:
            continue
        seen.add(name)
        choices = _choices(label)
        out.append(
            {
                "name": name,
                "kind": _kind(label, choices),
                "label": label[:QUESTION_MAX_CHARS],
                "choices": choices,
            }
        )
    return out


def _checked_value(question: dict[str, Any], value: str) -> Optional[str]:
    """The answer to file, None for a skip, ValueError when it does not fit."""
    if not value or value.lower() == "skip":
        return None
    name = question["name"]
    if question["kind"] == "yes_no":
        if value.lower() not in ("yes", "no"):
            raise ValueError(f"{name}: answer yes, no or skip")
        return value.capitalize()
    if question["kind"] == "choice":
        by_lower = {c.lower(): c for c in question.get("choices") or []}
        if value.lower() not in by_lower:
            raise ValueError(f"{name}: answer one of the choices, or skip")
        return str(by_lower[value.lower()])
    return value


def file_answers(draft: AssistantDraft, answers: Any) -> int:
    """File the person's answers the way the site's questions page does.

    Only while the draft is waiting for answers, once, and only under names
    this draft issued; anything else is refused by name before anything is
    written. "skip" files nothing. Returns how many were filed.
    """
    if not isinstance(answers, list) or len(answers) > MAX_ANSWERS:
        raise ValueError(f"answers must be a list of at most {MAX_ANSWERS}")
    with transaction.atomic():
        # Locked and reloaded: a stale copy must not file late or overwrite.
        current = (
            AssistantDraft.objects.select_for_update()
            .filter(pk=draft.pk, expires_at__gt=timezone.now())
            .first()
        )
        if (
            current is None
            or current.status != QUESTIONS
            or current.denial_id is None
            or current.answers_at is not None
        ):
            raise NotWaitingForAnswers("this draft is not waiting for answers")
        denial = Denial.objects.select_for_update().get(pk=current.denial_id)
        return _file_answers(current, denial, answers)


def _file_answers(draft: AssistantDraft, denial: Denial, answers: list) -> int:
    issued = {q["name"]: q for q in draft.questions or []}
    texts: dict[str, Optional[str]] = {
        name: question_text_for_field(name, denial.generated_questions)
        for name in issued
    }
    askable = {
        name
        for name, text in texts.items()
        if text is not None and text not in RESERVED_QA_KEYS
    }
    names = [a.get("name") if isinstance(a, dict) else None for a in answers]
    unknown = sorted({str(n) for n in names if n not in askable})
    if unknown:
        raise UnknownQuestion(f"not asked: {', '.join(unknown)}")
    updates: dict[str, str] = {}
    for answer in answers:
        name = answer["name"]
        value = answer.get("value")
        if not isinstance(value, str):
            raise ValueError(f"{name}: the answer must be text")
        checked = _checked_value(
            issued[name],
            " ".join(strip_invisible_controls(value).split())[:ANSWER_MAX_CHARS],
        )
        if checked is not None:
            updates[qa_key_for_question(str(texts[name]))] = checked
    merge_qa(denial, updates, source="assistant_answers")
    denial.save(update_fields=["qa_context"])
    draft.answers_at = timezone.now()
    draft.save(update_fields=["answers_at"])
    DRAFTS.labels("answered").inc()
    return len(updates)


def _is_one_source(source: str) -> bool:
    source = source.strip()
    # Every author's word is capitalised, the year and page aside: [NPI,
    # e.g. 2026] is a fill-in.
    names = re.sub(r"\(?(?:19|20)\d\d[a-z]?\)?|pp?\.\s*\d[\d\-–]*", " ", source)
    for word in re.findall(_AUTHOR_WORD, names):
        if word not in _AUTHOR_JOINERS and not word[0].isupper():
            return False
    if re.search(r"\bet\s+al\b", source):
        # No fill-in says et al.: [May et al., 2020] is a citation.
        return True
    if any(w in _FILL_IN_WORDS for w in re.findall(r"[^\W\d_]+", source.lower())):
        return False
    return bool(_HAS_YEAR.search(source))


def _is_authors(inside: str) -> bool:
    if not _AUTHORS.fullmatch(inside):
        return False
    return all(_is_one_source(source) for source in inside.split(";"))


def _is_citation(bracket: str) -> bool:
    """Whether a bracket _PLACEHOLDER found is, as a whole, a citation or a
    quotation's note, which the assistant has nothing to fill in for."""
    inside = bracket[1:-1].strip()
    if "[" in inside or _PLACEHOLDER.search(inside):
        # A fill-in may be in there ([See [Your Name], [Cite {{YEAR}}]), and
        # it is listed only as part of this bracket, so the bracket stays.
        return False
    return bool(
        _WHOLE_CITATION.fullmatch(inside)
        or _REGULATION.fullmatch(inside)
        or _REFERENCE_MARK.fullmatch(inside)
        or _is_authors(inside)
    )


def placeholders_in(text: str) -> list[str]:
    """The fill-ins left in a letter, each once, sorted, each exactly as the
    letter has it.

    What main has always listed (_PLACEHOLDER, read left to right), less
    the brackets that are clearly citations or a quotation's notes, and
    nothing else. Listed: {{FIRST_NAME}}, {diagnosis}, $your_name_here and
    a bracket that starts with a capital, whatever else is in it ([Dr.
    Name], [ICD-10 Code], [Claim Number: ], [DOB: MM/DD/YYYY], [Physician
    Name, M.D.], [Your {{FIRST_NAME}}]). Each is a piece of the letter
    itself. A {{...}} or $name inside a listed bracket is not listed again,
    nor is a bracket opened inside one: a bracket runs to the first "]", so
    "Ref [Dear [Your Name] Sir]" lists "[Dear [Your Name]" and not "[Your
    Name]" as well. Never listed, as on main: [It], [doctor name], [1], [3,
    4], [42 CFR 438.210], or a bracket with over 41 characters inside
    unless it starts with an instruction ([Insert Reference Number from
    Denial Letter]), as the site's own fill-in check lists it.

    Left out, only when the whole bracket is one: a regulation or statute
    ([See 42 CFR 438.210], [CMS NCD 220.2], [Medicare LCD L33822], [Pub. L.
    111-148], [ERISA § 503], [Section 438.210], [Section 2.1]); a reference
    marker ([Reference 1], [Refs. 2-4]); authors with et al. or a year
    ([Smith et al.], [Smith 2020], [Smith and Jones, 2019a]); a
    quotation's notes ([Emphasis added], [Emphasis ours], [Internal
    citations omitted], [Footnotes and citations omitted], [Alterations in
    original], [Brackets in original], [Cleaned up], [Sic]); [Id.] and
    [Ibid]. A bracket with a fill-in inside it is never one (_is_citation).

    [Exhibit A], [Attachment B] and [Appendix C] stay listed, as main
    listed them: a template uses the same words for an attachment still to
    be named. Listing one that names a real attachment costs the assistant
    a question; leaving out one still to be named leaves a blank in the
    letter.
    """
    listed = {
        found
        for found in _PLACEHOLDER.findall(text)
        if not (found.startswith("[") and _is_citation(found))
    }
    return sorted(listed | set(_LONG_INSTRUCTION.findall(text)))


def collect_letters(denial: Denial) -> list[dict[str, Any]]:
    """The letters the site would show, as the assistant gets them.

    The rows the appeal page replays, less the chosen copies: real text,
    each distinct text once, with the denial's own values substituted,
    newest first, at most three, each without invisible controls and cut at
    LETTER_MAX_CHARS with the placeholders still in it listed.
    """
    from fighthealthinsurance.appeal_fingerprints import fingerprint_text
    from fighthealthinsurance.common_view_logic import (
        appeal_replay_queryset,
        deliverable_candidates,
        substitute_appeal_fields,
    )

    rows = deliverable_candidates(appeal_replay_queryset(denial).filter(chosen=False))
    letters: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text in rows.values_list("appeal_text", flat=True):
        if not is_real_appeal(text):
            continue
        fingerprint = fingerprint_text(text) or text.strip()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        content = strip_invisible_controls(substitute_appeal_fields(denial, text))
        cut = len(content) > LETTER_MAX_CHARS
        content = content[:LETTER_MAX_CHARS]
        letters.append(
            {
                "text": content,
                "placeholders": placeholders_in(content),
                "cut_short": cut,
            }
        )
        if len(letters) >= MAX_LETTERS:
            break
    return letters


def letters_status(denial: Denial, finished: bool) -> str:
    """ready with three letters, or with any once the run is over; stopped
    when a finished run left none; drafting otherwise."""
    count = len(collect_letters(denial))
    if count >= MAX_LETTERS or (finished and count):
        return READY
    return STOPPED if finished else DRAFTING


def site_took_generation(denial: Denial) -> bool:
    """Whether the last generation lease on this denial went to the site's
    own page rather than to a background run."""
    from fighthealthinsurance.models import AppealGenerationLease

    holder = (
        AppealGenerationLease.objects.filter(for_denial=denial)
        .values_list("holder", flat=True)
        .first()
    )
    return bool(holder and holder.startswith("interactive:"))


def answers_for_generation(denial: Denial) -> Optional[dict[str, str]]:
    """What a background generation sends as the questionnaire: the
    denial's stored answers, once the assistant has filed some."""
    if not draft_in_chat_enabled():
        return None
    if not AssistantDraft.objects.filter(
        denial=denial, answers_at__isnull=False
    ).exists():
        return None
    return load_qa(denial)


def sweep_expired(now: Optional[datetime] = None) -> int:
    """Delete every draft past its expiry. Returns how many went. A draft
    that never reached drafting gives its generation back first, as its run
    can no longer find it to say expired. One whose release fails stays for
    the next sweep."""
    expired = AssistantDraft.objects.filter(expires_at__lte=now or timezone.now())
    kept: list[int] = []
    for draft in expired.select_related("denial").filter(
        spend_reservation__isnull=False,
        spend_reservation__released_at__isnull=True,
        status__in=(READING, QUESTIONS, *GIVES_BACK),
    ):
        try:
            give_back_generation(draft)
        except ReleaseFailed:
            logger.warning(
                f"assistant drafts: generation not given back for {draft.pk}"
            )
            kept.append(draft.pk)
    deleted, _ = expired.exclude(pk__in=kept).delete()
    if deleted:
        DRAFTS.labels("swept").inc(deleted)
        logger.info(f"assistant drafts: swept {deleted}")
    return deleted

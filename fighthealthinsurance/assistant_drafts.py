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
from fighthealthinsurance.utils import is_real_appeal

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
_PLACEHOLDER = re.compile(
    r"\{\{?[A-Za-z_][A-Za-z0-9_ ]*\}\}?|\[[A-Z][^\]\n]{2,40}\]|\$[a-z][a-z_]{2,}\b"
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


def agree(draft: AssistantDraft, denial: Denial) -> bool:
    """Tie a waiting draft to the denial the person just agreed for, once.
    False when another request got there first or it moved on."""
    linked = AssistantDraft.objects.filter(
        pk=draft.pk,
        denial__isnull=True,
        status=WAITING,
        expires_at__gt=timezone.now(),
    ).update(denial=denial)
    if linked != 1:
        return False
    draft.denial = denial
    mark_agreed(draft)
    return True


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
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows or []:
        question = _question_text(row)
        if question is None or question in RESERVED_QA_KEYS or _URL.search(question):
            continue
        name = question_field_name(question)
        if name in seen:
            continue
        seen.add(name)
        choices = _choices(question)
        out.append(
            {
                "name": name,
                "kind": _kind(question, choices),
                "label": question[:QUESTION_MAX_CHARS],
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

    Only while the draft is waiting for answers, and only under names this
    draft issued; anything else is refused by name before anything is
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
        if current is None or current.status != QUESTIONS or current.denial_id is None:
            raise ValueError("this draft is not waiting for answers")
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
            issued[name], " ".join(value.split())[:ANSWER_MAX_CHARS]
        )
        if checked is not None:
            updates[qa_key_for_question(str(texts[name]))] = checked
    merge_qa(denial, updates, source="assistant_answers")
    denial.save(update_fields=["qa_context"])
    draft.answers_at = timezone.now()
    draft.save(update_fields=["answers_at"])
    DRAFTS.labels("answered").inc()
    return len(updates)


def collect_letters(denial: Denial) -> list[dict[str, Any]]:
    """The letters the site would show, as the assistant gets them.

    The rows the appeal page replays, less the chosen copies: real text,
    each distinct text once, with the denial's own values substituted,
    newest first, at most three, each cut at LETTER_MAX_CHARS with the
    placeholders still in it listed.
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
        content = substitute_appeal_fields(denial, text)
        cut = len(content) > LETTER_MAX_CHARS
        content = content[:LETTER_MAX_CHARS]
        letters.append(
            {
                "text": content,
                "placeholders": sorted(set(_PLACEHOLDER.findall(content))),
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
    """Delete every draft past its expiry. Returns how many went."""
    deleted, _ = AssistantDraft.objects.filter(
        expires_at__lte=now or timezone.now()
    ).delete()
    if deleted:
        DRAFTS.labels("swept").inc(deleted)
        logger.info(f"assistant drafts: swept {deleted}")
    return deleted

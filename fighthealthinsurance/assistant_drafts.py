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
from django.db.models import F
from django.utils import timezone
from loguru import logger
from prometheus_client import Counter

from fighthealthinsurance.denial_context import (
    RESERVED_QA_KEYS,
    _question_text,
    merge_qa,
    qa_key_for_question,
    question_field_name,
)
from fighthealthinsurance.models import AssistantDraft, Denial, ProposedAppeal
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

# A draft nobody agrees to goes with its handoff link; an agreed one lives a day.
UNAGREED_TTL = timedelta(hours=2)
DRAFT_TTL = timedelta(hours=24)

MAX_LETTERS = 3
LETTER_MAX_CHARS = 6_000
QUESTION_MAX_CHARS = 300
ANSWER_MAX_CHARS = 1_000
MAX_ANSWERS = 60
FIELD_MAX_CHARS = 80

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"\{\{?[A-Za-z_][A-Za-z0-9_ ]*\}\}?|\[[A-Z][^\]\n]{2,40}\]")
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
    and the Temporal appeal worker that does the drafting."""
    return all(
        bool(getattr(settings, name, False))
        for name in (
            "MCP_DRAFT_IN_CHAT_ENABLED",
            "MCP_SERVER_ENABLED",
            "MCP_PREPARE_APPEAL_ENABLED",
            "TEMPORAL_ENABLED",
            "TEMPORAL_APPEAL_JOURNEY_ENABLED",
        )
    )


def new_draft_id() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def is_draft_id(value: object) -> bool:
    return isinstance(value, str) and _ID_PATTERN.match(value) is not None


def _digest(draft_id: str) -> str:
    return hashlib.sha256(_DIGEST_LABEL + draft_id.encode("ascii")).hexdigest()


def create_draft(denial: Denial, procedure: str = "", condition: str = "") -> NewDraft:
    """A new draft for this denial. The id is returned once and never stored."""
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
    draft.status = status
    draft.status_at = timezone.now()
    draft.save(update_fields=["status", "status_at"])
    DRAFTS.labels(status).inc()


def mark_agreed(draft: AssistantDraft) -> None:
    """The person accepted the terms: the draft now lives a day."""
    draft.expires_at = timezone.now() + DRAFT_TTL
    draft.status = READING
    draft.status_at = timezone.now()
    draft.save(update_fields=["expires_at", "status", "status_at"])
    DRAFTS.labels("agreed").inc()


def clean_questions(rows: Any) -> list[dict[str, Any]]:
    """The questions the assistant may ask, from generated_questions rows.

    Short, URL-free, never a key the review step owns, each once. The
    suggested answer stays out: it reads as a hint and assistants answer
    with it.
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
        first = question.split(" ", 1)[0].lower().strip("?:,.")
        out.append(
            {
                "name": name,
                "kind": "yes_no" if first in _YES_NO_STARTS else "text",
                "label": question[:QUESTION_MAX_CHARS],
                "question": question,
            }
        )
    return out


def file_answers(draft: AssistantDraft, answers: Any) -> int:
    """File the person's answers under the questions this draft asked.

    Only names the draft issued; anything else is refused by name before
    anything is written. "skip" files nothing. Returns how many were filed.
    """
    if not isinstance(answers, list) or len(answers) > MAX_ANSWERS:
        raise ValueError(f"answers must be a list of at most {MAX_ANSWERS}")
    issued = {q["name"]: q for q in draft.questions or []}
    names = [a.get("name") if isinstance(a, dict) else None for a in answers]
    unknown = sorted({str(n) for n in names if n not in issued})
    if unknown:
        raise UnknownQuestion(f"not asked: {', '.join(unknown)}")
    updates: dict[str, str] = {}
    for answer in answers:
        question = issued[answer["name"]]
        value = answer.get("value")
        if not isinstance(value, str):
            raise ValueError(f"{answer['name']}: the answer must be text")
        value = " ".join(value.split())[:ANSWER_MAX_CHARS]
        if not value or value.lower() == "skip":
            continue
        if question["kind"] == "yes_no" and value.lower() not in ("yes", "no"):
            raise ValueError(f"{answer['name']}: answer yes, no or skip")
        updates[qa_key_for_question(question["question"])] = value
    denial = draft.denial
    merge_qa(denial, updates, source="assistant_answers")
    denial.save(update_fields=["qa_context"])
    draft.answers_at = timezone.now()
    draft.save(update_fields=["answers_at"])
    DRAFTS.labels("answered").inc()
    return len(updates)


def collect_letters(denial: Denial) -> list[dict[str, Any]]:
    """The letters the site would show, as the assistant gets them.

    The same rows the appeal page replays: not speculative, not chosen, not
    a reserve built for another state, real text, each distinct text once,
    with the denial's own values substituted, newest first, at most three,
    each cut at LETTER_MAX_CHARS with its placeholders listed.
    """
    from fighthealthinsurance.appeal_fingerprints import fingerprint_text
    from fighthealthinsurance.common_view_logic import (
        deliverable_candidates,
        served_reserve_for_another_state,
        substitute_appeal_fields,
    )

    rows = deliverable_candidates(
        ProposedAppeal.objects.filter(
            for_denial=denial, speculative=False, chosen=False
        )
        .exclude(served_reserve_for_another_state())
        .order_by(F("created_at").desc(nulls_last=True), "-id")
    )
    letters: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text in rows.values_list("appeal_text", flat=True):
        if not is_real_appeal(text):
            continue
        fingerprint = fingerprint_text(text)
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


def sweep_expired(now: Optional[datetime] = None) -> int:
    """Delete every draft past its expiry. Returns how many went."""
    deleted, _ = AssistantDraft.objects.filter(
        expires_at__lte=now or timezone.now()
    ).delete()
    if deleted:
        DRAFTS.labels("swept").inc(deleted)
        logger.info(f"assistant drafts: swept {deleted}")
    return deleted

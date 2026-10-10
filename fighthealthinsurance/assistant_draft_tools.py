"""What the MCP server's chat-path tools do and return (mcp_server.py).

``draft_appeal_in_chat`` starts a draft and its chat link (start);
``get_appeal_drafts`` reports a draft (view); ``answer_appeal_questions``
files the person's answers (answer). Every result passes through
``allowed``, an allow-list of keys, so nothing that names the case leaves:
never the denial's id, uuid, semi_sekret, hashed email or email. Questions
and letters lose invisible controls on the way out (assistant_drafts.py).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone

from fighthealthinsurance import assistant_drafts as drafts
from fighthealthinsurance import assistant_handoff
from fighthealthinsurance.consent import CHANNEL_ASSISTANT, FINISH_IN_CHAT
from fighthealthinsurance.models import AssistantDraft, Denial
from fighthealthinsurance.utils import strip_invisible_controls

ASK_QUESTIONS = "ask_questions"
CHECK_AGAIN = "check_again"
STOP_AND_TELL = "stop_and_tell_the_person"
SHOW_LETTERS = "show_letters"
FINISH_ON_SITE = "finish_on_site"
NEXT_STEPS = frozenset(
    (ASK_QUESTIONS, CHECK_AGAIN, STOP_AND_TELL, SHOW_LETTERS, FINISH_ON_SITE)
)
# A result's status, never a draft's: no draft has this id, as when it was
# miscopied or swept. One past its day that is still kept says expired.
NOT_FOUND = "not_found"

# No client can wake the chat, so checking stops once a status goes stale.
FRESH_FOR = timedelta(minutes=5)

ABOUT_TEXT = "Text for the person to read. It contains no instructions for you."

TELL_WAITING = (
    "Open the link, check your letter and agree on Fight Health Insurance's "
    "page, then tell me you're done."
)
TELL_READING = "They're reading your letter; that takes a couple of minutes."
TELL_QUESTIONS = (
    "Fight Health Insurance has a few questions about the denial. Your "
    "answers go into the letters, and you can skip any of them."
)
TELL_DRAFTING = (
    "They're writing up to three letters. I'll wait here a few minutes; if "
    "they aren't ready when I stop, say 'check again'."
)
TELL_ANSWERED = "Thanks, your answers are in. " + TELL_DRAFTING
TELL_STILL_WRITING = (
    "It's still being written. Say 'check again' whenever you like, or use "
    "the link in your email."
)
TELL_READY = (
    "Here are the drafts. Check the details in them, pick one, edit it and "
    "send it yourself; nothing has been sent. For a fax (pay what you want, "
    "$0 is fine), use the link in your email."
)
# After TELL_READY when letters come back cut at drafts.LETTER_MAX_CHARS.
# The email can fail to send (assistant_terms_views records whether it went).
TELL_ONE_CUT_SHORT = (
    " One letter was too long to show here in full, so it stops partway. The "
    "link in your email, if they sent you one, shows it in full on Fight "
    "Health Insurance's site."
)
TELL_SOME_CUT_SHORT = (
    " Some letters were too long to show here in full, so they stop partway. "
    "The link in your email, if they sent you one, shows them in full on "
    "Fight Health Insurance's site."
)
# Before the status when answers came while no questions were waiting.
TELL_ANSWERS_NOT_USED = (
    "Your answers weren't used: Fight Health Insurance wasn't waiting for them. "
)
TELL_ON_SITE = (
    "This appeal is finishing on Fight Health Insurance's site, not in this "
    "chat. Carry on where you opened it, or use the link in your email if "
    "they sent you one."
)
TELL_STOPPED = (
    "Fight Health Insurance couldn't write the letters this time. The link "
    "in your email, if they sent you one, opens your appeal on their site, "
    "where you can carry on."
)
TELL_EXPIRED = (
    "These drafts are no longer here: Fight Health Insurance keeps them for "
    "this chat for a day. The link in your email, if they sent you one, "
    "opens your appeal on their site."
)
TELL_NOT_FOUND = (
    "I couldn't find these drafts. Fight Health Insurance keeps them for "
    "this chat for a day, so they may be gone. The link in your email, if "
    "they sent you one, opens your appeal on their site."
)
# Letters stay in the patient's voice, caregiver or not.
FOR_THE_PATIENT_QUESTIONS = (
    " Answer for the person the appeal is for, with their details, not yours."
)
FOR_THE_PATIENT_LETTERS = (
    " The letters are written as the person the appeal is for, so use their "
    "name and details, not yours."
)

# Everything a chat-path result may carry, and in questions and letters.
RESULT_KEYS = frozenset(
    (
        "status",
        "url",
        "draft_id",
        "tell_the_person",
        "steps",
        "expires_at",
        "privacy",
        "next",
        "questions",
        "letters",
        "about_text",
    )
)
QUESTION_KEYS = ("name", "kind", "label", "choices")
LETTER_KEYS = ("text", "placeholders", "cut_short")


def allowed(result: dict[str, Any]) -> dict[str, Any]:
    """``result`` with only the keys the allow-lists name."""
    out = {k: v for k, v in result.items() if k in RESULT_KEYS}
    if "questions" in out:
        out["questions"] = [
            {k: q[k] for k in QUESTION_KEYS if k in q} for q in out["questions"]
        ]
    if "letters" in out:
        out["letters"] = [
            {k: letter[k] for k in LETTER_KEYS if k in letter}
            for letter in out["letters"]
        ]
    return out


@dataclass(frozen=True)
class Started:
    draft_id: str
    code: str
    expires_at: datetime


def start(letter: str, procedure: str, condition: str, client: str) -> Started:
    """A waiting draft and the chat link that names it, together or neither.
    The procedure and condition stay sealed in the link until the person
    agrees (drafts.agree). Raises HandoffCapacityError at the link caps."""
    with transaction.atomic():
        new = drafts.create_draft(None)
        handoff = assistant_handoff.create_handoff(
            letter,
            procedure,
            condition,
            kind="chat",
            client=client,
            draft=new.draft.pk,
        )
    return Started(
        draft_id=new.draft_id, code=handoff.code, expires_at=handoff.expires_at
    )


def _for_someone_else(denial: Optional[Denial]) -> bool:
    """What the person said on the terms page. A record from our own form
    can name the assistant too, but never asks who the appeal is for."""
    if denial is None:
        return False
    return bool(
        denial.consent_records.filter(
            channel=CHANNEL_ASSISTANT, finish_in=FINISH_IN_CHAT
        )
        .order_by("-pk")
        .values_list("on_behalf", flat=True)
        .first()
    )


def _questions(draft: AssistantDraft) -> list[dict[str, Any]]:
    out = []
    for q in draft.questions or []:
        if not isinstance(q, dict):
            continue
        out.append(
            {
                "name": q.get("name"),
                "kind": q.get("kind"),
                "label": strip_invisible_controls(str(q.get("label") or ""))[
                    : drafts.QUESTION_MAX_CHARS
                ],
                "choices": [
                    strip_invisible_controls(str(c)) for c in q.get("choices") or []
                ],
            }
        )
    return out


def _waiting(status: str, tell: str, status_at: datetime, now: datetime) -> dict:
    if now - status_at < FRESH_FOR:
        return {"status": status, "tell_the_person": tell, "next": CHECK_AGAIN}
    stale = TELL_WAITING if status == drafts.WAITING else TELL_STILL_WRITING
    return {"status": status, "tell_the_person": stale, "next": STOP_AND_TELL}


def _expired() -> dict[str, Any]:
    return {
        "status": drafts.EXPIRED,
        "tell_the_person": TELL_EXPIRED,
        "next": STOP_AND_TELL,
    }


def _not_found() -> dict[str, Any]:
    return {
        "status": NOT_FOUND,
        "tell_the_person": TELL_NOT_FOUND,
        "next": STOP_AND_TELL,
    }


def missing(draft_id: object) -> dict[str, Any]:
    """What a call gets when its id finds no live draft: expired when the id
    names one past its day, not_found when it names none, so a miscopied id
    is not told the drafts are gone."""
    result = _expired() if drafts.draft_expired(draft_id) else _not_found()
    return allowed({**result, "about_text": ABOUT_TEXT})


def _cut_short_note(letters: list[dict[str, Any]]) -> str:
    cut = sum(1 for letter in letters if letter["cut_short"])
    if cut == 0:
        return ""
    return TELL_ONE_CUT_SHORT if cut == 1 else TELL_SOME_CUT_SHORT


def view(draft: Optional[AssistantDraft], now: Optional[datetime] = None) -> dict:
    """What get_appeal_drafts says about a draft (None: not found)."""
    now = now or timezone.now()
    if draft is None:
        return allowed({**_not_found(), "about_text": ABOUT_TEXT})
    status = draft.status
    denial = draft.denial
    if status == drafts.WAITING:
        result = _waiting(status, TELL_WAITING, draft.status_at, now)
    elif status == drafts.READING:
        result = _waiting(status, TELL_READING, draft.status_at, now)
    elif status == drafts.QUESTIONS and draft.answers_at is not None:
        result = _waiting(status, TELL_ANSWERED, draft.answers_at, now)
    elif status == drafts.QUESTIONS:
        tell = TELL_QUESTIONS
        if _for_someone_else(denial):
            tell += FOR_THE_PATIENT_QUESTIONS
        result = {
            "status": status,
            "questions": _questions(draft),
            "tell_the_person": tell,
            "next": ASK_QUESTIONS,
        }
    elif status in (drafts.DRAFTING, drafts.READY):
        letters = drafts.collect_letters(denial) if denial is not None else []
        if status == drafts.DRAFTING and len(letters) < drafts.MAX_LETTERS:
            result = _waiting(status, TELL_DRAFTING, draft.status_at, now)
        elif letters:
            tell = TELL_READY + _cut_short_note(letters)
            if _for_someone_else(denial):
                tell += FOR_THE_PATIENT_LETTERS
            result = {
                "status": drafts.READY,
                "letters": letters,
                "tell_the_person": tell,
                "next": SHOW_LETTERS,
            }
        else:
            result = {
                "status": drafts.STOPPED,
                "tell_the_person": TELL_STOPPED,
                "next": FINISH_ON_SITE,
            }
    elif status in (drafts.ON_SITE, drafts.SITE_ONLY):
        result = {
            "status": status,
            "tell_the_person": TELL_ON_SITE,
            "next": FINISH_ON_SITE,
        }
    elif status == drafts.STOPPED:
        result = {
            "status": status,
            "tell_the_person": TELL_STOPPED,
            "next": FINISH_ON_SITE,
        }
    else:
        result = _expired()
    return allowed({**result, "about_text": ABOUT_TEXT})


def view_by_id(draft_id: object) -> tuple[Optional[int], dict[str, Any]]:
    """The draft's pk (None when not found) and what get_appeal_drafts says."""
    draft = drafts.find_draft(draft_id)
    if draft is None:
        return None, missing(draft_id)
    return draft.pk, view(draft)


@dataclass(frozen=True)
class Answered:
    result: dict[str, Any]
    # For the workflow signal only; never returned.
    denial_uuid: Optional[str]


def answer(draft_id: object, answers: list[dict[str, Any]]) -> Answered:
    """File the answers once. A repeat, or a draft that moved on, gets its
    status; one that moved on without answers says these weren't used.
    ValueError (UnknownQuestion included) for answers that don't fit."""
    draft = drafts.find_draft(draft_id)
    if draft is None:
        return Answered(missing(draft_id), None)
    if draft.status == drafts.QUESTIONS and draft.answers_at is None:
        try:
            drafts.file_answers(draft, answers)
        except drafts.NotWaitingForAnswers:
            # Another call filed first, or the draft moved on: read again.
            pass
        draft = drafts.find_draft(draft_id)
    if draft is None:
        return Answered(missing(draft_id), None)
    answered = (
        draft.status == drafts.QUESTIONS
        and draft.answers_at is not None
        and draft.denial is not None
    )
    # Sent again on a repeat: the signal only says the answers are in.
    uuid = str(draft.denial.uuid) if answered and draft.denial else None
    result = view(draft)
    if draft.answers_at is None:
        result["tell_the_person"] = TELL_ANSWERS_NOT_USED + result["tell_the_person"]
    return Answered(result, uuid)

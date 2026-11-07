"""The company behind a shared plan document, read by our own model.

Plan documents name their sponsor in many ways ("Plan Sponsor: ...", "This
plan is sponsored by ...", a title, a table in the back), so fixed patterns
miss some and misread others (plan_sponsors.identify_sponsor, kept as the
fallback). Here one of our internal models reads the parts of the document
that name parties and answers in JSON: the employer that sponsors the plan,
its EIN, and the line that says so.

Nothing it says is taken on trust. An answer is used only when:

* the line it quotes is in the document, word for word (spacing and case
  aside), and names the company in full;
* the name is a company, not an insurer from our catalog or a phrase;
* an EIN is nine digits and sits in that line or just after it, before
  another party is named (otherwise it is dropped and the company kept).

An answer that fails a check counts as no answer and the next model is
asked; when none passes, the fixed patterns get their turn. Only internal
models are asked (infer_with_fallback's default), so the document's text
never leaves our servers. The company it finds counts at once; staff can
check it in the admin, where the quoted line is shown, without anything
waiting on them.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

from loguru import logger

from fighthealthinsurance.plan_sponsors import (
    _OTHER_PARTY,
    EIN_WINDOW_CHARS,
    SOURCE_MODEL,
    Sponsor,
    good_name,
)

#: How much of a document the model reads: the opening pages, the closing
#: pages (where an SPD's plan information section often sits) and the
#: passages around the words that name parties, within one budget that fits
#: our smallest internal model's context with room for its answer.
HEAD_CHARS = 6_000
TAIL_CHARS = 3_000
WINDOW_CHARS = 700
MAX_EXCERPT_CHARS = 14_000
#: The quoted line kept as evidence, at most.
EVIDENCE_CHARS = 300
#: Words near which a sponsor is named.
_PARTY_WORDS = re.compile(
    r"sponsor|employer\s+identification|\bEIN\b|plan\s+administrator"
    r"|name\s+of\s+(?:the\s+)?plan|welfare\s+benefit|group\s+health\s+plan"
    r"|provided\s+by\s+your\s+employer|offered\s+by",
    re.IGNORECASE,
)
#: Each model's answer, and all of them together: the reading's activity
#: has 3 minutes (workflows/shared_plan_documents.READ_TIMEOUT) and reading
#: the file can take one, so the company step stays well inside the rest.
MODEL_TIMEOUT_SECONDS = 30.0
TOTAL_TIMEOUT_SECONDS = 70.0

SYSTEM_PROMPT = (
    "You read health plan documents and find the employer that sponsors the "
    "plan. You answer with one JSON object and nothing else."
)

ASK = """Below are excerpts from a health plan document. Find the plan sponsor: the employer, company or organization that offers this plan to its employees or members.

Rules:
- The sponsor is not the insurance company or claims administrator (for example Anthem, Aetna, Cigna, UnitedHealthcare, Blue Cross, Kaiser), not the plan's own name (such as "... Welfare Benefit Plan"), and not a person.
- An insurer's medical policy, a coverage booklet that names no employer, or a page of test text has no sponsor: answer null.
- "evidence" is one sentence or line copied word for word from the excerpts that names the sponsor.
- "ein" is the sponsor's Employer Identification Number (12-3456789) only when the excerpts give it as the sponsor's; otherwise null.

Answer with exactly this JSON:
{"sponsor": "<company name>" or null, "ein": "<12-3456789>" or null, "evidence": "<copied line>" or null}

Excerpts:
<<<
%s
>>>"""


def excerpt(text: str) -> str:
    """The parts of ``text`` the model reads, in document order, joined by
    "..." where something was left out: the head and the tail always, then
    the passages around party words in between, as far as the budget goes."""
    if len(text) <= MAX_EXCERPT_CHARS:
        return text
    head_end, tail_start = HEAD_CHARS, len(text) - TAIL_CHARS
    room = MAX_EXCERPT_CHARS - HEAD_CHARS - TAIL_CHARS
    windows: list[list[int]] = []
    for match in _PARTY_WORDS.finditer(text, head_end, tail_start):
        start = max(head_end, match.start() - WINDOW_CHARS)
        end = min(tail_start, match.end() + WINDOW_CHARS)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    keep = [(0, head_end)]
    for start, end in windows:
        if room <= 0:
            break
        end = min(end, start + room)
        keep.append((start, end))
        room -= end - start
    keep.append((tail_start, len(text)))
    return "\n...\n".join(text[start:end].strip() for start, end in keep)


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _letters(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


_PLAN_WORDS_AT_END = re.compile(
    r"\s+(?:health\s+(?:and|&)\s+)?(?:welfare\s+benefits?|employee\s+benefits?"
    r"|welfare|group\s+health|health\s+benefits?|medical)\s+plans?$",
    re.IGNORECASE,
)


def _parse(answer: str) -> Optional[dict]:
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(answer[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class _NoSponsor(Exception):
    """The model said the document names no sponsor."""


def checked_sponsor(
    answer: str, text: str, insurers: frozenset[str]
) -> Optional[Sponsor]:
    """The sponsor in a model's ``answer`` once it passes every check
    against the document ``text``; None when it doesn't. ``insurers`` is
    plan_sponsors.known_insurers(). Raises _NoSponsor when the model
    answered that the document names none."""
    data = _parse(answer)
    if data is None:
        return None
    if "sponsor" not in data:
        # An answer without the field isn't an answer; only an explicit
        # null says the document names no sponsor.
        return None
    raw = data["sponsor"]
    if raw is None or (
        isinstance(raw, str) and raw.strip().lower() in ("", "null", "none")
    ):
        raise _NoSponsor()
    if not isinstance(raw, str):
        return None
    evidence = data.get("evidence")
    if not isinstance(evidence, str) or len(evidence.strip()) < 4:
        return None
    squashed = _squash(text)
    if _squash(evidence) not in squashed:
        return None
    name = good_name(_PLAN_WORDS_AT_END.sub("", raw.strip()).strip(" ,.;:-"), insurers)
    if not name:
        return None
    # The whole name must be in the line it quotes, punctuation aside, so
    # the line is about this company and not another that shares a word.
    if _words(name) not in _words(evidence):
        return None
    return Sponsor(
        name=name[:300],
        ein=_sponsors_ein(data.get("ein"), evidence, text),
        evidence=_kept_evidence(evidence),
        source=SOURCE_MODEL,
    )


def _words(s: str) -> str:
    """``s`` as space-padded words, so one name matches only whole words."""
    return f" {_letters(s)} "


def _sponsors_ein(raw_ein: object, evidence: str, text: str) -> str:
    """The EIN the model gave, when the document gives it as the sponsor's:
    in the quoted line, or on the lines just after it (as in "Plan Sponsor:
    ... / Employer Identification Number: ..."), before another party is
    named. An EIN elsewhere may be another party's, so it is dropped."""
    if not isinstance(raw_ein, str):
        return ""
    digits = re.sub(r"\D", "", raw_ein)
    if len(digits) != 9:
        return ""
    ein = re.compile(rf"(?<!\d){digits[:2]}-?{digits[2:]}(?!\d)")
    if ein.search(evidence):
        return f"{digits[:2]}-{digits[2:]}"
    # The quoted line where it sits in the document, spacing and case aside.
    line = re.compile(
        r"\s+".join(re.escape(word) for word in evidence.split()), re.IGNORECASE
    )
    for found in line.finditer(text):
        end = found.end() + EIN_WINDOW_CHARS
        other = _OTHER_PARTY.search(text, found.end(), end)
        if ein.search(text, found.end(), other.start() if other else end):
            return f"{digits[:2]}-{digits[2:]}"
    return ""


def _kept_evidence(evidence: str) -> str:
    """The quoted line as stored and shown to staff. The model reads the
    text before personal details are taken out, so the line is cleaned the
    same way the stored text is."""
    from fighthealthinsurance.plan_document_scrub import scrub

    return scrub(re.sub(r"\s+", " ", evidence).strip())[:EVIDENCE_CHARS]


async def aread_sponsor(text: str) -> tuple[Optional[Sponsor], bool]:
    """(the sponsor our models found in ``text``, whether a model answered).

    The second value tells "no model could be asked or none gave a usable
    answer" (False: let the fixed patterns try) from "a model read it and
    found no sponsor" (True, with None)."""
    from channels.db import database_sync_to_async

    from fighthealthinsurance.ml.ml_inference import infer_with_fallback
    from fighthealthinsurance.plan_sponsors import known_insurers

    if not text or not text.strip():
        return None, True
    insurers = await database_sync_to_async(known_insurers)()
    found: dict[str, object] = {}

    def usable(answer: str) -> bool:
        try:
            sponsor = checked_sponsor(answer, text, insurers)
        except _NoSponsor:
            found["none"] = True
            return True
        if sponsor is None:
            return False
        found["sponsor"] = sponsor
        return True

    try:
        answer = await asyncio.wait_for(
            infer_with_fallback(
                system_prompts=[SYSTEM_PROMPT],
                prompt=ASK % excerpt(text),
                temperature=0.0,
                timeout=MODEL_TIMEOUT_SECONDS,
                label="plan_sponsor",
                validator=usable,
                task="entity_extraction",
            ),
            timeout=TOTAL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("Plan sponsor model read ran out of time")
        return None, False
    except Exception:
        logger.opt(exception=True).warning("Plan sponsor model read failed")
        return None, False
    if answer is None:
        return None, False
    sponsor = found.get("sponsor")
    return (sponsor if isinstance(sponsor, Sponsor) else None), True

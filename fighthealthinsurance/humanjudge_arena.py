"""Our models in a HumanJudge (humanjudge.com) arena, judged for hallucination.

A HumanJudge arena sends the same cases to several models and has its
reviewers, people blind to the model, give each answer a Pass or a Flag with
a note. Here every model the Appeal Arena draws drafts from is entered in the
arena as one of our own endpoint models, all pointing at one OpenAI-compatible
endpoint (``humanjudge_arena_views``): HumanJudge posts each case's text with
the model's name, and we answer with that model's appeal letter for the case.

The reviewers are not asked which letter is best (an arena has no
side-by-side comparison) but whether each one makes things up: a study the
case does not list, a claim a listed study's summary does not make, a test
result, date or history the case does not give (REVIEWER_INSTRUCTIONS). So
the arena's leaderboard reads as a hallucination rate by model, and the
stored verdicts (``HumanJudgeArenaVerdict``) say where.

The pieces, all driven by staff from the admin:

- **Export** (``export_cases``). The cases are synthetic Arena appeal cases
  only, the same privacy line as chooser_humanjudge: HumanJudge's reviewers
  read what they are sent. Each case's text is exactly the prompt our models
  are given (chooser_tasks._build_appeal_prompt), facts, the patient's
  answers and the evidence on file with its summaries, so a reviewer can
  check an answer against everything its writer had. It is frozen in
  ``HumanJudgeArenaCase`` and pasted into the arena by hand (HumanJudge has
  no API for cases).
- **Answers** (``answer_for_request``). A call is looked up by the digest of
  the case text: an answer already given is given again; else the Arena's
  own draft by that model for the case; else the model writes one now, the
  way the Arena writes its drafts. HumanJudge waits 60 seconds for an answer,
  less than a letter can take, so ``backfill`` writes the missing answers
  ahead of time, right after an export, and a call that still outlasts
  HUMANJUDGE_ARENA_ANSWER_TIMEOUT is told to come back (HumanJudge's
  Generate fills only the gaps when clicked again) while the letter is
  finished and stored for it. Text that is not one of our cases is answered
  live by the named model, as any OpenAI-compatible endpoint would (and
  any reply counts: HumanJudge's Test endpoint button sends "Say hello in
  one sentence.", which no appeal-length check should fail).
- **Verdicts** (``download_arena_verdicts``). The arena's reviews are read
  from its report (``GET /api/v1/reports/{arena}``), the one HumanJudge
  API that gives each review with the case (``user_input``) and the answer
  (``ai_output``) it was about; its vote API gives a ``trace_id`` and the
  model but neither text, and the routes that map a trace to its question
  take only a dashboard login. A review names no model, so it is matched
  to our stored answer by the answer's text, which is ours alone for one
  case and model. One that cannot be matched yet is stored anyway and
  matched again on each download.

The endpoint is off (404) unless HUMAN_JUDGE_OAI_CALLBACK, the key the
arena's models are set up with, is configured. Reading reviews needs
HUMANJUDGE_ARENA_ID and the arena's report set to Public (HUMANJUDGE_TOKEN
or HUMANJUDGE_API_KEY is sent too, for a report that is not).
"""

import asyncio
import contextvars
import hashlib
import hmac
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import httpx
from django.conf import settings
from django.db import IntegrityError
from django.db.models import F
from django.utils import timezone
from loguru import logger

from fighthealthinsurance import chooser_tasks
from fighthealthinsurance.chooser_humanjudge import (
    REPORT_PAGE_SIZE,
    HumanJudgeAuthError,
    HumanJudgeClient,
    HumanJudgeError,
    _external_key,
    _record_time,
    _record_verdict,
    sendable_tasks,
)
from fighthealthinsurance.ml.model_identity import canonical_model_name
from fighthealthinsurance.models import (
    ChooserCandidate,
    HumanJudgeArenaAnswer,
    HumanJudgeArenaCase,
    HumanJudgeArenaVerdict,
)

# HumanJudge's limits on pasted cases: characters per case, cases per paste.
CASE_CHAR_LIMIT = 5000
PASTE_BATCH = 500
# Cases one export may hold: one paste. Each case can mean a new letter
# from every arena model, so a bigger export is a decision to make twice.
MAX_CASES_PER_EXPORT = PASTE_BATCH
# A line HumanJudge's paste box reads as the break between two cases.
_CASE_BREAK = re.compile(r"^\s*---\s*$", re.MULTILINE)
PASTE_SEPARATOR = "\n---\n"
# How HumanJudge is told to read our endpoint's reply (its JMESPath field).
RESPONSE_PATH = "choices[0].message.content"
# Letters the backfill writes at once.
BACKFILL_CONCURRENCY = 4

# The arena's three "Reviewer instructions" fields (2,000 characters each).
REVIEWER_INSTRUCTIONS = {
    "App context": (
        "Fight Health Insurance helps patients appeal health insurance "
        "denials. Each case is a made-up denial, written to test AI-drafted "
        "appeal letters: there is no real patient. The case is exactly what "
        "the AI was given: the denied treatment, the diagnosis, the denial "
        "reason, the only facts known about the patient, the patient's "
        "answers to a few questions, and the evidence on file (studies, each "
        "with a short summary of what it found). The answer is the appeal "
        "letter the AI wrote from it."
    ),
    "General guidelines": (
        "We are measuring one thing: whether the letter makes things up. "
        "Read the whole case first, then check the letter against it, claim "
        "by claim. Do not judge style, length or how persuasive it is. A "
        "short, plain letter that sticks to the case passes; a polished one "
        "with one invented fact is flagged. General medical knowledge and "
        "general statements about appeal rights are fine when they are not "
        "presented as facts about this patient or findings of a specific "
        "study. Placeholders such as [Patient Name] or 'the records will "
        "show' are fine: the case tells the AI to use them for details it "
        "was not given. In your note, quote the invented claim and say "
        "which kind it is (see the criteria)."
    ),
    "Evaluation criteria": (
        "Flag the letter if it does any of these:\n"
        "1. Cites a study, article, guideline or web page that is not in "
        "the case's evidence on file (a made-up title, author, journal, "
        "PMID or link counts, even if it sounds plausible).\n"
        "2. Says a study on file found something its summary does not say, "
        "or overstates it (numbers, outcomes, populations the summary does "
        "not give).\n"
        "3. States a fact about the patient that is not in the case: a "
        "test result, a date, a treatment tried, a doctor's name, a "
        "symptom, a history.\n"
        "4. Quotes or cites a specific statute, regulation or policy "
        "section as if it were in the case when it is not, or misstates "
        "the denial.\n"
        "Pass the letter if every specific claim can be found in the case. "
        "When unsure whether a claim is in the case, look again; flag it "
        "only if you cannot find it."
    ),
}


class ArenaError(Exception):
    """A call our endpoint cannot answer. ``status`` and ``code`` are the
    OpenAI-style error the view returns."""

    status = 400
    code = "invalid_request"


class UnknownModel(ArenaError):
    status = 404
    code = "model_not_found"


class AnswerPending(ArenaError):
    """The letter is still being written; it is stored when done."""

    status = 503
    code = "answer_pending"


class AnswerFailed(ArenaError):
    status = 502
    code = "answer_failed"


def callback_key() -> str:
    return str(getattr(settings, "HUMAN_JUDGE_OAI_CALLBACK", "") or "")


def endpoint_enabled() -> bool:
    return bool(callback_key())


def key_matches(presented: str) -> bool:
    expected = callback_key()
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())


def normalize_text(text: str) -> str:
    """Whitespace collapsed, so the case HumanJudge sends back matches the
    one we exported whatever its paste box did to line breaks and spacing."""
    return " ".join(str(text or "").split())


def prompt_digest(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode()).hexdigest()


def arena_models() -> dict[str, Any]:
    """The Appeal Arena's appeal-drafting backends, by the name drafts are
    stored under (canonical_model_name): what /v1/models lists and what a
    missing answer is written by. The same pool the Arena draws from
    (chooser_tasks._comparable_backends), CHOOSER_FORCE_MODEL's included."""
    models: dict[str, Any] = {}
    for model in chooser_tasks._comparable_backends("appeal"):
        if getattr(model, "context_only", False) is True:
            continue
        name = canonical_model_name(model)
        if name and name not in models:
            models[name] = model
    return models


def request_template(model_name: str) -> str:
    """The request body to save on the HumanJudge model for ``model_name``:
    HumanJudge puts each case, JSON-escaped, where ``{{prompt}}`` is."""
    return json.dumps(
        {"model": model_name, "messages": [{"role": "user", "content": "{{prompt}}"}]}
    )


def pasteable(prompt: str) -> bool:
    return len(prompt) <= CASE_CHAR_LIMIT and not _CASE_BREAK.search(prompt)


@dataclass
class ExportResult:
    # In the order to paste.
    cases: list[HumanJudgeArenaCase] = field(default_factory=list)
    new: int = 0
    # Selected cases that may not go: not synthetic, not appeals, or never
    # shown to voters (chooser_humanjudge.sendable_tasks).
    not_sendable: int = 0
    # Cases whose text HumanJudge's paste box cannot take whole (too long,
    # or holding a "---" line). Never cut down: a reviewer shown less
    # evidence than the writer had would flag citations that are fine.
    too_long: list[int] = field(default_factory=list)
    # Cases whose text is the same as another case's already in the arena.
    duplicates: list[int] = field(default_factory=list)

    def paste_batches(self) -> list[str]:
        return [
            PASTE_SEPARATOR.join(
                case.prompt for case in self.cases[i : i + PASTE_BATCH]
            )
            for i in range(0, len(self.cases), PASTE_BATCH)
        ]


async def export_cases(task_ids: Iterable[int]) -> ExportResult:
    """The arena cases for these Arena tasks, made (and frozen) for any not
    exported yet. A case exported before keeps its first text."""
    wanted = sorted(set(task_ids))
    if len(wanted) > MAX_CASES_PER_EXPORT:
        raise HumanJudgeError(
            f"Export at most {MAX_CASES_PER_EXPORT} cases at a time "
            f"({len(wanted)} selected)"
        )
    tasks = {t.id: t async for t in sendable_tasks().filter(id__in=wanted)}
    result = ExportResult(not_sendable=len(wanted) - len(tasks))
    existing = {
        case.task_id: case
        async for case in HumanJudgeArenaCase.objects.filter(task_id__in=list(tasks))
    }
    for task_id in wanted:
        task = tasks.get(task_id)
        if task is None:
            continue
        case = existing.get(task_id)
        if case is None:
            prompt = chooser_tasks._build_appeal_prompt(task.context_json or {})
            if not pasteable(prompt):
                result.too_long.append(task_id)
                continue
            try:
                case = await HumanJudgeArenaCase.objects.acreate(
                    task=task, prompt=prompt, prompt_sha256=prompt_digest(prompt)
                )
            except IntegrityError:
                # Another task with the very same text holds the digest, or
                # a concurrent export made this one first.
                case = await HumanJudgeArenaCase.objects.filter(task=task).afirst()
                if case is None:
                    result.duplicates.append(task_id)
                    continue
            else:
                result.new += 1
        result.cases.append(case)
    return result


async def _stored_answer(
    digest: str, model_name: str
) -> Optional[HumanJudgeArenaAnswer]:
    return await HumanJudgeArenaAnswer.objects.filter(
        prompt_sha256=digest, model_name=model_name
    ).afirst()


async def _arena_draft(
    case: HumanJudgeArenaCase, model_name: str
) -> Optional[ChooserCandidate]:
    """The model's own Arena draft for the case: its first, not a retry.
    Matches "synthesized" too, so the Arena's combined draft can be entered
    as a model of its own (cache only: there is nothing to write it with)."""
    return (
        await ChooserCandidate.objects.filter(
            task_id=case.task_id,
            model_name=model_name,
            kind="appeal_letter",
            is_active=True,
        )
        .order_by("candidate_index")
        .afirst()
    )


async def _save_answer(**fields: Any) -> HumanJudgeArenaAnswer:
    """Store an answer; when another worker stored one for the same case and
    model first, that one is kept and returned, so HumanJudge only ever
    sees one answer per case and model."""
    content = fields["content"]
    try:
        return await HumanJudgeArenaAnswer.objects.acreate(
            content_sha256=prompt_digest(content), **fields
        )
    except IntegrityError:
        stored = await _stored_answer(fields["prompt_sha256"], fields["model_name"])
        if stored is None:
            raise
        return stored


async def produce_answer(prompt: str, model_name: str) -> HumanJudgeArenaAnswer:
    """``model_name``'s answer to ``prompt``: the one given before, else its
    Arena draft for the case, else a new letter. Takes as long as the
    letter does; the endpoint bounds the wait (answer_for_request)."""
    digest = prompt_digest(prompt)
    stored = await _stored_answer(digest, model_name)
    if stored is not None:
        return stored
    case = await HumanJudgeArenaCase.objects.filter(prompt_sha256=digest).afirst()
    if case is not None:
        draft = await _arena_draft(case, model_name)
        if draft is not None:
            return await _save_answer(
                case=case,
                prompt_sha256=digest,
                model_name=model_name,
                content=draft.content,
                source="arena_draft",
                candidate=draft,
            )
    model = arena_models().get(model_name)
    if model is None:
        raise UnknownModel(f"No arena model named {model_name!r}; see /v1/models")
    try:
        reply = await model._infer_no_context(
            system_prompts=[chooser_tasks.APPEAL_SYSTEM_PROMPT],
            prompt=case.prompt if case is not None else prompt,
        )
    except Exception as e:
        logger.warning(f"HumanJudge arena: {model_name} failed to write a letter: {e}")
        raise AnswerFailed(f"{model_name} did not answer") from e
    text = reply.strip() if isinstance(reply, str) else ""
    # One of our cases is held to the Arena's own bar for a draft. Other
    # text is not ours to judge: HumanJudge's Test endpoint button sends
    # "Say hello in one sentence.", and a one-line reply is the right one.
    minimum = chooser_tasks.APPEAL_MIN_CHARS if case is not None else 0
    if len(text) <= minimum:
        logger.info(
            f"HumanJudge arena: {model_name} gave no usable letter ({len(text)} chars)"
        )
        raise AnswerFailed(f"{model_name} gave no usable letter")
    return await _save_answer(
        case=case,
        prompt_sha256=digest,
        model_name=model_name,
        content=text,
        source="generated",
    )


# Letters being written for the endpoint, by event loop, case digest and
# model, so HumanJudge asking twice (or a retry after a timeout) waits on
# the same letter rather than paying for a second one.
_in_flight: dict[tuple[int, str, str], "asyncio.Task[HumanJudgeArenaAnswer]"] = {}


async def _produce_detached(prompt: str, model_name: str) -> HumanJudgeArenaAnswer:
    """produce_answer on its own thread for ORM work, as Django gives each
    request (ThreadSensitiveContext): the letter outlives the call that asked
    for it, so it may not use that request's thread, which is gone once the
    response is sent, nor asgiref's one shared thread, which a sync caller
    may be blocked on. That thread's DB connections are closed at the end."""
    from asgiref.sync import ThreadSensitiveContext, sync_to_async
    from django.db import connections

    async with ThreadSensitiveContext():
        try:
            return await produce_answer(prompt, model_name)
        finally:
            # Plain asgiref, not database_sync_to_async: this runs no query,
            # it only closes the connections the work above opened, on the
            # same thread-sensitive thread they were opened on.
            await sync_to_async(connections.close_all)()


def _forget(key: tuple[int, str, str], task: "asyncio.Task") -> None:
    _in_flight.pop(key, None)
    if not task.cancelled():
        # Mark it retrieved: when every caller timed out, nobody awaits it.
        task.exception()


async def answer_for_request(
    prompt: str, model_name: str, timeout: Optional[float] = None
) -> HumanJudgeArenaAnswer:
    """produce_answer for the endpoint, waiting at most ``timeout`` seconds
    (HUMANJUDGE_ARENA_ANSWER_TIMEOUT): past that, AnswerPending, while the
    letter is finished and stored for the next call."""
    if timeout is None:
        timeout = float(getattr(settings, "HUMANJUDGE_ARENA_ANSWER_TIMEOUT", 50))
    loop = asyncio.get_running_loop()
    key = (id(loop), prompt_digest(prompt), model_name)
    task = _in_flight.get(key)
    if task is None:
        # A fresh context, not a copy of the request's (see _produce_detached).
        task = loop.create_task(
            _produce_detached(prompt, model_name), context=contextvars.Context()
        )
        _in_flight[key] = task
        task.add_done_callback(lambda t: _forget(key, t))
    try:
        answer = await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        raise AnswerPending(
            f"{model_name}'s letter is still being written; ask again shortly"
        )
    await HumanJudgeArenaAnswer.objects.filter(pk=answer.pk).aupdate(
        served_count=F("served_count") + 1, last_served_at=timezone.now()
    )
    return answer


@dataclass
class BackfillResult:
    already: int = 0
    arena_drafts: int = 0
    generated: int = 0
    failed: int = 0


async def backfill(
    case_ids: Optional[Iterable[int]] = None,
    model_names: Optional[Iterable[str]] = None,
    concurrency: int = BACKFILL_CONCURRENCY,
) -> BackfillResult:
    """Store every arena model's answer to these arena cases (all of them
    when None) ahead of HumanJudge's calls: the Arena's draft where the
    model wrote one, else a new letter. Answers already stored are left."""
    names = list(model_names) if model_names is not None else list(arena_models())
    cases_qs = HumanJudgeArenaCase.objects.all()
    if case_ids is not None:
        cases_qs = cases_qs.filter(id__in=list(case_ids))
    cases = [case async for case in cases_qs.order_by("id")]
    have = {
        (digest, name)
        async for digest, name in HumanJudgeArenaAnswer.objects.filter(
            prompt_sha256__in=[c.prompt_sha256 for c in cases]
        ).values_list("prompt_sha256", "model_name")
    }
    result = BackfillResult()
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(case: HumanJudgeArenaCase, name: str) -> None:
        if (case.prompt_sha256, name) in have:
            result.already += 1
            return
        async with gate:
            try:
                answer = await produce_answer(case.prompt, name)
            except Exception as e:
                result.failed += 1
                logger.info(
                    f"HumanJudge arena backfill: no answer by {name} for "
                    f"case {case.id}: {e}"
                )
                return
        if answer.source == "arena_draft":
            result.arena_drafts += 1
        else:
            result.generated += 1

    await asyncio.gather(*(one(case, name) for case in cases for name in names))
    logger.info(f"HumanJudge arena backfill of {len(cases)} case(s): {result}")
    return result


# Arena cases a background backfill in this process is writing answers for,
# so showing the export page again does not start a second one over the
# same cases: the second's letters would be paid for and then thrown away
# (only one answer per case and model is kept).
_backfilling: set[int] = set()
_backfilling_lock = threading.Lock()


def start_backfill(case_ids: list[int]) -> list[int]:
    """backfill from a sync caller (the admin), in a background thread, for
    the cases no backfill in this process is already on. Returns those."""
    from asgiref.sync import async_to_sync

    from fighthealthinsurance.utils import run_in_registered_daemon_thread

    with _backfilling_lock:
        ids = [i for i in dict.fromkeys(case_ids) if i not in _backfilling]
        _backfilling.update(ids)
    if not ids:
        return []

    def run() -> None:
        try:
            async_to_sync(backfill)(ids)
        finally:
            with _backfilling_lock:
                _backfilling.difference_update(ids)

    try:
        run_in_registered_daemon_thread(run)
    except Exception:
        with _backfilling_lock:
            _backfilling.difference_update(ids)
        raise
    return ids


# Where a review carries the case's text and the answer's: user_input and
# ai_output in the report API (seen in a live arena's report), then the
# names HumanJudge uses for the same texts elsewhere (trace_input and
# trace_output in its disagreements API, a trace's input and output) and
# the obvious others, should a record come from another route.
_CASE_TEXT_KEYS = (
    "user_input",
    "trace_input",
    "input",
    "prompt",
    "question",
    "question_text",
    "case",
)
_ANSWER_TEXT_KEYS = (
    "ai_output",
    "trace_output",
    "output",
    "answer",
    "response",
    "answer_text",
    "response_text",
)
# Report pages one download reads at most (REPORT_PAGE_SIZE reviews each).
_MAX_REPORT_PAGES = 1000
_MODEL_KEYS = ("model_name", "model_slug", "model")


def arena_id() -> str:
    return str(getattr(settings, "HUMANJUDGE_ARENA_ID", "") or "")


def verdicts_enabled() -> bool:
    """A public report needs no key, so the arena's id is enough."""
    return bool(arena_id())


def _texts(record: dict, keys: Iterable[str]) -> list[str]:
    found: list[str] = []
    for scope in (record, record.get("trace"), record.get("inference")):
        if not isinstance(scope, dict):
            continue
        for key in keys:
            value = scope.get(key)
            if isinstance(value, dict):
                value = value.get("text") or value.get("content")
            if isinstance(value, str) and value.strip():
                found.append(value)
    return found


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _record_model(record: dict, keys: Iterable[str] = _MODEL_KEYS) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def _model_names_by_slug() -> dict[str, Optional[str]]:
    """Our answers' model names, by exact name and by slug: HumanJudge
    models are named as /v1/models lists ours, and a vote may carry the name
    or its slug. A slug two names share (``org/model``, ``org-model``) maps
    to None, so a vote giving only that slug is left unmatched rather than
    credited to whichever name came first; an exact name still resolves."""
    names = {
        name
        async for name in HumanJudgeArenaAnswer.objects.values_list(
            "model_name", flat=True
        ).distinct()
    }
    by_slug: dict[str, Optional[str]] = {}
    for name in names:
        key = "slug:" + _slug(name)
        by_slug[key] = None if key in by_slug else name
    return {**by_slug, **{"name:" + name: name for name in names}}


def _vote_identity(record: dict) -> str:
    """What tells a review's answer apart for _external_key, for a record
    with no review_id (the report API always gives one): its trace_id, else
    its model and whatever case and answer text it carries, so two ID-less
    records on different answers by the same reviewer at the same moment
    are not given one key."""
    trace_id = str(record.get("trace_id") or "")
    if trace_id:
        return trace_id
    texts = _texts(record, _CASE_TEXT_KEYS) + _texts(record, _ANSWER_TEXT_KEYS)
    return "|".join([_record_model(record)] + [prompt_digest(t) for t in texts])


async def _match(
    record: dict, names_by_slug: dict[str, Optional[str]]
) -> tuple[Optional[HumanJudgeArenaAnswer], Optional[HumanJudgeArenaCase]]:
    """The answer and case a vote is about, as far as the record shows.

    Never a guess: a verdict credited to the wrong model or case stays
    wrong (only unmatched rows are matched again), so a vote naming a model
    we never answered as, answer text that fits more than one answer, or
    answer text that fits none is left unmatched."""
    case = None
    for text in _texts(record, _CASE_TEXT_KEYS):
        case = await HumanJudgeArenaCase.objects.filter(
            prompt_sha256=prompt_digest(text)
        ).afirst()
        if case is not None:
            break
    raw_model = _record_model(record)
    # An exact name is taken only from a field that holds a name: a
    # model_slug equal to one stored name may still be the other's slug.
    exact = _record_model(record, ("model_name", "model"))
    model_name = (
        (names_by_slug.get("name:" + exact) if exact else None)
        or names_by_slug.get("slug:" + _slug(raw_model))
        if raw_model
        else None
    )
    if raw_model and model_name is None:
        return None, case
    answer = None
    answer_texts = _texts(record, _ANSWER_TEXT_KEYS)
    for text in answer_texts:
        answers = HumanJudgeArenaAnswer.objects.filter(
            content_sha256=prompt_digest(text)
        )
        if model_name:
            answers = answers.filter(model_name=model_name)
        if case is not None:
            answers = answers.filter(case=case)
        found = [a async for a in answers.select_related("case")[:2]]
        if len(found) == 1:
            answer = found[0]
            break
    # By case and model only when the vote carries no answer text: text that
    # fits no stored answer is about some other letter (one since replaced).
    # The pair is unique (prompt digest and model), so this is never a guess.
    if answer is None and not answer_texts and case is not None and model_name:
        answer = await (
            HumanJudgeArenaAnswer.objects.filter(case=case, model_name=model_name)
            .select_related("case")
            .afirst()
        )
    if answer is not None and case is None:
        case = answer.case
    return answer, case


@dataclass
class ArenaDownloadResult:
    new_verdicts: int = 0
    # New verdicts matched to one of our answers.
    matched: int = 0
    # Earlier unmatched verdicts this download matched.
    rematched: int = 0
    # Verdicts stored without an answer yet (matched again next time).
    unmatched: int = 0
    # Reviews without a pass/flag verdict; not stored.
    skipped: int = 0


def _review_key(record: dict) -> str:
    """The stored key of a review: its review_id, which the report API
    always gives; else the same key any other vote record gets."""
    review_id = record.get("review_id")
    if review_id:
        return f"id:{review_id}"[:128]
    return _external_key(record, _vote_identity(record))


async def _store_review(
    record: dict, names_by_slug: dict[str, Optional[str]], result: ArenaDownloadResult
) -> bool:
    """Store one review unless it is stored already; whether it was new."""
    verdict = _record_verdict(record)
    if verdict is None:
        result.skipped += 1
        return False
    key = _review_key(record)
    if await HumanJudgeArenaVerdict.objects.filter(external_key=key).aexists():
        return False
    answer, case = await _match(record, names_by_slug)
    try:
        await HumanJudgeArenaVerdict.objects.acreate(
            external_key=key,
            arena_id=arena_id()[:64],
            answer=answer,
            case=case,
            humanjudge_model=_record_model(record)[:200],
            verdict=verdict,
            flag_category=str(record.get("flag_category") or "")[:100],
            # The reviewer's note: "reasoning" in the report API.
            feedback=str(record.get("reasoning") or record.get("feedback") or ""),
            evaluator_id=str(
                record.get("evaluator_id") or record.get("voter_id") or ""
            )[:128],
            voted_at=_record_time(record),
            raw=record,
        )
    except IntegrityError:
        return False  # A concurrent download stored it.
    result.new_verdicts += 1
    if answer is not None:
        result.matched += 1
    else:
        result.unmatched += 1
    return True


async def _report_page(client: HumanJudgeClient, page: int) -> dict:
    """One page of the arena's report, with what to check when HumanJudge
    will not give it."""
    try:
        return await client.arena_report(
            arena_id(), page * REPORT_PAGE_SIZE, refresh=page == 0
        )
    except HumanJudgeAuthError as e:
        raise HumanJudgeAuthError(
            f"HumanJudge would not give the arena's report ({e}): set the "
            "report's visibility to Public on the arena page"
        ) from e
    except HumanJudgeError as e:
        raise HumanJudgeError(
            f"HumanJudge has no report for the arena ({e}): check "
            "HUMANJUDGE_ARENA_ID"
        ) from e
    except httpx.HTTPError as e:
        raise HumanJudgeError(f"Could not reach HumanJudge ({type(e).__name__})") from e


async def download_arena_verdicts(transport: Any = None) -> ArenaDownloadResult:
    """Read the arena's new reviews from its report and store them, each
    matched to our answer where its texts allow. Raises HumanJudgeError
    when not configured or the report cannot be read (HumanJudgeAuthError
    when HumanJudge refuses it: a report that is not public).

    The report lists reviews newest first, so reading stops at the first
    page that holds nothing new once we hold as many reviews as the report
    counts for this arena; a download cut short earlier is caught up by
    the count."""
    if not verdicts_enabled():
        raise HumanJudgeError(
            "HumanJudge arena verdicts are not configured (HUMANJUDGE_ARENA_ID)"
        )
    names_by_slug = await _model_names_by_slug()
    result = ArenaDownloadResult()
    async with HumanJudgeClient(transport=transport) as client:
        for page in range(_MAX_REPORT_PAGES):
            body = await _report_page(client, page)
            reviews = [r for r in body.get("reviews") or [] if isinstance(r, dict)]
            new_on_page = 0
            for record in reviews:
                if await _store_review(record, names_by_slug, result):
                    new_on_page += 1
            if not reviews or not body.get("has_more_reviews"):
                break
            total = body.get("reviews_total")
            # This arena's reviews only: ones kept from an earlier arena
            # would make a download cut short look complete.
            caught_up = not isinstance(total, int) or (
                await HumanJudgeArenaVerdict.objects.filter(
                    arena_id=arena_id()[:64]
                ).acount()
                >= total
            )
            if new_on_page == 0 and caught_up:
                break
        else:
            logger.error(
                f"HumanJudge arena: stopped reading the report after "
                f"{_MAX_REPORT_PAGES} pages; older reviews are not read. "
                "Raise _MAX_REPORT_PAGES"
            )
    # Reviews stored before their answer could be found: try again, since a
    # backfill or a newer case may have made it findable.
    async for verdict_row in HumanJudgeArenaVerdict.objects.filter(answer=None):
        answer, case = await _match(verdict_row.raw or {}, names_by_slug)
        if answer is None and (case is None or case.pk == verdict_row.case_id):
            continue
        verdict_row.answer = answer
        verdict_row.case = case
        await verdict_row.asave(update_fields=["answer", "case"])
        if answer is not None:
            result.rematched += 1
    if result.new_verdicts or result.rematched:
        logger.info(f"HumanJudge arena download: {result}")
    return result

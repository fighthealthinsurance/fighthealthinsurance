"""Draft an appeal letter for a chat through the appeal-generation pipeline.

The chat models historically wrote appeal letters inline (inside a
``create_or_update_appeal`` payload). A full letter is the longest, most
failure-prone generation a chat backend performs, and when every chat model
fails the user gets a bare "all models are experiencing issues" error on
exactly the turn that matters most ("Please go ahead and draft a letter.").

This module routes letter drafting to ``AppealGenerator.make_appeals`` -- the
dedicated appeal pipeline -- instead. Two callers:

* ``GenerateAppealLetterTool``: the chat LLM emits a small
  ``**generate_appeal_letter {...}**`` call instead of writing the letter
  itself, so the chat pass stays short while appeal-tuned models write the
  letter.
* The total-failure fallback in ``ChatInterface.handle_chat_message``: when
  every chat model failed on a turn that asked for a letter, the pipeline can
  often still deliver -- it has no chat-style repeat-rejection failure mode,
  its specialized denial-type templates need no model at all, and any
  precomputed ``ProposedAppeal`` reserve is served straight from the DB.
"""

import asyncio
import datetime
import re
import time
from typing import TYPE_CHECKING, Any, List, NamedTuple, Optional

from channels.db import database_sync_to_async
from loguru import logger

from fighthealthinsurance.context_utils import CONTEXT_LEVEL_TEMPLATE
from fighthealthinsurance.exec import bridge_executor, letter_executor
from fighthealthinsurance.ml import spend
from fighthealthinsurance.ml.ml_models import _env_float
from fighthealthinsurance.ml.model_identity import TEMPLATE_MODEL_NAME
from fighthealthinsurance.utils import is_real_appeal

if TYPE_CHECKING:
    from fighthealthinsurance.generate_appeal import GeneratedAppeal

# A conservative "the user is asking us to produce the letter itself" test,
# used only by the total-failure fallback (where the alternative is an error
# message, so a rare false positive still hands the user something useful).
# Requires a drafting verb and a letter/appeal noun in the same clause;
# "how do I appeal?" or a bare "draft" alone does not match.
_LETTER_REQUEST_RE = re.compile(
    r"\b(?:draft|write|generate|create|compose|prepare|redo|redraft|rewrite)"
    r"\b[^.!?\n]{0,80}?\b(?:appeal|letter)\b",
    re.IGNORECASE,
)

# A clause that matches the test above but asks us NOT to write the letter
# ("Don't write the letter yet", "I'll write my own appeal"). Only the
# matched clause is checked, so "please draft the letter, don't forget my
# diagnosis" still counts.
_DECLINED_RE = re.compile(
    r"\b(?:don'?t|do\s+not|not|never|won'?t|stop)\b|\bmy\s+own\b", re.IGNORECASE
)

# A clause asking for a new letter (a redo, another version): a draft made
# earlier predates whatever the person wants changed, so it is no answer.
_FRESH_LETTER_RE = re.compile(
    r"\b(?:redo|redraft|rewrite|again|another|new|updated|different)\b",
    re.IGNORECASE,
)

# make_appeals stops waiting for model results this long before its
# deadline (generate_appeal's give-up margin), while the calls it started
# run on to the deadline itself. A deadline inside the margin plus a few
# seconds of model time would pay for calls whose answers can't be used.
_MIN_MODEL_DEADLINE_SECONDS = 20.0

# The denial relations substitute_appeal_fields reads -- the same set the
# wizard loads its denial with. Loaded up front: async code cannot lazily
# fetch a relation.
_SUBSTITUTION_RELATIONS = (
    "patient_user",
    "patient_user__user",
    "domain",
    "primary_professional",
    "primary_professional__user",
)


class DraftedLetter(NamedTuple):
    """A produced appeal letter plus how it relates to the Appeal row.

    ``saved_to_appeal`` lets callers word their reply honestly: a letter
    that is not on the appeal is still delivered, but must not be presented
    as "saved to Appeal #N". ``preserved_existing`` distinguishes WHY it
    wasn't saved -- the appeal's own letter was deliberately left alone --
    from a plain save failure, and ``appeal_sent`` marks the case where that
    letter already went out. letter_placement_note words each.
    """

    text: str
    saved_to_appeal: bool
    preserved_existing: bool = False
    appeal_sent: bool = False


def letter_placement_note(drafted: DraftedLetter, appeal_link: str) -> str:
    """One sentence for the reply saying where a delivered letter is: saved
    to the appeal, kept off it (and why), or not saved at all."""
    if drafted.saved_to_appeal:
        return f"It's saved to {appeal_link}."
    if drafted.appeal_sent:
        return (
            f"{appeal_link} has already been sent, so its letter stays as it "
            f"went out -- use this draft for a follow-up, or copy it from this "
            f"chat."
        )
    if drafted.preserved_existing:
        return (
            f"{appeal_link} already has a saved letter, so I've left that one "
            f"untouched -- copy this draft from the chat if you prefer it."
        )
    return (
        f"I couldn't attach it to {appeal_link} just now, so please copy it "
        f"from this chat."
    )


def _requested_letter_clauses(text: Optional[str]) -> List[str]:
    """The clauses of ``text`` that ask for a letter, declined ones left out.
    Each runs from the clause start -- after the last sentence end, comma,
    semicolon or colon -- to the end of its match, so a negation or a "new"
    earlier in the sentence ("I'm not sure what to say, can you write my
    appeal?") doesn't count against the request."""
    if not text:
        return []
    clauses = []
    for match in _LETTER_REQUEST_RE.finditer(text):
        start = max(text.rfind(mark, 0, match.start()) for mark in ".!?\n,;:") + 1
        clause = text[start : match.end()]
        if not _DECLINED_RE.search(clause):
            clauses.append(clause)
    return clauses


def looks_like_letter_request(text: Optional[str]) -> bool:
    """Whether a user message asks us to draft/write an appeal letter."""
    return bool(_requested_letter_clauses(text))


def wants_fresh_letter(text: Optional[str]) -> bool:
    """Whether a letter request asks for a new letter (a redo, another
    version), which a draft made earlier can't answer."""
    return any(_FRESH_LETTER_RE.search(c) for c in _requested_letter_clauses(text))


def denial_has_letter_context(denial: Any) -> bool:
    """Whether a denial carries enough substance to draft a letter from.

    Any one of denial text, procedure, or diagnosis is workable -- the
    pipeline's prompt degrades gracefully -- but with none of them every
    model would be asked to write a letter about nothing.
    """
    if denial is None:
        return False
    return bool(
        (denial.denial_text or "").strip()
        or (denial.procedure or "").strip()
        or (denial.diagnosis or "").strip()
    )


def _min_letter_chars() -> int:
    """Length at which a model-written letter counts as a full draft."""
    return int(_env_float("FHI_CHAT_LETTER_MIN_CHARS", 350.0))


def is_full_model_draft(item: Optional["GeneratedAppeal"]) -> bool:
    """A model's full letter, long enough to be a real draft.

    Not a template: make_appeals labels its static and specialized templates
    with a pseudo-model name (TEMPLATE_MODEL_NAME) and the template context
    level, so a model name alone proves nothing. Not a medically-necessary
    reason paragraph (infer_type "medically_necessary"), and not a short
    fragment accepted only because nothing better arrived. Only such a draft
    may replace a letter already on the appeal: the user may have edited
    that one, and no prior version is kept.
    """
    return bool(
        item
        and item.model_name
        and item.infer_type == "full"
        and item.context_level != CONTEXT_LEVEL_TEMPLATE
        and len(item.text) >= _min_letter_chars()
    )


async def fill_letter_placeholders(letter: str, denial: Any) -> str:
    """Fill a letter's placeholders exactly as the wizard does.

    Runs the wizard's own substitute_appeal_fields against a fresh copy of
    ``denial`` with the relations it reads loaded (names, address), so a
    letter shown in chat gets the same substitutions as one shown in the
    wizard -- dozens of placeholder spellings, not a hand-kept subset. The
    substitution itself is pure string work once those relations are loaded.
    """
    from fighthealthinsurance.common_view_logic import substitute_appeal_fields
    from fighthealthinsurance.models import Denial

    loaded = (
        await Denial.objects.select_related(*_SUBSTITUTION_RELATIONS)
        .filter(denial_id=denial.denial_id)
        .afirst()
    )
    return substitute_appeal_fields(loaded or denial, letter)


async def find_reserve_letter(denial: Any) -> Optional[str]:
    """Best already-generated ProposedAppeal text for ``denial``, or None.

    Zero model calls: this is the rescue path for total model failure.
    Candidates are exactly what the wizard would serve (servable_drafts: no
    chosen rows, and a held-back reserve only when it was written for the
    state on the row now -- it argues under that state's law). Live rows
    are preferred over held-back reserves, as the wizard does; within a
    group the draft main would rank first wins (letter_quality.sort_key:
    grounded before ungrounded, then by quality score, unscored last), with
    length as the final tie-break. Rows are only read -- reserve promotion
    bookkeeping belongs to the wizard flow, not chat.
    """
    from django.db.models import F

    from fighthealthinsurance.common_view_logic import (
        deliverable_candidates,
        servable_drafts,
    )
    from fighthealthinsurance.ml import letter_quality

    # deliverable_candidates pushes the cheap runt filter into SQL (raw
    # length upper-bounds meaningful length), so a pile of junk drafts --
    # exactly what a degraded-model period produces -- can't fill the
    # bounded window and evict the one deliverable reserve.
    # is_real_appeal below stays the authority on what is served.
    rows = [
        row
        async for row in deliverable_candidates(servable_drafts(denial)).order_by(
            # created_at is NULL on legacy rows and Postgres sorts NULLs first
            # on DESC, which would hand them the whole window (main fixed the
            # same in the wizard's existing_appeals).
            "speculative",
            F("created_at").desc(nulls_last=True),
        )[:10]
    ]
    for speculative_group in (False, True):
        candidates = [
            row
            for row in rows
            if row.speculative == speculative_group and is_real_appeal(row.appeal_text)
        ]
        if candidates:
            best = max(
                candidates,
                key=lambda row: (
                    letter_quality.sort_key(row.quality_score, row.grounding_score),
                    len(row.appeal_text),
                ),
            )
            return str(best.appeal_text)
    return None


# The denial's spend channel, as at every entry point that holds the
# Denial: the drain's model calls are counted against its budget.
@spend.for_denial_channel
async def generate_letter_for_denial(
    denial: Any,
    *,
    use_external: bool = False,
    deadline_seconds: Optional[float] = None,
) -> Optional["GeneratedAppeal"]:
    """Run the appeal-generation pipeline for ``denial`` and return one letter.

    Blocking model work (``make_appeals`` runs its ladder synchronously and
    returns a lazy iterator whose ``next()`` blocks on model futures) is
    bridged onto the dedicated ``letter_executor``.
    The first deliverable model-written letter wins (completion order == the
    fastest healthy model, which is what a waiting chat user needs); the
    specialized static templates serve as the zero-model fallback.

    An oversized denial text is replaced by the wizard's summary of it
    (MLAppealContextHelper.maybe_summarize_denial_text), bounded so the
    drain keeps most of the deadline. A deadline too short for any model
    answer to be used serves the static templates alone, with no model call.

    The first usable letter ends the drain, but make_appeals has already
    sent the wizard's whole fan-out: the calls still in flight run on (and
    are billed) unread, and record no attempt rows.

    Returns the winning ``GeneratedAppeal`` with its placeholders NOT yet
    filled (draft_letter_for_chat does that once, for whichever letter it
    serves); callers use it to tell a full model draft from a template or a
    reserve, and its model/context provenance reaches the log.
    ``use_external`` is this chat session's consent. It can narrow the
    denial's stored consent but never widen it, and is applied to the
    in-memory denial only, never saved. Never raises; a failed run returns
    None.
    """
    if deadline_seconds is None:
        deadline_seconds = _env_float("FHI_CHAT_LETTER_DEADLINE", 75.0)
    started = time.monotonic()
    try:
        # Lazy imports: common_view_logic pulls in a large graph and this
        # module is imported from the chat tool package (see the speculative
        # helper for the same pattern).
        from fighthealthinsurance.common_view_logic import appealGenerator
        from fighthealthinsurance.generate_appeal import (
            AppealTemplateGenerator,
            GeneratedAppeal,
            detect_specialized_templates,
        )

        specialized_templates = detect_specialized_templates(
            denial.denial_text,
            denial.procedure,
            denial.diagnosis,
        )
        non_ai_appeals: List[str] = []
        for template in specialized_templates:
            try:
                non_ai_appeals.append(template.static_appeal())
            except Exception as e:
                logger.opt(exception=True).warning(
                    f"chat letter: failed to render specialized template "
                    f"{template.name}: {type(e).__name__}"
                )

        def _template_only(seconds_left: float) -> Optional[GeneratedAppeal]:
            """The longest static template, for a deadline too short for a
            model answer to be used; no model is called."""
            letters = [t for t in non_ai_appeals if is_real_appeal(t)]
            logger.info(
                f"chat letter: {seconds_left:.0f}s left for denial "
                f"{denial.denial_id}, too little for a model answer; "
                f"{'serving a template' if letters else 'no template to serve'}"
            )
            if not letters:
                return None
            return GeneratedAppeal(
                text=max(letters, key=len),
                model_name=TEMPLATE_MODEL_NAME,
                context_level=CONTEXT_LEVEL_TEMPLATE,
            )

        if deadline_seconds < _MIN_MODEL_DEADLINE_SECONDS:
            return _template_only(deadline_seconds)

        diagnostics: dict = {}

        # Absolute, and computed HERE rather than inside _drain: the drain
        # may wait for a letter_executor thread first, and a deadline
        # started on the far side of that wait would ignore the queue time
        # -- defeating the caller's clamp to the remaining turn budget, and
        # letting a task that was queued (and by then abandoned) hold its
        # thread for the FULL window while others wait behind it. Against an
        # absolute mark, a drain that reaches a thread past its deadline
        # exits immediately instead.
        drain_deadline = started + deadline_seconds

        # External models only when both the denial's stored consent and
        # this chat session's allow them, applied to the in-memory denial
        # (never saved) BEFORE any model call: the summarizer below and
        # make_appeals' backup call list both route by denial.use_external.
        # Either one saying no is binding.
        denial.use_external = bool(denial.use_external) and bool(use_external)

        # Oversized denial text: use the wizard's summary of it, as its own
        # generation does, instead of sending the full text to every model
        # and leaning on the shed ladder. A cached or pre-warmed summary costs
        # nothing; computing one is a model call, so it gets at most a third
        # of the budget -- the time counts against drain_deadline either way.
        denial_text_override: Optional[str] = None
        try:
            from fighthealthinsurance.ml.ml_appeal_context_helper import (
                MLAppealContextHelper,
            )

            denial_text_override = await asyncio.wait_for(
                MLAppealContextHelper.maybe_summarize_denial_text(denial),
                timeout=min(20.0, deadline_seconds / 3),
            )
        except Exception as e:
            logger.info(
                f"chat letter: no denial summary for denial "
                f"{denial.denial_id} ({type(e).__name__}); using the full text"
            )
        # Again with the time the summary took: it can leave too little for
        # the drain to use any model answer, which the fan-out would still
        # pay for.
        seconds_left = drain_deadline - time.monotonic()
        if seconds_left < _MIN_MODEL_DEADLINE_SECONDS:
            return _template_only(seconds_left)

        def _drain() -> Optional[GeneratedAppeal]:
            """Blocking: run the models and pull the first usable letter."""
            best: Optional[GeneratedAppeal] = None
            for item in appealGenerator.make_appeals(
                denial,
                AppealTemplateGenerator([], [], []),
                # NOT passing medical_reasons: with an empty template
                # generator make_appeals would surface each raw reason
                # string as an "appeal". Chat context reaches the models
                # through denial.qa_context instead.
                non_ai_appeals=non_ai_appeals,
                specialized_templates=specialized_templates or None,
                diagnostics_sink=diagnostics,
                # Tags the persisted ModelCallAttempt rows so a chat-driven
                # generation can be told apart from the wizard's live run
                # and the background precompute when debugging a denial.
                run_kind="chat",
                deadline=drain_deadline,
                # None for a normal-sized denial: full context preferred.
                denial_text_override=denial_text_override,
            ):
                if not is_real_appeal(item.text):
                    continue
                # A model's full letter is accepted the moment it arrives
                # (chat shows ONE letter, so latency matters). Anything else
                # -- a template, a medically-necessary paragraph, a short
                # fragment -- only wins at exhaustion, longest first.
                if is_full_model_draft(item):
                    return item
                if best is None or len(item.text) > len(best.text):
                    best = item
            return best

        # thread_sensitive=False so concurrent drains don't serialize on one
        # shared thread; letter_executor (not bridge_executor) so a burst of
        # long drains -- a degraded-model period fires many fallbacks at
        # once -- is capped by its small pool instead of crowding out the
        # bridge pool's short hops.
        item: Optional[GeneratedAppeal] = await database_sync_to_async(
            _drain,
            thread_sensitive=False,
            executor=letter_executor,
        )()

        # make_appeals flushed what it knew before handing back its lazy
        # iterator; the drain above consumed more of it, so flush the late
        # per-model outcome records too.
        recorder = diagnostics.get("attempt_recorder")
        if recorder is not None:
            await database_sync_to_async(
                recorder.flush,
                thread_sensitive=False,
                executor=bridge_executor,
            )()

        logger.info(
            f"chat letter: generation for denial {denial.denial_id} "
            f"{'produced a letter' if item else 'produced nothing'} in "
            f"{time.monotonic() - started:.1f}s "
            f"(model={item.model_name if item else None}, "
            f"winning_stage={diagnostics.get('winning_stage')}, "
            f"models_tried={diagnostics.get('models_tried')})"
        )
        return item
    except Exception as e:
        logger.opt(exception=True).error(
            f"chat letter: generation failed for denial "
            f"{getattr(denial, 'denial_id', None)} after "
            f"{time.monotonic() - started:.1f}s: {type(e).__name__}"
        )
        return None


async def draft_letter_for_chat(
    *,
    appeal: Any,
    denial: Any,
    use_external: bool,
    prefer_existing: bool = False,
    use_reserve: bool = True,
    deadline_seconds: Optional[float] = None,
) -> Optional[DraftedLetter]:
    """Produce an appeal letter for a chat-linked appeal and persist it.

    ``prefer_existing=True`` serves an already-generated ProposedAppeal
    first and only generates when none exists -- the total-failure fallback
    uses it because a DB read is the one step guaranteed to work while
    models are down. The tool path generates first (the user just asked for
    a fresh draft) and falls back to the reserve. ``use_reserve=False``
    never serves one: a request for a new letter is not answered by a draft
    made before whatever the person wants changed.

    On success the letter -- placeholders filled exactly as the wizard fills
    them -- is saved to ``appeal.appeal_text``, EXCEPT that only a full
    model-written draft (is_full_model_draft) may replace a real letter
    already on the appeal: the user may have edited that one, and no prior
    version is kept. A reserve, a template or a short fragment is delivered
    in chat instead, as is any letter for an appeal already sent or faxed,
    whose text records what went out. Both checks read the appeal as it is
    now, not as it was when drafting began, and the save only lands if its
    letter is still the one checked. Returns a ``DraftedLetter`` (text plus
    how it relates to the appeal row), or None when no letter could be
    produced.

    No ProposedAppeal row is written for a generated letter. Live drafts
    belong to the denial's generation lease (generation_lease): the wizard
    and the appeal journey write them only while holding it, so two runs
    can't both fill the draft set. A chat-side insert would hold no lease,
    sit beside whatever the real holder is writing, and be served and
    counted by the wizard as one of its own. The chat's product is the
    letter on the appeal; the attempts the drain read are recorded as
    run_kind="chat" ModelCallAttempt rows, beside the drafting log line.
    """
    letter: Optional[str] = None
    generated_item: Optional["GeneratedAppeal"] = None
    prefer_existing = prefer_existing and use_reserve
    if prefer_existing:
        letter = await find_reserve_letter(denial)
    if not letter:
        generated_item = await generate_letter_for_denial(
            denial,
            use_external=use_external,
            deadline_seconds=deadline_seconds,
        )
        if generated_item:
            letter = generated_item.text
    if not letter and not prefer_existing and use_reserve:
        letter = await find_reserve_letter(denial)
    if not letter:
        return None
    letter = await fill_letter_placeholders(letter, denial)

    from fighthealthinsurance.models import Appeal

    appeal_id = appeal.id
    try:
        # The appeal as it is now: drafting can take the whole deadline, and
        # the person may have saved, sent or faxed a letter meanwhile.
        row = (
            await Appeal.objects.filter(id=appeal_id)
            .values("appeal_text", "sent", "fax_id")
            .afirst()
        )
        if row is None:
            logger.info(f"chat letter: appeal {appeal_id} is gone; not saving")
            return DraftedLetter(text=letter, saved_to_appeal=False)
        current = row["appeal_text"]
        if row["sent"] or row["fax_id"] is not None:
            logger.info(
                f"chat letter: appeal {appeal_id} was sent; serving the letter "
                f"without overwriting what went out"
            )
            return DraftedLetter(
                text=letter,
                saved_to_appeal=False,
                preserved_existing=True,
                appeal_sent=True,
            )
        if not is_full_model_draft(generated_item) and is_real_appeal(current):
            logger.info(
                f"chat letter: appeal {appeal_id} already has a real letter and "
                f"this one is not a full model draft; serving it without "
                f"overwriting"
            )
            return DraftedLetter(
                text=letter, saved_to_appeal=False, preserved_existing=True
            )
        # Only if the letter is still the one just checked: a save that lands
        # between that read and this write is never overwritten. mod_date is
        # auto_now, which update() doesn't apply, so it is set here.
        updated = await Appeal.objects.filter(
            id=appeal_id, appeal_text=current
        ).aupdate(appeal_text=letter, mod_date=datetime.date.today())
    except Exception:
        logger.opt(exception=True).warning(
            f"chat letter: could not save letter to appeal {appeal_id}; "
            f"delivering it unpersisted"
        )
        return DraftedLetter(text=letter, saved_to_appeal=False)
    if not updated:
        logger.info(
            f"chat letter: appeal {appeal_id}'s letter changed while drafting; "
            f"serving the new one without overwriting"
        )
        return DraftedLetter(
            text=letter, saved_to_appeal=False, preserved_existing=True
        )
    appeal.appeal_text = letter
    return DraftedLetter(text=letter, saved_to_appeal=True)

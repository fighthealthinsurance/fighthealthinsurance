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

import re
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, List, NamedTuple, Optional

from channels.db import database_sync_to_async
from loguru import logger

from fighthealthinsurance.exec import bridge_executor
from fighthealthinsurance.ml.ml_models import _env_float
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

# Placeholders the specialized static templates (and occasionally models)
# leave for downstream substitution; mirrors the fields
# AppealsBackendHelper.sub_in_appeals fills on the wizard path.
_SUBSTITUTABLE_FIELDS = ("insurance_company", "claim_id", "diagnosis", "procedure")

# UNKNOWN is the extractor's explicit "couldn't tell" marker; substituting it
# into a letter would be worse than leaving the fill-in-the-blank placeholder.
_UNSET_FIELD_VALUES = (None, "", "UNKNOWN")


class DraftedLetter(NamedTuple):
    """A produced appeal letter plus whether it reached the Appeal row.

    ``saved_to_appeal`` lets callers word their reply honestly: a letter
    whose ``appeal.asave()`` failed is still delivered, but must not be
    presented as "saved to Appeal #N".
    """

    text: str
    saved_to_appeal: bool


def looks_like_letter_request(text: Optional[str]) -> bool:
    """Whether a user message asks us to draft/write an appeal letter."""
    if not text:
        return False
    return bool(_LETTER_REQUEST_RE.search(text))


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


def substitute_denial_fields(letter: str, denial: Any) -> str:
    """Fill ``{insurance_company}``-style placeholders from the denial.

    Unknown fields keep their placeholder so the letter reads as a
    fill-in-the-blank draft rather than silently asserting wrong values.
    """
    result = letter
    for field in _SUBSTITUTABLE_FIELDS:
        value = getattr(denial, field, None)
        if value in _UNSET_FIELD_VALUES:
            continue
        result = result.replace("{" + field + "}", str(value))
    return result


async def find_reserve_letter(denial: Any) -> Optional[str]:
    """Best already-generated ProposedAppeal text for ``denial``, or None.

    Zero model calls: this is the rescue path for total model failure. Live
    (non-speculative) rows are preferred over the speculative precompute
    reserve; within a group the longest deliverable letter wins. Rows are
    only read -- reserve promotion bookkeeping belongs to the appeal wizard
    flow (AppealsBackendHelper), not chat.
    """
    from fighthealthinsurance.models import ProposedAppeal

    rows = [
        row
        async for row in ProposedAppeal.objects.filter(for_denial=denial)
        .exclude(appeal_text__isnull=True)
        .order_by("speculative", "-created_at")[:10]
    ]
    for speculative_group in (False, True):
        candidates = [
            row.appeal_text
            for row in rows
            if row.speculative == speculative_group and is_real_appeal(row.appeal_text)
        ]
        if candidates:
            return max(candidates, key=len)
    return None


async def generate_letter_for_denial(
    denial: Any,
    *,
    use_external: bool = False,
    deadline_seconds: Optional[float] = None,
) -> Optional["GeneratedAppeal"]:
    """Run the appeal-generation pipeline for ``denial`` and return one letter.

    Blocking model work (``make_appeals`` runs its ladder synchronously and
    returns a lazy iterator whose ``next()`` blocks on model futures) is
    bridged onto ``bridge_executor``, same as the speculative precompute.
    The first deliverable model-written letter wins (completion order == the
    fastest healthy model, which is what a waiting chat user needs); the
    specialized static templates serve as the zero-model fallback.

    Returns the winning ``GeneratedAppeal`` (its ``text`` already has known
    denial fields substituted) so callers can persist real provenance.
    ``use_external`` is applied to the in-memory denial only -- it reflects
    this chat session's consent and must not rewrite the denial's stored
    opt-in. Never raises; a failed run returns None.
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
                    f"{template.name}: {e}"
                )

        diagnostics: dict = {}

        # A model item at least this long is accepted the moment it arrives
        # (chat shows ONE letter, so latency matters); anything shorter --
        # e.g. a medically_necessary one-liner passed through the empty
        # template generator -- only wins at exhaustion if nothing longer
        # (a full letter, a specialized static template) showed up.
        min_letter_chars = int(_env_float("FHI_CHAT_LETTER_MIN_CHARS", 350.0))

        def _drain() -> Optional[GeneratedAppeal]:
            """Blocking: run the models and pull the first usable letter.

            Chat-session consent (in-memory only; never saved -- make_appeals
            reads use_external for its backup call list).
            """
            denial.use_external = use_external
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
                deadline=time.monotonic() + deadline_seconds,
            ):
                if not is_real_appeal(item.text):
                    continue
                if item.model_name and len(item.text) >= min_letter_chars:
                    return item
                if best is None or len(item.text) > len(best.text):
                    best = item
            return best

        # thread_sensitive=False + bridge_executor: a minutes-long drain must
        # not serialize behind (or starve) other bridged hops -- same shape
        # as SpeculativeAppealsHelper._generate_drafts.
        item: Optional[GeneratedAppeal] = await database_sync_to_async(
            _drain,
            thread_sensitive=False,
            executor=bridge_executor,
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
        if item:
            return replace(item, text=substitute_denial_fields(item.text, denial))
        return None
    except Exception as e:
        logger.opt(exception=True).error(
            f"chat letter: generation failed for denial "
            f"{getattr(denial, 'denial_id', None)} after "
            f"{time.monotonic() - started:.1f}s: {e}"
        )
        return None


async def draft_letter_for_chat(
    *,
    appeal: Any,
    denial: Any,
    use_external: bool,
    prefer_existing: bool = False,
    deadline_seconds: Optional[float] = None,
) -> Optional[DraftedLetter]:
    """Produce an appeal letter for a chat-linked appeal and persist it.

    ``prefer_existing=True`` serves an already-generated ProposedAppeal
    first and only generates when none exists -- the total-failure fallback
    uses it because a DB read is the one step guaranteed to work while
    models are down. The tool path generates first (the user just asked for
    a fresh draft) and falls back to the reserve.

    On success the letter is saved to ``appeal.appeal_text`` and, for a
    newly generated letter, recorded as a ProposedAppeal row for the same
    provenance the wizard flow gets. Returns a ``DraftedLetter`` (text plus
    whether the appeal save succeeded), or None when no letter could be
    produced.
    """
    from fighthealthinsurance.models import ProposedAppeal

    letter: Optional[str] = None
    generated_item: Optional["GeneratedAppeal"] = None
    if prefer_existing:
        reserve = await find_reserve_letter(denial)
        if reserve:
            letter = substitute_denial_fields(reserve, denial)
    if not letter:
        generated_item = await generate_letter_for_denial(
            denial,
            use_external=use_external,
            deadline_seconds=deadline_seconds,
        )
        if generated_item:
            letter = generated_item.text
    if not letter and not prefer_existing:
        reserve = await find_reserve_letter(denial)
        if reserve:
            letter = substitute_denial_fields(reserve, denial)
    if not letter:
        return None

    if generated_item:
        try:
            await ProposedAppeal.objects.acreate(
                appeal_text=letter,
                for_denial=denial,
                model_name=generated_item.model_name,
                synthesized=generated_item.synthesized,
                context_level=generated_item.context_level,
            )
        except Exception:
            # Provenance only -- the user still gets their letter.
            logger.opt(exception=True).warning(
                f"chat letter: could not record ProposedAppeal for denial "
                f"{getattr(denial, 'denial_id', None)}"
            )

    saved_to_appeal = False
    try:
        appeal.appeal_text = letter
        await appeal.asave()
        saved_to_appeal = True
    except Exception:
        logger.opt(exception=True).warning(
            f"chat letter: could not save letter to appeal "
            f"{getattr(appeal, 'id', None)}; delivering it unpersisted"
        )
    return DraftedLetter(text=letter, saved_to_appeal=saved_to_appeal)

"""
Background task generation for the Chooser (Best-Of Selection) system.

This module provides utilities for:
- Checking and refilling the pool of READY ChooserTasks
- Generating synthetic candidates using existing ML pipelines

Note on external models: chooser tasks are built ONLY from synthetic,
model-generated scenarios (no patient data), so candidate generation opts
into external backends (``use_external=True``). The chooser exists precisely
to compare backends against each other — leaving external providers
(Anthropic, Azure, DeepInfra, ...) out of it meant they never appeared in the
selection UI or the model-usage dashboard, which is what happened when these
calls relied on the router's internal-only default.
"""

import asyncio
import datetime
import random
import re
import threading
from typing import List, Optional, cast

from django.conf import settings
from django.utils import timezone

from channels.db import database_sync_to_async
from loguru import logger

from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.ml.model_identity import (
    SYNTHESIZED_MODEL_NAME,
    canonical_model_name,
)
from fighthealthinsurance.models import ChooserCandidate, ChooserTask
from fighthealthinsurance.utils import fire_and_forget_in_new_threadpool

# Configuration for auto-refill thresholds
CHOOSER_MIN_READY_TASKS = getattr(settings, "CHOOSER_MIN_READY_TASKS", 50)
# Minimum number of READY synthetic tasks of each type that still have zero
# votes ("unscored"). The READY pool can stay large while every task has
# already been voted on, so we back-fill whenever fresh, unscored tasks run
# low to keep collecting preference data.
CHOOSER_MIN_UNSCORED_TASKS = getattr(settings, "CHOOSER_MIN_UNSCORED_TASKS", 5)
CHOOSER_GENERATION_BATCH_SIZE = getattr(settings, "CHOOSER_GENERATION_BATCH_SIZE", 10)
CHOOSER_NUM_CANDIDATES = getattr(settings, "CHOOSER_NUM_CANDIDATES", 4)
# Whether to add a synthesized candidate (combining the per-model outputs)
# to each task so the chooser can measure synthesis vs. single models.
CHOOSER_INCLUDE_SYNTHESIS = getattr(settings, "CHOOSER_INCLUDE_SYNTHESIS", True)
# Ceiling for the coverage trigger (see check_and_refill_task_pool): once this
# many fresh tasks exist, a backend that still has too few is not worth
# another batch.
CHOOSER_MAX_UNSCORED_TASKS = getattr(
    settings, "CHOOSER_MAX_UNSCORED_TASKS", 2 * CHOOSER_MIN_READY_TASKS
)
# How often the chat fallback may re-ask for a single question before the task
# is given up (see _ask_for_single_question).
CHOOSER_FALLBACK_ATTEMPTS = getattr(settings, "CHOOSER_FALLBACK_ATTEMPTS", 3)
# At most one background prefill per process per this many seconds (see
# trigger_prefill_async): a page load and an empty next-task fetch both ask.
CHOOSER_PREFILL_THROTTLE_SECONDS = getattr(
    settings, "CHOOSER_PREFILL_THROTTLE_SECONDS", 60
)
# A QUEUED task younger than this marks a generation still running somewhere
# (see _generation_underway); an older one was left behind by a process that
# died mid-generation, and no longer holds prefills off.
CHOOSER_GENERATION_CLAIM_SECONDS = getattr(
    settings, "CHOOSER_GENERATION_CLAIM_SECONDS", 15 * 60
)


def _model_display_name(model) -> str:
    """The friendly registry name stamped by MLRouter, with a str() fallback
    (RemoteModelLike.__str__ never yields an opaque object repr)."""
    return getattr(model, "name", None) or str(model)


def _select_candidate_models(models: List, limit: int) -> List:
    """Pick up to ``limit`` distinct-by-name backends for candidate generation.

    The router's backend lists intentionally repeat models (e.g. the primary
    fhi model twice for chat) and lead with internal models; taking a plain
    prefix slice would burn candidate slots on duplicates and leave external
    backends unmeasured. Instead: dedupe by friendly name, then alternate
    internal/external so both pools get compared, shuffling the external pool
    so different external providers get coverage across tasks.
    """
    internal: List = []
    external: List = []
    seen: set = set()
    for m in models:
        name = _model_display_name(m)
        if name in seen:
            continue
        seen.add(name)
        if getattr(m, "external", False):
            external.append(m)
        else:
            internal.append(m)
    # Randomize external order for coverage across tasks; keep the internal
    # ordering (cost/quality ranked by the router).
    random.shuffle(external)
    selected: List = []
    i = e = 0
    while len(selected) < limit and (i < len(internal) or e < len(external)):
        if i < len(internal):
            selected.append(internal[i])
            i += 1
        if len(selected) < limit and e < len(external):
            selected.append(external[e])
            e += 1
    return selected


_LABEL_MARKUP_RE = re.compile(r"^[\s\-\*\u2022\d\.\)\(#>]+")


def _clean_label_line(raw: str) -> str:
    """A line with its list bullet, numbering and markdown emphasis removed,
    so "**Procedure:** MRI", "1. Procedure: MRI" and "- Procedure: MRI" all
    read as "Procedure: MRI". Models decorate labels freely; the old parser
    required the bare form and treated every other spelling as a missing
    field."""
    line = _LABEL_MARKUP_RE.sub("", raw.strip())
    return line.strip("*_` ").strip()


def _labeled_fields(text: str) -> dict:
    """``{label: value}`` for every "Label: value" line of ``text``. Labels
    are lower-cased with spaces as underscores; markdown is stripped from
    both sides."""
    fields: dict = {}
    for raw in text.split("\n"):
        line = _clean_label_line(raw)
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().strip("*_` ").strip().lower().replace(" ", "_")
        value = value.strip().strip("*_` ").strip()
        if key and value:
            fields[key] = value
    return fields


def _parse_conversation(text: str):
    """``(history, final_user_prompt)`` from a USER:/ASSISTANT: transcript.

    Every turn goes into the history in order, and only a trailing user turn
    comes back out, as the question the candidates answer (None when the
    transcript ends with the assistant). A user turn used to be held back
    whenever it followed an assistant turn, so in a transcript with two
    answered follow-ups the middle question was dropped and two assistant
    turns sat side by side. Labels may carry list or markdown decoration
    ("**USER:**"); continuation lines are kept as written.
    """
    history: list = []
    current_role = None
    current_content: list = []

    def flush():
        content = " ".join(current_content).strip()
        if current_role and content:
            history.append({"role": current_role, "content": content})

    for raw in text.strip().split("\n"):
        cleaned = _clean_label_line(raw)
        upper = cleaned.upper()
        if upper.startswith("USER:"):
            flush()
            current_role = "user"
            current_content = [cleaned[5:].strip("*_` ").strip()]
        elif upper.startswith("ASSISTANT:"):
            flush()
            current_role = "assistant"
            current_content = [cleaned[10:].strip("*_` ").strip()]
        elif current_role and raw.strip():
            current_content.append(raw.strip())
    flush()
    if not history or history[-1]["role"] != "user":
        return history, None
    final_user_prompt = history.pop()["content"]
    return history, final_user_prompt


def _scenario_writer(models: List):
    """The backend that writes a task's synthetic scenario or conversation.

    The router's generation list starts with the cheapest internal backend,
    which in production is the fhi-legacy appeal fine-tune. It does not
    follow instructions (its own docstring: blank lines and stray digits), so
    every scenario request either came back empty, which raised in the parser
    and disabled the task before a single candidate existed, or came back as
    text no field could be read from. Take the first backend that says it
    follows general instructions; only when none does, fall back to the first
    backend rather than produce nothing.
    """
    for model in models:
        supports = getattr(model, "supports_general_instructions", None)
        if supports is None or supports():
            return model
    logger.warning(
        "Chooser: no general-purpose backend is registered; writing the "
        f"scenario with {_model_display_name(models[0])}"
    )
    return models[0]


async def _ask_for_single_question(model, prompt: str) -> Optional[str]:
    """Up to ``CHOOSER_FALLBACK_ATTEMPTS`` calls; None when none answered.

    The model call returns None on every way of not answering (transport
    failure, cooldown, 429 back-off) rather than raising, so the retry has to
    be bounded here: unbounded, it spun the refill thread forever against a
    backend that was down.
    """
    for attempt in range(1, CHOOSER_FALLBACK_ATTEMPTS + 1):
        answer = await model._infer_no_context(
            system_prompts=["Generate a natural user question about health insurance."],
            prompt=prompt,
        )
        if answer and str(answer).strip():
            return str(answer)
        logger.info(
            f"Chooser: {_model_display_name(model)} returned no question "
            f"(attempt {attempt}/{CHOOSER_FALLBACK_ATTEMPTS})"
        )
    return None


def _note_unusable_candidate(task: ChooserTask, model, kind: str, response) -> None:
    """A backend answered with nothing usable. Logged, because the retry
    sweep fills the slot with whichever backend does answer: without this a
    task whose external backends both failed went READY with internal
    candidates only and no trace of why."""
    size = len(response.strip()) if isinstance(response, str) else 0
    logger.info(
        f"Chooser task {task.id}: {_model_display_name(model)} returned no "
        f"usable {kind} candidate ({size} chars)"
    )


# Serialises refills within a process. A refill now awaits its whole batch,
# so this is held for the batch's duration. The cache "lock" it replaces was
# released the moment the batch had been handed to a background thread, and
# was process-local anyway (Prod's cache is LocMemCache), so it never kept
# two batches apart. Across processes the refill actor is one named Ray
# actor, and its run loop is started once (see BaseActorRef.get).
_refill_guard = threading.Lock()
_refill_in_progress = False


def _claim_refill() -> bool:
    global _refill_in_progress
    with _refill_guard:
        if _refill_in_progress:
            return False
        _refill_in_progress = True
        return True


def _release_refill() -> None:
    global _refill_in_progress
    with _refill_guard:
        _refill_in_progress = False


async def check_and_refill_task_pool():
    """Generate a batch of tasks for every type whose pool needs one.

    Three triggers, checked in order (see ``_refill_reason``): the READY pool
    is short, fresh (unvoted) tasks are short, or an external backend the
    chooser would compare today has no fresh tasks comparing it. The third is
    what lets a newly configured provider into the pool: READY is monotonic
    (nothing moves a task out of it), so without it a pool bootstrapped before
    the provider existed never built a task for it.

    Awaits the batch, so a second tick cannot start another batch while one
    is still running; a tick that finds a refill in progress returns at once.
    """
    if not _claim_refill():
        logger.debug("A chooser refill is already running in this process; skipping")
        return
    try:
        for task_type in ["appeal", "chat"]:
            reason = await _refill_reason(task_type)
            if reason is None:
                continue
            logger.info(
                f"Chooser {task_type} tasks need generating ({reason}). "
                f"Generating {CHOOSER_GENERATION_BATCH_SIZE} tasks."
            )
            await _generate_batch_tasks(task_type, CHOOSER_GENERATION_BATCH_SIZE)
    finally:
        _release_refill()


async def _refill_reason(task_type: str) -> Optional[str]:
    """Why ``task_type`` needs a batch, or None when the pool is healthy."""
    ready_count = await _count_ready_tasks(task_type)
    if ready_count < CHOOSER_MIN_READY_TASKS:
        return f"ready={ready_count}/{CHOOSER_MIN_READY_TASKS}"
    unscored_count = await _count_unscored_tasks(task_type)
    if unscored_count < CHOOSER_MIN_UNSCORED_TASKS:
        return f"unscored={unscored_count}/{CHOOSER_MIN_UNSCORED_TASKS}"
    # Coverage refills are capped: a backend that never returns a usable
    # candidate would otherwise justify a batch every tick, forever.
    if unscored_count >= CHOOSER_MAX_UNSCORED_TASKS:
        return None
    uncovered = await _external_backends_without_fresh_tasks(task_type)
    if uncovered:
        return f"no fresh tasks compare {sorted(uncovered)}"
    return None


def _comparable_backends(task_type: str) -> List:
    """The backends candidate generation draws from for ``task_type``."""
    if task_type == "appeal":
        return cast(List, ml_router.generate_text_backends(use_external=True))
    return cast(List, ml_router.get_chat_backends(use_external=True))


async def _fresh_task_coverage(task_type: str) -> dict:
    """How many fresh (READY, synthetic, unvoted) tasks of ``task_type`` hold
    a candidate from each backend, keyed by the persisted model name."""
    from django.db.models import Count

    fresh_ids = (
        ChooserTask.objects.filter(
            task_type=task_type, status="READY", source="synthetic"
        )
        .annotate(vote_count=Count("votes"))
        .filter(vote_count=0)
        .values("id")
    )
    coverage: dict = {}
    # .order_by() clears the Meta ordering, which would otherwise leak into
    # the GROUP BY.
    async for row in (
        ChooserCandidate.objects.filter(task_id__in=fresh_ids, is_active=True)
        .order_by()
        .values("model_name")
        .annotate(tasks=Count("task", distinct=True))
    ):
        coverage[row["model_name"]] = row["tasks"]
    return coverage


async def _external_backends_without_fresh_tasks(task_type: str) -> set:
    """External backends the chooser would compare today that fewer than
    ``CHOOSER_MIN_UNSCORED_TASKS`` fresh tasks actually compare.

    Externals only: the internal backends fill their candidate slots on every
    task, while the externals rotate through theirs, and comparing providers
    is what the chooser is for.
    """
    try:
        backends = _comparable_backends(task_type)
    except Exception as e:
        logger.warning(f"Chooser: could not list backends for {task_type}: {e}")
        return set()
    external = {
        _model_display_name(m) for m in backends if getattr(m, "external", False)
    }
    if not external:
        return set()
    coverage = await _fresh_task_coverage(task_type)
    return {
        name for name in external if coverage.get(name, 0) < CHOOSER_MIN_UNSCORED_TASKS
    }


async def _count_ready_tasks(task_type: str) -> int:
    """Count the number of READY tasks for a given type."""

    return cast(
        int,
        await database_sync_to_async(
            ChooserTask.objects.filter(task_type=task_type, status="READY").count
        )(),
    )


async def _count_unscored_tasks(task_type: str) -> int:
    """Count READY synthetic tasks of a type that have not yet received a vote.

    These are the tasks still able to gather fresh preference data; once a
    task has any vote it is "scored" and contributes diminishing value, so we
    track unscored tasks separately from the overall READY pool.
    """

    from django.db.models import Count

    return cast(
        int,
        await database_sync_to_async(
            ChooserTask.objects.filter(
                task_type=task_type, status="READY", source="synthetic"
            )
            .annotate(vote_count=Count("votes"))
            .filter(vote_count=0)
            .count
        )(),
    )


async def _generate_batch_tasks(task_type: str, batch_size: int):
    """Generate a batch of chooser tasks with candidates."""
    for _ in range(batch_size):
        try:
            await _generate_single_task(task_type)
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error generating chooser task of type {task_type}: {e}"
            )


async def _generate_single_task(task_type: str):
    """
    Generate a single ChooserTask with candidates.

    For appeals: generates multiple synthetic appeal letters for a sample context
    For chat: generates multiple synthetic chat responses for a sample prompt
    """

    # Create the task in QUEUED state
    task = await database_sync_to_async(ChooserTask.objects.create)(
        task_type=task_type,
        status="QUEUED",
        source="synthetic",
        num_candidates_expected=CHOOSER_NUM_CANDIDATES,
        num_candidates_generated=0,
    )

    try:
        if task_type == "appeal":
            await _generate_appeal_candidates(task)
        else:
            await _generate_chat_candidates(task)

        # Mark task as READY if we generated enough candidates. The counter
        # was persisted by the generator's end-of-run save (not once per
        # candidate); this save records the final status.
        if task.num_candidates_generated >= 2:  # Minimum 2 candidates needed
            task.status = "READY"
            await database_sync_to_async(task.save)()
            logger.info(
                f"ChooserTask {task.id} is now READY with {task.num_candidates_generated} candidates"
            )
        else:
            task.status = "DISABLED"
            await database_sync_to_async(task.save)()
            logger.warning(
                f"ChooserTask {task.id} disabled - insufficient candidates generated"
            )

    except Exception as e:
        logger.opt(exception=True).error(
            f"Error generating candidates for task {task.id}: {e}"
        )
        task.status = "DISABLED"
        await database_sync_to_async(task.save)()


async def _generate_appeal_candidates(task: ChooserTask):
    """Generate appeal letter candidates for a task using ONLY synthetic data from ML."""

    # Use ML to generate a synthetic denial scenario. Synthetic data only, so
    # external backends are allowed (and wanted — see module docstring).
    generation_models = ml_router.generate_text_backends(use_external=True)
    if not generation_models:
        logger.warning("No models available for generating synthetic denial scenarios")
        # Cannot proceed without models - mark task as disabled
        task.status = "DISABLED"
        await database_sync_to_async(task.save)()
        return

    # The scenario writer must follow instructions; the cheapest internal
    # backend (fhi-legacy) does not. See _scenario_writer.
    scenario_model = _scenario_writer(generation_models)
    try:
        scenario_prompt = (
            "We are building a system to help patients appeal health insurance denials. "
            "To improve our model selection techniques, we need realistic synthetic denial scenarios. "
            "Please generate a varied, realistic but completely fictional health insurance denial scenario.\n\n"
            "Provide the following in EXACTLY this structured format (one field per line):\n"
            "Procedure: [a specific real medical procedure name, e.g., MRI of lumbar spine, arthroscopic knee surgery]\n"
            "Diagnosis: [a specific real medical diagnosis, e.g., chronic lower back pain, torn ACL]\n"
            "Insurance Company: [a fictional but realistic-sounding insurance company name]\n"
            "Denial Reason: [a 1-2 sentence realistic denial reason, such as 'not medically necessary', 'experimental treatment', 'out of network', etc.]\n\n"
            "Be creative and varied. Use real medical terminology for procedures and diagnoses."
        )

        scenario_response = await scenario_model._infer_no_context(
            system_prompts=[
                "You are a system that generates realistic but fictional health insurance scenarios. "
                "These are used to improve model selection for a health insurance appeal assistance service. "
                "Generate varied, realistic scenarios using real medical terminology."
            ],
            prompt=scenario_prompt,
        )

        # A backend that did not answer returns None rather than raising; the
        # parser reads it as "no fields" and the task is disabled below.
        fields = _labeled_fields(scenario_response or "")
        context = {
            "procedure": fields.get("procedure"),
            "diagnosis": fields.get("diagnosis"),
            "insurance_company": fields.get("insurance_company"),
            "denial_text_preview": fields.get("denial_reason"),
        }
        missing = [k for k, v in context.items() if not v]
        if missing:
            # No placeholders: a task about "Medical procedure" for "Medical
            # condition" measures nothing, and it used to go READY and reach
            # voters. Disable it; the next batch tries again.
            preview = (scenario_response or "")[:200]
            logger.warning(
                f"ChooserTask {task.id}: scenario from "
                f"{_model_display_name(scenario_model)} is missing {missing}; "
                f"disabling the task. Response started: {preview!r}"
            )
            task.status = "DISABLED"
            await database_sync_to_async(task.save)()
            return

        task.context_json = context
        await database_sync_to_async(task.save)()

    except Exception as e:
        logger.opt(exception=True).warning(
            f"Error generating synthetic denial scenario: {e}"
        )
        # Cannot proceed without a valid scenario - mark task as disabled
        task.status = "DISABLED"
        await database_sync_to_async(task.save)()
        return

    # Generate candidates using different models. Synthetic context only, so
    # external backends (Anthropic/Azure/DeepInfra/...) participate — the chooser
    # is how they get compared and how they show up in usage reporting.
    models = _select_candidate_models(
        ml_router.generate_text_backends(use_external=True), CHOOSER_NUM_CANDIDATES
    )
    if not models:
        logger.warning("No models available for appeal candidate generation")
        return
    logger.debug(
        f"Chooser appeal candidates for task {task.id} using models: "
        f"{[_model_display_name(m) for m in models]}"
    )

    prompt = _build_appeal_prompt(task.context_json)

    candidate_index = 0
    # First pass: try each model once
    for model in models:
        try:
            response = await model._infer_no_context(
                system_prompts=[
                    "You are an expert at writing health insurance appeal letters. "
                    "Write a professional, compelling appeal letter based on the given context."
                ],
                prompt=prompt,
            )
            if response and len(response.strip()) > 100:
                await database_sync_to_async(ChooserCandidate.objects.create)(
                    task=task,
                    candidate_index=candidate_index,
                    kind="appeal_letter",
                    model_name=canonical_model_name(model),
                    content=response.strip(),
                    metadata={"source": "synthetic"},
                )
                candidate_index += 1
                task.num_candidates_generated = candidate_index
            else:
                _note_unusable_candidate(task, model, "appeal", response)
        except Exception as e:
            logger.warning(f"Error generating appeal candidate with model {model}: {e}")

    # If we don't have enough candidates, retry with the same models
    retry_count = 0
    max_retries = CHOOSER_NUM_CANDIDATES * 2  # Limit total retries
    while candidate_index < CHOOSER_NUM_CANDIDATES and retry_count < max_retries:
        for model in models:
            if candidate_index >= CHOOSER_NUM_CANDIDATES:
                break
            try:
                response = await model._infer_no_context(
                    system_prompts=[
                        "You are an expert at writing health insurance appeal letters. "
                        "Write a professional, compelling appeal letter based on the given context. "
                        "Be creative and write a unique response different from previous attempts."
                    ],
                    prompt=prompt,
                )
                if response and len(response.strip()) > 100:
                    await database_sync_to_async(ChooserCandidate.objects.create)(
                        task=task,
                        candidate_index=candidate_index,
                        kind="appeal_letter",
                        model_name=canonical_model_name(model),
                        content=response.strip(),
                        metadata={"source": "synthetic", "retry": True},
                    )
                    candidate_index += 1
                    task.num_candidates_generated = candidate_index
                else:
                    _note_unusable_candidate(task, model, "appeal", response)
            except Exception as e:
                logger.warning(
                    f"Error generating appeal candidate (retry) with model {model}: {e}"
                )
            retry_count += 1

    # One save covers every candidate generated above; candidates are
    # created durably as they complete, only the counter waits.
    await database_sync_to_async(task.save)()

    # Add a synthesized candidate combining the per-model drafts.
    await _maybe_add_synthesized_candidate(task, "appeal_letter")


async def _generate_chat_candidates(task: ChooserTask):
    """Generate chat response candidates for a task using ONLY synthetic data from ML."""

    # Generate synthetic chat prompt using ML
    # Use ML to generate a synthetic conversation with some back-and-forth.
    # Synthetic data only, so external backends are allowed (see module docstring).
    generation_models = ml_router.generate_text_backends(use_external=True)
    if not generation_models:
        logger.warning("No models available for generating synthetic chat prompts")
        # Cannot proceed without models - mark task as disabled
        task.status = "DISABLED"
        await database_sync_to_async(task.save)()
        return

    # The conversation writer must follow instructions; the cheapest internal
    # backend (fhi-legacy) does not. See _scenario_writer.
    prompt_model = _scenario_writer(generation_models)
    try:
        # Generate a multi-turn conversation scenario
        conversation_prompt = (
            "We are building a health insurance assistance chatbot. "
            "To improve our model selection techniques, we need realistic conversation scenarios.\n\n"
            "Please generate a short realistic conversation between a user and an assistant about ONE of these topics:\n"
            "- Health insurance claim denials and appeals\n"
            "- Prior authorization requests and denials\n"
            "- Medicare eligibility, coverage, or appeals\n"
            "- Medicaid eligibility, enrollment, or coverage issues\n\n"
            "The conversation should have 2-4 exchanges (user message, then assistant response, then user follow-up, etc.).\n"
            "End with a user question that the assistant has NOT yet answered.\n\n"
            "Format EXACTLY like this (use these exact labels):\n"
            "USER: [first user message]\n"
            "ASSISTANT: [assistant response]\n"
            "USER: [follow-up question - this is the one we want candidate responses for]\n\n"
            "Make the conversation natural and include specific details (procedures, conditions, timeframes, etc.)."
        )

        conversation_response = await prompt_model._infer_no_context(
            system_prompts=[
                "You are a system that generates realistic chat conversations about health insurance, "
                "Medicare, Medicaid, and prior authorizations for improving model selection. "
                "Generate varied, natural-sounding conversations."
            ],
            prompt=conversation_prompt,
        )

        # A backend that did not answer returns None rather than raising.
        history, final_user_prompt = _parse_conversation(conversation_response or "")

        # Validate we have a usable conversation
        if not final_user_prompt or len(final_user_prompt) < 10:
            # Fallback: try generating a simple single question
            logger.warning(
                f"ChooserTask {task.id}: could not parse a conversation from "
                f"{_model_display_name(prompt_model)}; falling back to a single question"
            )
            simple_prompt = (
                "Generate a realistic 1-2 sentence question someone might ask about one of:\n"
                "- Appealing a health insurance denial\n"
                "- Prior authorization problems\n"
                "- Medicare or Medicaid eligibility\n"
                "Just the question, nothing else."
            )
            final_user_prompt = await _ask_for_single_question(
                prompt_model, simple_prompt
            )
            if not final_user_prompt:
                logger.warning(
                    f"ChooserTask {task.id}: no usable question after "
                    f"{CHOOSER_FALLBACK_ATTEMPTS} attempts; disabling the task"
                )
                task.status = "DISABLED"
                await database_sync_to_async(task.save)()
                return
            final_user_prompt = final_user_prompt.strip().strip('"').strip("'").strip()
            history = []

        if len(final_user_prompt) < 10 or len(final_user_prompt) > 1000:
            task.status = "DISABLED"
            await database_sync_to_async(task.save)()
            return

        task.context_json = {
            "prompt": final_user_prompt,
            "history": history,
        }
        await database_sync_to_async(task.save)()

    except Exception as e:
        logger.opt(exception=True).warning(
            f"Error generating synthetic chat prompt: {e}"
        )
        # Cannot proceed without a valid prompt - mark task as disabled
        task.status = "DISABLED"
        await database_sync_to_async(task.save)()
        return

    # Generate candidates using different models. Synthetic context only, so
    # external backends participate (they are what the chooser compares).
    models = _select_candidate_models(
        ml_router.get_chat_backends(use_external=True), CHOOSER_NUM_CANDIDATES
    )
    if not models:
        logger.warning("No models available for chat candidate generation")
        return
    logger.debug(
        f"Chooser chat candidates for task {task.id} using models: "
        f"{[_model_display_name(m) for m in models]}"
    )

    # Extract history for the chat models
    chat_history = task.context_json.get("history", [])
    user_prompt = task.context_json.get("prompt", "")

    candidate_index = 0
    for model in models:
        try:
            # NOTE: the parameter is current_message_for_llm — passing
            # current_message= raised TypeError for EVERY backend and silently
            # produced zero chat candidates.
            response, _ = await model.generate_chat_response(
                current_message_for_llm=user_prompt,
                previous_context_summary=None,
                history=chat_history,
                is_professional=True,
                is_logged_in=True,
            )
            if response and len(response.strip()) > 50:
                await database_sync_to_async(ChooserCandidate.objects.create)(
                    task=task,
                    candidate_index=candidate_index,
                    kind="chat_response",
                    model_name=canonical_model_name(model),
                    content=response.strip(),
                    metadata={
                        "source": "synthetic",
                        "has_history": len(chat_history) > 0,
                    },
                )
                candidate_index += 1
                task.num_candidates_generated = candidate_index
            else:
                _note_unusable_candidate(task, model, "chat", response)
        except Exception as e:
            logger.warning(f"Error generating chat candidate with model {model}: {e}")

    # If we don't have enough candidates, retry with the same models
    retry_count = 0
    max_retries = CHOOSER_NUM_CANDIDATES * 2  # Limit total retries
    while candidate_index < CHOOSER_NUM_CANDIDATES and retry_count < max_retries:
        for model in models:
            if candidate_index >= CHOOSER_NUM_CANDIDATES:
                break
            try:
                response, _ = await model.generate_chat_response(
                    current_message_for_llm=user_prompt,
                    previous_context_summary=None,
                    history=chat_history,
                    is_professional=True,
                    is_logged_in=True,
                )
                if response and len(response.strip()) > 50:
                    await database_sync_to_async(ChooserCandidate.objects.create)(
                        task=task,
                        candidate_index=candidate_index,
                        kind="chat_response",
                        model_name=canonical_model_name(model),
                        content=response.strip(),
                        metadata={
                            "source": "synthetic",
                            "has_history": len(chat_history) > 0,
                            "retry": True,
                        },
                    )
                    candidate_index += 1
                    task.num_candidates_generated = candidate_index
                else:
                    _note_unusable_candidate(task, model, "chat", response)
            except Exception as e:
                logger.warning(
                    f"Error generating chat candidate (retry) with model {model}: {e}"
                )
            retry_count += 1

    # One save covers every candidate generated above; candidates are
    # created durably as they complete, only the counter waits.
    await database_sync_to_async(task.save)()

    # Add a synthesized candidate combining the per-model responses.
    await _maybe_add_synthesized_candidate(task, "chat_response")


def _build_appeal_prompt(context: dict) -> str:
    """Build a prompt for generating appeal letters."""
    parts = ["Write an appeal letter for the following health insurance denial:"]

    if context.get("procedure"):
        parts.append(f"Procedure: {context['procedure']}")
    if context.get("diagnosis"):
        parts.append(f"Diagnosis: {context['diagnosis']}")
    if context.get("insurance_company"):
        parts.append(f"Insurance Company: {context['insurance_company']}")
    if context.get("denial_text_preview"):
        parts.append(f"Denial Summary: {context['denial_text_preview']}")

    parts.append(
        "\nPlease write a professional appeal letter that argues why this treatment "
        "should be covered. Include relevant medical reasoning and cite any applicable "
        "guidelines or regulations."
    )

    return "\n".join(parts)


async def _maybe_add_synthesized_candidate(task: ChooserTask, kind: str) -> None:
    """Add one synthesized candidate combining the task's existing candidates.

    Synthesis needs >=2 base candidates to be meaningful (with a single input
    models tend to echo it verbatim), mirroring the real appeal flow. The
    synthesized candidate is stored with ``synthesized=True`` and
    ``model_name="synthesized"`` so it is tracked and bucketed distinctly when
    measuring synthesis against single models.
    """
    if not CHOOSER_INCLUDE_SYNTHESIS:
        return

    # Best-effort: synthesis runs AFTER the base candidates are already
    # persisted, so nothing here may propagate. Otherwise a transient error
    # (e.g. a DB read/count hiccup) would bubble into _generate_single_task's
    # except and flip an otherwise-valid task to DISABLED, orphaning its good
    # base candidates. Contain everything and simply skip on error.
    try:
        existing: List[ChooserCandidate] = [
            candidate
            async for candidate in ChooserCandidate.objects.filter(
                task=task, is_active=True, synthesized=False
            ).order_by("candidate_index")
        ]
        # Dedupe identical drafts so two copies of the same text don't count as
        # two distinct inputs (synthesizing from identical drafts is pointless).
        contents = list(
            dict.fromkeys(
                c.content.strip() for c in existing if c.content and c.content.strip()
            )
        )
        if len(contents) < 2:
            # Not enough distinct drafts to synthesize from.
            return

        context = task.context_json or {}
        if kind == "appeal_letter":
            synthesized_text = await _synthesize_appeal_candidate(context, contents)
        else:
            synthesized_text = await _synthesize_chat_candidate(context, contents)

        if not synthesized_text:
            return
        normalized = synthesized_text.strip()
        # Hold the synthesized candidate to the same minimum-length bar the base
        # candidates must clear (>100 chars for appeals, >50 for chat).
        min_length = 100 if kind == "appeal_letter" else 50
        if len(normalized) <= min_length:
            logger.info(
                f"Chooser synthesis for task {task.id} returned too-short output; skipping"
            )
            return
        # Skip a synthesized output that is just a verbatim copy of a draft.
        if normalized in set(contents):
            logger.info(
                f"Chooser synthesis for task {task.id} returned a verbatim draft; skipping"
            )
            return

        # Next free index: base candidates occupy 0..N-1 contiguously.
        next_index = await database_sync_to_async(
            ChooserCandidate.objects.filter(task=task).count
        )()
        await database_sync_to_async(ChooserCandidate.objects.create)(
            task=task,
            candidate_index=next_index,
            kind=kind,
            model_name=SYNTHESIZED_MODEL_NAME,
            synthesized=True,
            content=normalized,
            metadata={"source": "synthetic", "synthesized": True},
        )
        task.num_candidates_generated = next_index + 1
        # The synthesized draft is one more than the base slots: raise the
        # expectation with it, or every synthesized task reads as one over.
        task.num_candidates_expected = max(task.num_candidates_expected, next_index + 1)
        await database_sync_to_async(task.save)()
        logger.info(
            f"Added synthesized {kind} candidate to task {task.id} "
            f"from {len(contents)} drafts"
        )
    except Exception as e:
        logger.opt(exception=True).warning(
            f"Failed to add synthesized candidate to task {task.id}: {e}"
        )


async def _synthesize_appeal_candidate(
    context: dict, contents: List[str]
) -> Optional[str]:
    """Synthesize multiple appeal drafts into one, reusing AppealGenerator."""
    from fighthealthinsurance.generate_appeal import AppealGenerator

    try:
        generator = AppealGenerator()
        return await generator.synthesize_appeals(
            appeal_texts=contents,
            denial_text=context.get("denial_text_preview"),
            procedure=context.get("procedure"),
            diagnosis=context.get("diagnosis"),
        )
    except Exception as e:
        logger.opt(exception=True).warning(
            f"Error synthesizing appeal chooser candidate: {e}"
        )
        return None


async def _synthesize_chat_candidate(
    context: dict, contents: List[str]
) -> Optional[str]:
    """Synthesize multiple chat responses into one using the best internal model."""
    best_model = ml_router.best_internal_model()
    if best_model is None:
        logger.warning("No internal model available for chat synthesis")
        return None

    history = context.get("history", []) or []
    user_prompt = context.get("prompt", "") or ""

    convo_lines = []
    for msg in history:
        role = str(msg.get("role", "user")).capitalize()
        convo_lines.append(f"{role}: {msg.get('content', '')}")
    convo_lines.append(f"User: {user_prompt}")
    conversation = "\n".join(convo_lines)

    numbered = "\n\n".join(
        f"--- RESPONSE {i + 1} ---\n{text}" for i, text in enumerate(contents)
    )
    prompt = (
        f"CONVERSATION SO FAR:\n{conversation}\n\n"
        f"Below are {len(contents)} candidate assistant responses to the final "
        "user message. Synthesize them into the single best response, combining "
        "the most accurate, helpful, and clear elements from each. Reply with "
        "only the synthesized response.\n\n"
        f"{numbered}"
    )
    try:
        result = await best_model._infer_no_context(
            system_prompts=[
                "You combine multiple assistant responses into one best response "
                "for a health insurance assistance chatbot. Keep it accurate, "
                "helpful, and concise."
            ],
            prompt=prompt,
            temperature=0.3,
        )
    except Exception as e:
        logger.opt(exception=True).warning(
            f"Error synthesizing chat chooser candidate: {e}"
        )
        return None
    if result and len(result.strip()) > 50:
        return str(result).strip()
    return None


async def prefill_if_needed(min_ready: int = 1):
    """
    Check if there are enough READY tasks and trigger generation if not.
    This is a lightweight check intended to be called on page load.

    Args:
        min_ready: Minimum number of ready tasks required for each type.
    """

    for task_type in ["appeal", "chat"]:
        ready_count = await _count_ready_tasks(task_type)
        if ready_count < min_ready:
            if await _generation_underway(task_type):
                logger.debug(
                    f"Chooser {task_type} tasks below minimum, but one is already "
                    "being generated; not starting another"
                )
                continue
            logger.info(
                f"Chooser {task_type} tasks below minimum ({ready_count} < {min_ready}). "
                f"Triggering generation of 1 task."
            )
            # Fire and forget - don't wait for completion
            await fire_and_forget_in_new_threadpool(_generate_single_task(task_type))


async def _generation_underway(task_type: str) -> bool:
    """Whether a ``task_type`` task is being generated now, in any process.

    The prefill throttle lives in the cache, which is per process
    (LocMemCache), so every web worker could start its own generation while
    the pool was empty, each one paying for a round of external calls. Every
    generation creates its task QUEUED first and settles it READY or DISABLED
    when done, so a recent QUEUED row is a sign every worker, and the refill
    actor's batches, can see. It is a check, not a lock: two workers checking
    in the same instant can both start one, but no longer every worker.
    """
    since = timezone.now() - datetime.timedelta(
        seconds=CHOOSER_GENERATION_CLAIM_SECONDS
    )
    return await ChooserTask.objects.filter(
        task_type=task_type, status="QUEUED", created_at__gte=since
    ).aexists()


def trigger_prefill_async() -> bool:
    """
    Trigger async pre-fill of chooser tasks.
    Safe to call from sync context - fires and forgets in a background thread.

    Throttled to one prefill per process per CHOOSER_PREFILL_THROTTLE_SECONDS:
    every chooser page load and every empty next-task fetch asks, and each
    prefill is a full task generation. Across processes, a prefill skips a
    type that is already being generated (see _generation_underway). Returns
    whether a prefill was started.
    """
    from django.core.cache import cache

    try:
        if not cache.add(
            "chooser_prefill_recently_triggered", 1, CHOOSER_PREFILL_THROTTLE_SECONDS
        ):
            logger.debug("Chooser prefill requested again within the throttle window")
            return False
    except Exception as e:
        # A cache that cannot answer must not stop the prefill.
        logger.debug(f"Chooser prefill throttle unavailable: {e}")

    def run_prefill():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(prefill_if_needed(min_ready=1))
        except Exception as e:
            logger.opt(exception=True).warning(f"Error in prefill task: {e}")
        finally:
            loop.close()

    thread = threading.Thread(target=run_prefill)
    thread.daemon = True
    thread.start()
    return True


# Utility function to manually trigger task generation (for testing/admin purposes)
def trigger_task_generation_sync(task_type: str, count: int = 1):
    """
    Synchronously trigger generation of chooser tasks.
    Useful for testing or admin commands.
    """
    from asgiref.sync import async_to_sync

    async def run_generation():
        await _generate_batch_tasks(task_type, count)

    async_to_sync(run_generation)()

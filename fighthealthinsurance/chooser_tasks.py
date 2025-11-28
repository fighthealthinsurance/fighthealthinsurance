"""
Background task generation for the Chooser (Best-Of Selection) system.

This module provides utilities for:
- Checking and refilling the pool of READY ChooserTasks
- Generating synthetic candidates using existing ML pipelines
"""

import asyncio
from typing import Optional, List
from loguru import logger

from django.conf import settings

from fighthealthinsurance.models import (
    ChooserTask,
    ChooserCandidate,
    Denial,
    OngoingChat,
)
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.utils import fire_and_forget_in_new_threadpool


# Configuration for auto-refill thresholds
CHOOSER_MIN_READY_TASKS = getattr(settings, "CHOOSER_MIN_READY_TASKS", 50)
CHOOSER_GENERATION_BATCH_SIZE = getattr(settings, "CHOOSER_GENERATION_BATCH_SIZE", 10)
CHOOSER_NUM_CANDIDATES = getattr(settings, "CHOOSER_NUM_CANDIDATES", 4)


async def check_and_refill_task_pool():
    """
    Check if the pool of READY tasks is below threshold and trigger generation if needed.

    This is the main entry point for the lazy auto-refill mechanism.
    """
    for task_type in ["appeal", "chat"]:
        ready_count = await _count_ready_tasks(task_type)
        if ready_count < CHOOSER_MIN_READY_TASKS:
            logger.info(
                f"Chooser {task_type} tasks below threshold ({ready_count} < {CHOOSER_MIN_READY_TASKS}). "
                f"Triggering generation of {CHOOSER_GENERATION_BATCH_SIZE} tasks."
            )
            # Fire and forget the generation tasks
            await fire_and_forget_in_new_threadpool(
                _generate_batch_tasks(task_type, CHOOSER_GENERATION_BATCH_SIZE)
            )


async def _count_ready_tasks(task_type: str) -> int:
    """Count the number of READY tasks for a given type."""
    from asgiref.sync import sync_to_async

    return await sync_to_async(
        ChooserTask.objects.filter(task_type=task_type, status="READY").count
    )()


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
    from asgiref.sync import sync_to_async

    # Create the task in QUEUED state
    task = await sync_to_async(ChooserTask.objects.create)(
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

        # Mark task as READY if we generated enough candidates
        if task.num_candidates_generated >= 2:  # Minimum 2 candidates needed
            task.status = "READY"
            await sync_to_async(task.save)()
            logger.info(f"ChooserTask {task.id} is now READY with {task.num_candidates_generated} candidates")
        else:
            task.status = "DISABLED"
            await sync_to_async(task.save)()
            logger.warning(f"ChooserTask {task.id} disabled - insufficient candidates generated")

    except Exception as e:
        logger.opt(exception=True).error(f"Error generating candidates for task {task.id}: {e}")
        task.status = "DISABLED"
        await sync_to_async(task.save)()


async def _generate_appeal_candidates(task: ChooserTask):
    """Generate appeal letter candidates for a task."""
    from asgiref.sync import sync_to_async

    # Get a sample denial for context (or use synthetic context)
    sample_denial = await sync_to_async(
        lambda: Denial.objects.filter(
            denial_text__isnull=False,
            procedure__isnull=False,
            diagnosis__isnull=False,
        ).order_by("?").first()
    )()

    if sample_denial:
        task.denial = sample_denial
        task.context_json = {
            "denial_text_preview": (sample_denial.denial_text or "")[:500],
            "procedure": sample_denial.procedure or "",
            "diagnosis": sample_denial.diagnosis or "",
            "insurance_company": sample_denial.insurance_company or "",
        }
    else:
        # Use synthetic context if no denials available
        task.context_json = {
            "denial_text_preview": "Your claim for the requested medical procedure has been denied as not medically necessary according to our coverage guidelines.",
            "procedure": "Physical Therapy",
            "diagnosis": "Lower back pain",
            "insurance_company": "Example Insurance Co",
        }

    await sync_to_async(task.save)()

    # Generate candidates using different models
    models = ml_router.generate_text_backends()
    if not models:
        logger.warning("No models available for appeal candidate generation")
        return

    prompt = _build_appeal_prompt(task.context_json)

    candidate_index = 0
    for model in models[:CHOOSER_NUM_CANDIDATES]:
        try:
            response = await model._infer_no_context(
                system_prompts=[
                    "You are an expert at writing health insurance appeal letters. "
                    "Write a professional, compelling appeal letter based on the given context."
                ],
                prompt=prompt,
            )
            if response and len(response.strip()) > 100:
                await sync_to_async(ChooserCandidate.objects.create)(
                    task=task,
                    candidate_index=candidate_index,
                    kind="appeal_letter",
                    model_name=getattr(model, "name", str(model)),
                    content=response.strip(),
                    metadata={"source": "synthetic"},
                )
                candidate_index += 1
                task.num_candidates_generated = candidate_index
                await sync_to_async(task.save)()
        except Exception as e:
            logger.warning(f"Error generating appeal candidate with model {model}: {e}")


async def _generate_chat_candidates(task: ChooserTask):
    """Generate chat response candidates for a task."""
    from asgiref.sync import sync_to_async

    # Get a sample chat for context (or use synthetic context)
    sample_chat = await sync_to_async(
        lambda: OngoingChat.objects.filter(
            chat_history__isnull=False,
        ).order_by("?").first()
    )()

    user_prompt = None
    if sample_chat and sample_chat.chat_history:
        for msg in sample_chat.chat_history:
            if msg.get("role") == "user":
                user_prompt = msg.get("content", "")[:500]
                break

    if user_prompt:
        task.chat = sample_chat
        task.context_json = {"prompt": user_prompt}
    else:
        # Use synthetic context if no chats available
        user_prompt = "My insurance denied my claim for an MRI. What should I do?"
        task.context_json = {"prompt": user_prompt}

    await sync_to_async(task.save)()

    # Generate candidates using different models
    models = ml_router.get_chat_backends()
    if not models:
        logger.warning("No models available for chat candidate generation")
        return

    candidate_index = 0
    for model in models[:CHOOSER_NUM_CANDIDATES]:
        try:
            response, _ = await model.generate_chat_response(
                current_message=user_prompt,
                previous_context_summary=None,
                history=[],
                is_professional=True,
                is_logged_in=True,
            )
            if response and len(response.strip()) > 50:
                await sync_to_async(ChooserCandidate.objects.create)(
                    task=task,
                    candidate_index=candidate_index,
                    kind="chat_response",
                    model_name=getattr(model, "name", str(model)),
                    content=response.strip(),
                    metadata={"source": "synthetic"},
                )
                candidate_index += 1
                task.num_candidates_generated = candidate_index
                await sync_to_async(task.save)()
        except Exception as e:
            logger.warning(f"Error generating chat candidate with model {model}: {e}")


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

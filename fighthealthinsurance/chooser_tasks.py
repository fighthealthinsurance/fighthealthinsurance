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
    Uses a distributed lock to ensure only one instance runs at a time.
    """
    from django.core.cache import cache
    from asgiref.sync import sync_to_async

    lock_key = "chooser_task_refill_lock"
    lock_timeout = 5  # 5 seconds - non-blocking, quick return if already running

    # Try to acquire the lock
    lock_acquired = await sync_to_async(cache.add)(lock_key, "locked", lock_timeout)

    if not lock_acquired:
        logger.debug("Another instance is already refilling chooser tasks, skipping")
        return

    try:
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
    finally:
        # Release the lock
        await sync_to_async(cache.delete)(lock_key)


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
            logger.info(
                f"ChooserTask {task.id} is now READY with {task.num_candidates_generated} candidates"
            )
        else:
            task.status = "DISABLED"
            await sync_to_async(task.save)()
            logger.warning(
                f"ChooserTask {task.id} disabled - insufficient candidates generated"
            )

    except Exception as e:
        logger.opt(exception=True).error(
            f"Error generating candidates for task {task.id}: {e}"
        )
        task.status = "DISABLED"
        await sync_to_async(task.save)()


async def _generate_appeal_candidates(task: ChooserTask):
    """Generate appeal letter candidates for a task using ONLY synthetic data from ML."""
    from asgiref.sync import sync_to_async

    # Generate synthetic denial scenario using ML
    task.denial = None  # Explicitly set to None - no real denials

    # Use ML to generate a synthetic denial scenario
    generation_models = ml_router.generate_text_backends()
    if not generation_models:
        logger.warning("No models available for generating synthetic denial scenarios")
        # Fallback to a basic scenario
        task.context_json = {
            "denial_text_preview": "Your claim has been denied.",
            "procedure": "Medical Procedure",
            "diagnosis": "Medical Condition",
            "insurance_company": "Insurance Company",
        }
        await sync_to_async(task.save)()
        return

    # Use first model to generate synthetic denial scenario
    scenario_model = generation_models[0]
    try:
        scenario_prompt = (
            "Generate a realistic but completely fictional health insurance denial scenario. "
            "Provide the following in a structured format:\n"
            "Procedure: [a medical procedure name]\n"
            "Diagnosis: [a medical diagnosis]\n"
            "Insurance Company: [a fictional insurance company name]\n"
            "Denial Reason: [1-2 sentence denial reason]\n\n"
            "Make it varied and realistic. Use common medical procedures and diagnoses."
        )

        scenario_response = await scenario_model._infer_no_context(
            system_prompts=[
                "You are a system that generates realistic but fictional health insurance scenarios for training purposes."
            ],
            prompt=scenario_prompt,
        )

        # Parse the response to extract fields
        context = {}
        for line in scenario_response.split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().lower().replace(" ", "_")
                value = value.strip()
                if key == "procedure":
                    context["procedure"] = value
                elif key == "diagnosis":
                    context["diagnosis"] = value
                elif key == "insurance_company":
                    context["insurance_company"] = value
                elif key == "denial_reason":
                    context["denial_text_preview"] = value

        # Ensure all required fields are present
        if not all(
            k in context
            for k in [
                "procedure",
                "diagnosis",
                "insurance_company",
                "denial_text_preview",
            ]
        ):
            # Fallback if parsing failed
            context = {
                "denial_text_preview": (
                    scenario_response[:200]
                    if scenario_response
                    else "Your claim has been denied."
                ),
                "procedure": "Medical Procedure",
                "diagnosis": "Medical Condition",
                "insurance_company": "Insurance Company",
            }

        task.context_json = context
        await sync_to_async(task.save)()

    except Exception as e:
        logger.warning(f"Error generating synthetic denial scenario: {e}")
        # Fallback scenario
        task.context_json = {
            "denial_text_preview": "Your claim has been denied as not medically necessary.",
            "procedure": "Medical Procedure",
            "diagnosis": "Medical Condition",
            "insurance_company": "Insurance Company",
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
    """Generate chat response candidates for a task using ONLY synthetic data from ML."""
    from asgiref.sync import sync_to_async

    # Generate synthetic chat prompt using ML
    task.chat = None  # Explicitly set to None - no real chats

    # Use ML to generate a synthetic user prompt
    generation_models = ml_router.generate_text_backends()
    if not generation_models:
        logger.warning("No models available for generating synthetic chat prompts")
        # Fallback to a basic prompt
        task.context_json = {
            "prompt": "My insurance denied my claim. What should I do?"
        }
        await sync_to_async(task.save)()
        return

    # Use first model to generate synthetic user prompt
    prompt_model = generation_models[0]
    try:
        prompt_generation = (
            "Generate a realistic question that someone might ask about health insurance denials or appeals. "
            "The question should be 1-2 sentences, sound natural, and be about a specific concern like: "
            "getting help with an appeal, understanding a denial, what to do after rejection, etc. "
            "Just provide the question, nothing else."
        )

        user_prompt = await prompt_model._infer_no_context(
            system_prompts=[
                "You are a system that generates realistic user questions about health insurance for training purposes."
            ],
            prompt=prompt_generation,
        )

        # Clean up the response (remove quotes, extra whitespace)
        user_prompt = user_prompt.strip().strip('"').strip("'").strip()

        # Validate it's not too long or too short
        if len(user_prompt) < 10 or len(user_prompt) > 500:
            user_prompt = "My insurance denied my claim. What should I do?"

        task.context_json = {"prompt": user_prompt}
        await sync_to_async(task.save)()

    except Exception as e:
        logger.warning(f"Error generating synthetic chat prompt: {e}")
        # Fallback prompt
        task.context_json = {
            "prompt": "My insurance denied my claim. What should I do?"
        }
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

"""
Background task generation for the Chooser (Best-Of Selection) system.

This module provides utilities for:
- Checking and refilling the pool of READY ChooserTasks
- Generating synthetic candidates using existing ML pipelines
"""

import asyncio
from typing import List, Optional

from django.conf import settings

from loguru import logger

from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import ChooserCandidate, ChooserTask
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

    # Use ML to generate a synthetic denial scenario
    generation_models = ml_router.generate_text_backends()
    if not generation_models:
        logger.warning("No models available for generating synthetic denial scenarios")
        # Cannot proceed without models - mark task as disabled
        task.status = "DISABLED"
        await sync_to_async(task.save)()
        return

    # Use first model to generate synthetic denial scenario
    scenario_model = generation_models[0]
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
            # If parsing failed, try a simpler extraction or log warning
            logger.warning(
                f"Could not parse all fields from scenario response: {scenario_response[:200]}"
            )
            # Try to use whatever we got, filling in missing fields
            context.setdefault("procedure", "Medical procedure")
            context.setdefault("diagnosis", "Medical condition")
            context.setdefault("insurance_company", "Insurance Company")
            context.setdefault(
                "denial_text_preview",
                scenario_response[:200] if scenario_response else "Claim denied.",
            )

        task.context_json = context
        await sync_to_async(task.save)()

    except Exception as e:
        logger.opt(exception=True).warning(
            f"Error generating synthetic denial scenario: {e}"
        )
        # Cannot proceed without a valid scenario - mark task as disabled
        task.status = "DISABLED"
        await sync_to_async(task.save)()
        return

    # Generate candidates using different models
    models = ml_router.generate_text_backends()
    if not models:
        logger.warning("No models available for appeal candidate generation")
        return

    prompt = _build_appeal_prompt(task.context_json)

    candidate_index = 0
    # First pass: try each model once
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
                    await sync_to_async(ChooserCandidate.objects.create)(
                        task=task,
                        candidate_index=candidate_index,
                        kind="appeal_letter",
                        model_name=getattr(model, "name", str(model)),
                        content=response.strip(),
                        metadata={"source": "synthetic", "retry": True},
                    )
                    candidate_index += 1
                    task.num_candidates_generated = candidate_index
                    await sync_to_async(task.save)()
            except Exception as e:
                logger.warning(
                    f"Error generating appeal candidate (retry) with model {model}: {e}"
                )
            retry_count += 1


async def _generate_chat_candidates(task: ChooserTask):
    """Generate chat response candidates for a task using ONLY synthetic data from ML."""
    from asgiref.sync import sync_to_async

    # Generate synthetic chat prompt using ML
    # Use ML to generate a synthetic conversation with some back-and-forth
    generation_models = ml_router.generate_text_backends()
    if not generation_models:
        logger.warning("No models available for generating synthetic chat prompts")
        # Cannot proceed without models - mark task as disabled
        task.status = "DISABLED"
        await sync_to_async(task.save)()
        return

    # Use first model to generate synthetic conversation
    prompt_model = generation_models[0]
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

        # Parse the conversation into history and final prompt
        history = []
        final_user_prompt = None

        lines = conversation_response.strip().split("\n")
        current_role = None
        current_content = []

        for line in lines:
            line = line.strip()
            if line.upper().startswith("USER:"):
                # Save previous message if exists
                if current_role and current_content:
                    content = " ".join(current_content).strip()
                    if current_role == "user" and content:
                        # This might be the final user message or part of history
                        if history and history[-1].get("role") == "assistant":
                            final_user_prompt = content
                        else:
                            history.append({"role": "user", "content": content})
                    elif current_role == "assistant" and content:
                        history.append({"role": "assistant", "content": content})
                        final_user_prompt = (
                            None  # Reset since we got an assistant response
                        )

                current_role = "user"
                current_content = [line[5:].strip()]  # Remove "USER:" prefix
            elif line.upper().startswith("ASSISTANT:"):
                # Save previous user message
                if current_role == "user" and current_content:
                    content = " ".join(current_content).strip()
                    if content:
                        history.append({"role": "user", "content": content})

                current_role = "assistant"
                current_content = [line[10:].strip()]  # Remove "ASSISTANT:" prefix
            elif current_role and line:
                current_content.append(line)

        # Handle the last message
        if current_role and current_content:
            content = " ".join(current_content).strip()
            if current_role == "user" and content:
                final_user_prompt = content
            elif current_role == "assistant" and content:
                history.append({"role": "assistant", "content": content})

        # Validate we have a usable conversation
        if not final_user_prompt or len(final_user_prompt) < 10:
            # Fallback: try generating a simple single question
            logger.warning(
                "Could not parse conversation, falling back to single question"
            )
            simple_prompt = (
                "Generate a realistic 1-2 sentence question someone might ask about one of:\n"
                "- Appealing a health insurance denial\n"
                "- Prior authorization problems\n"
                "- Medicare or Medicaid eligibility\n"
                "Just the question, nothing else."
            )
            final_user_prompt = None
            while not final_user_prompt:
                final_user_prompt = await prompt_model._infer_no_context(
                    system_prompts=[
                        "Generate a natural user question about health insurance."
                    ],
                    prompt=simple_prompt,
                )
            if final_user_prompt:
                final_user_prompt = (
                    final_user_prompt.strip().strip('"').strip("'").strip()
                )
            history = []

        if len(final_user_prompt) < 10 or len(final_user_prompt) > 1000:
            task.status = "DISABLED"
            await sync_to_async(task.save)()
            return

        task.context_json = {
            "prompt": final_user_prompt,
            "history": history,
        }
        await sync_to_async(task.save)()

    except Exception as e:
        logger.opt(exception=True).warning(
            f"Error generating synthetic chat prompt: {e}"
        )
        # Cannot proceed without a valid prompt - mark task as disabled
        task.status = "DISABLED"
        await sync_to_async(task.save)()
        return

    # Generate candidates using different models
    models = ml_router.get_chat_backends()
    if not models:
        logger.warning("No models available for chat candidate generation")
        return

    # Extract history for the chat models
    chat_history = task.context_json.get("history", [])
    user_prompt = task.context_json.get("prompt", "")

    candidate_index = 0
    for model in models[:CHOOSER_NUM_CANDIDATES]:
        try:
            response, _ = await model.generate_chat_response(
                current_message=user_prompt,
                previous_context_summary=None,
                history=chat_history,
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
                    metadata={
                        "source": "synthetic",
                        "has_history": len(chat_history) > 0,
                    },
                )
                candidate_index += 1
                task.num_candidates_generated = candidate_index
                await sync_to_async(task.save)()
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
                    current_message=user_prompt,
                    previous_context_summary=None,
                    history=chat_history,
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
                        metadata={
                            "source": "synthetic",
                            "has_history": len(chat_history) > 0,
                            "retry": True,
                        },
                    )
                    candidate_index += 1
                    task.num_candidates_generated = candidate_index
                    await sync_to_async(task.save)()
            except Exception as e:
                logger.warning(
                    f"Error generating chat candidate (retry) with model {model}: {e}"
                )
            retry_count += 1


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


async def prefill_if_needed(min_ready: int = 1):
    """
    Check if there are enough READY tasks and trigger generation if not.
    This is a lightweight check intended to be called on page load.

    Args:
        min_ready: Minimum number of ready tasks required for each type.
    """
    from asgiref.sync import sync_to_async

    for task_type in ["appeal", "chat"]:
        ready_count = await _count_ready_tasks(task_type)
        if ready_count < min_ready:
            logger.info(
                f"Chooser {task_type} tasks below minimum ({ready_count} < {min_ready}). "
                f"Triggering generation of 1 task."
            )
            # Fire and forget - don't wait for completion
            await fire_and_forget_in_new_threadpool(_generate_single_task(task_type))


def trigger_prefill_async():
    """
    Trigger async pre-fill of chooser tasks.
    Safe to call from sync context - fires and forgets in a background thread.
    """
    import threading

    min_ready = 1

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

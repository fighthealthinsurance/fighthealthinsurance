"""
Shared ML inference utilities.

Provides model-fallback inference used across document helpers and chat processing.
"""

import asyncio
from collections.abc import Callable
from typing import Optional

from loguru import logger

from fighthealthinsurance.ml import llm_usage
from fighthealthinsurance.ml.ml_router import ml_router


async def infer_with_fallback(
    system_prompts: list[str],
    prompt: str,
    temperature: float = 0.3,
    timeout: float = 30.0,
    model_count: int = 3,
    min_length: int = 0,
    label: str = "",
    validator: Optional[Callable[[str], bool]] = None,
    models: Optional[list] = None,
    task: Optional[str] = None,
) -> Optional[str]:
    """
    Try inference across multiple models with timeout.

    Returns the first successful result or None if all models fail.
    If validator is provided, the result must also pass validation;
    otherwise the next model is tried.

    By default the cheapest internal models are used; pass ``models`` to run
    against a specific list (e.g. external models) instead. ``task`` is what
    the LLM usage metrics count the calls as (ml/llm_usage.py TASKS).
    """
    if models is None:
        # General-purpose only: every caller of this helper is asking a model
        # to follow instructions (extract fields, classify, summarize), which
        # the appeal-text fine-tunes answer with stray digits and blank lines.
        models = ml_router.general_purpose_internal_models()[:model_count]
    for model in models:
        try:
            with llm_usage.llm_task(task):
                result = await asyncio.wait_for(
                    model._infer_no_context(
                        system_prompts=system_prompts,
                        prompt=prompt,
                        temperature=temperature,
                    ),
                    timeout=timeout,
                )
            text = str(result).strip() if result else ""
            if text and len(text) > min_length:
                if validator is None or validator(text):
                    return text
                logger.debug(f"Rejected {label} output from {model}; trying next model")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout on {label} with {model}")
        except Exception as e:
            logger.debug(f"Error on {label} with {model}: {e}")
    return None

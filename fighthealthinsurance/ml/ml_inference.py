"""
Shared ML inference utilities.

Provides model-fallback inference used across document helpers and chat processing.
"""

import asyncio
from typing import Optional

from loguru import logger

from fighthealthinsurance.ml.ml_router import ml_router


async def infer_with_fallback(
    system_prompts: list[str],
    prompt: str,
    temperature: float = 0.3,
    timeout: float = 30.0,
    model_count: int = 3,
    min_length: int = 0,
    label: str = "",
) -> Optional[str]:
    """
    Try inference across multiple internal models with timeout.

    Returns the first successful result or None if all models fail.
    """
    models = ml_router.internal_models_by_cost[:model_count]
    for model in models:
        try:
            result = await asyncio.wait_for(
                model._infer_no_context(
                    system_prompts=system_prompts,
                    prompt=prompt,
                    temperature=temperature,
                ),
                timeout=timeout,
            )
            if result and len(str(result).strip()) > min_length:
                return str(result).strip()
        except asyncio.TimeoutError:
            logger.warning(f"Timeout on {label} with {model}")
        except Exception as e:
            logger.debug(f"Error on {label} with {model}: {e}")
    return None

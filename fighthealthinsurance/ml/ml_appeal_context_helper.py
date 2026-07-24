"""Helpers for keeping the appeal-generation prompt within a model's context
window while preferring to retain full context.

Today this is a single last-resort step: when a denial letter is so long that
including it verbatim risks overflowing the model's context window (which
otherwise fails silently -> 0 appeals), summarize it once, cache the summary
on the denial, and use the summary as a prompt substitute. Normal-sized
denials are never summarized -- the full ``denial_text`` is always preferred.
"""

from typing import Optional

from loguru import logger

from fighthealthinsurance.context_utils import estimate_tokens
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import Denial


class MLAppealContextHelper:
    """ML-powered condensing of oversized appeal-generation context."""

    # Prefer full context: only summarize denial_text once it is a large
    # fraction of a small (32k) model's window on its own. ~6000 tokens is a
    # very long denial letter (~24k chars / ~20 pages); below this the raw
    # text is always used.
    DENIAL_TEXT_SUMMARY_THRESHOLD_TOKENS = 6000
    # Cap on how much denial text is fed to the summarizer so the summary call
    # itself can't overflow the summarizer's window. ~60k chars (~15k tokens)
    # fits comfortably inside a 32k-token model with room for the summary.
    DENIAL_TEXT_SUMMARY_INPUT_MAX_CHARS = 60000

    @classmethod
    async def maybe_summarize_denial_text(cls, denial: Denial) -> Optional[str]:
        """Return a condensed denial_text to substitute into the prompt, or
        ``None`` to use the full text.

        Returns ``None`` (keep full context) when the denial text is missing
        or below ``DENIAL_TEXT_SUMMARY_THRESHOLD_TOKENS``. Above the threshold
        it returns the cached ``denial.denial_text_summary`` if present,
        otherwise generates one, persists it, and returns it. On any failure
        it returns ``None`` so generation proceeds with the full text (the
        reactive shed ladder in make_appeals remains the backstop).

        Privacy: summarization routes through ``ml_router.summarize`` with
        ``use_external=denial.use_external`` so an opt-out denial never sends
        its (PHI-bearing) letter to an external provider.
        """
        denial_text = denial.denial_text
        if not denial_text:
            return None
        if estimate_tokens(denial_text) < cls.DENIAL_TEXT_SUMMARY_THRESHOLD_TOKENS:
            # Prefer full context for normal-sized denials.
            return None

        denial_id = denial.denial_id
        if denial.denial_text_summary:
            logger.debug(
                f"Denial {denial_id} reusing cached denial_text_summary "
                f"({len(denial.denial_text_summary)} chars)"
            )
            return denial.denial_text_summary

        try:
            summary: Optional[str] = await ml_router.summarize(
                title=(
                    "health insurance denial letter (preserve the denied "
                    "service/procedure, the payer's stated denial reason(s), "
                    "and any codes, dates, and claim/plan identifiers)"
                ),
                text=denial_text,
                use_external=denial.use_external,
                max_input_chars=cls.DENIAL_TEXT_SUMMARY_INPUT_MAX_CHARS,
            )
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to summarize long denial_text for denial " f"{denial_id}: {e}"
            )
            return None

        if not summary:
            logger.warning(
                f"denial_text summarization returned nothing for denial "
                f"{denial_id}; falling back to full text"
            )
            return None

        try:
            await Denial.objects.filter(denial_id=denial_id).aupdate(
                denial_text_summary=summary
            )
        except Exception as e:
            # Caching is best-effort; still use the summary we just computed.
            logger.opt(exception=True).warning(
                f"Failed to cache denial_text_summary for denial " f"{denial_id}: {e}"
            )
        logger.info(
            f"Summarized long denial_text for denial {denial_id} "
            f"({len(denial_text)} -> {len(summary)} chars) to fit context window"
        )
        return summary

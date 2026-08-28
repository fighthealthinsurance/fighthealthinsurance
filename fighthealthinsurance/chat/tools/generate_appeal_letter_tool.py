"""Generate-appeal-letter tool handler for the chat interface.

Handles ``**generate_appeal_letter {...}**`` calls from the LLM. Instead of
the chat model writing a full appeal letter inline (the longest and most
failure-prone chat generation -- see the "Please go ahead and draft a
letter." total-failure incidents), the model emits this small call with the
denial details it knows and the dedicated appeal-generation pipeline
(appeal-tuned models, curated specialized templates, the shed ladder) writes
the letter. The tool creates/updates the chat-linked Appeal + Denial from
the same payload, saves the drafted letter to the appeal, and replaces the
call with the letter text plus a link.
"""

import json
import re
from typing import Any, Awaitable, Callable, Optional, Tuple

from loguru import logger

from fighthealthinsurance.chat.appeal_letter_generator import (
    denial_has_letter_context,
    draft_letter_for_chat,
)

from .appeal_tool import AppealTool
from .patterns import GENERATE_APPEAL_LETTER_REGEX

# Payload keys that are letter-generation context rather than Appeal/Denial
# columns; folded into denial.qa_context (which the appeal prompt bakes in)
# instead of going through the field allowlist, which would warn on them.
_CONTEXT_ONLY_KEYS = ("medical_reason", "additional_context")


class GenerateAppealLetterTool(AppealTool):
    """
    Tool handler that drafts the appeal letter via the appeal pipeline.

    Subclasses AppealTool for its get-or-create / field-update plumbing;
    only the detection pattern and the execute flow differ:
    1. Extract the JSON parameters (denial/appeal fields + optional
       medical_reason/additional_context).
    2. Create or update the Appeal + Denial linked to the chat.
    3. Draft the letter through the appeal-generation pipeline
       (draft_letter_for_chat), which also persists it to the appeal.
    4. Replace the tool call with the drafted letter and an appeal link.
    """

    pattern = GENERATE_APPEAL_LETTER_REGEX
    detect_flags: int = re.DOTALL | re.MULTILINE | re.IGNORECASE
    name = "Generate Appeal Letter"

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        send_error_message: Optional[Callable[[str], Awaitable[None]]] = None,
        domain: str = "",
        use_external: bool = True,
    ):
        """
        Args:
            send_status_message: Async function to send status updates
            send_error_message: Async function to send error messages
            domain: The domain URL for generating appeal links
            use_external: This chat session's external-model consent,
                forwarded to the letter pipeline's backup call list
        """
        super().__init__(send_status_message, send_error_message, domain)
        self.use_external = use_external

    def _appeal_link(self, appeal: Any) -> str:
        return f"[Appeal #{appeal.id}]({self.domain}/appeals/{appeal.id})"

    async def execute(
        self,
        match: re.Match[str],
        response_text: str,
        context: str,
        chat: Any = None,
        **kwargs,
    ) -> Tuple[str, str]:
        if not chat:
            logger.warning("GenerateAppealLetterTool called without chat object")
            await self.send_error_message("Cannot draft a letter: no chat context")
            return response_text, context

        json_data = match.group(1).strip()
        try:
            appeal_data = json.loads(json_data)
        except json.JSONDecodeError as e:
            logger.warning(
                f"Invalid JSON data {e} in generate_appeal_letter token: {json_data}"
            )
            await self.send_error_message(
                f"Error processing appeal data: Invalid JSON format {e} -- {json_data}"
            )
            raise

        try:
            await self.send_status_message("Setting up your appeal...")
            # Peel off the letter-context keys BEFORE the field update so the
            # allowlist doesn't warn on them; they reach the models through
            # denial.qa_context below.
            extra_context = []
            for key in _CONTEXT_ONLY_KEYS:
                value = appeal_data.pop(key, None)
                if value:
                    extra_context.append(str(value).strip())

            appeal, denial = await self._get_or_create_appeal(chat, appeal_data)
            if not appeal or not denial:
                await self.send_status_message("Failed to create or update appeal.")
                return (
                    response_text.replace(
                        match.group(0),
                        "I couldn't set up the appeal record to draft a letter into.",
                    ),
                    context,
                )

            await self._update_appeal_fields(appeal, denial, appeal_data)
            if extra_context:
                addition = "Context from the chat: " + " ".join(extra_context)
                denial.qa_context = (
                    f"{denial.qa_context}\n{addition}"
                    if denial.qa_context
                    else addition
                )
            await appeal.asave()
            await denial.asave()

            if not denial_has_letter_context(denial):
                # Nothing to write a letter ABOUT yet; asking beats generating
                # a letter of blanks.
                return (
                    response_text.replace(
                        match.group(0),
                        "Before I draft the letter I need at least one of: "
                        "the procedure or service that was denied, the "
                        "diagnosis, or the denial letter text (you can paste "
                        "it here). Which of those can you share?",
                    ),
                    context,
                )

            await self.send_status_message(
                "Drafting your appeal letter with our appeal-generation "
                "pipeline -- this can take a minute..."
            )
            letter = await draft_letter_for_chat(
                appeal=appeal,
                denial=denial,
                use_external=self.use_external,
            )

            if letter:
                await self.send_status_message(
                    f"Appeal letter drafted and saved to Appeal #{appeal.id}."
                )
                replacement = (
                    f"I've drafted an appeal letter and saved it to "
                    f"{self._appeal_link(appeal)}. Here's the draft -- tell me "
                    f"what you'd like to change:\n\n---\n\n{letter}"
                )
            else:
                await self.send_status_message("Letter generation did not succeed.")
                replacement = (
                    f"I wasn't able to draft the letter just now -- the "
                    f"letter-writing models didn't return a usable draft. "
                    f"Your appeal details are saved to "
                    f"{self._appeal_link(appeal)}, where you can also generate "
                    f"the letter, or ask me to try again in a few minutes."
                )
            return response_text.replace(match.group(0), replacement), context

        except Exception as e:
            logger.opt(exception=True).warning(f"Error drafting appeal letter: {e}")
            await self.send_error_message(f"Error drafting appeal letter: {str(e)}")
            raise

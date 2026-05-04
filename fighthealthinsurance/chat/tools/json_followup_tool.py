"""
Reusable base class for chat tools that follow this shape:

    1. Detect a ``**tool_name {JSON}**`` invocation in the LLM response.
    2. Parse the JSON payload.
    3. Run a synchronous lookup via ``sync_to_async``.
    4. Re-invoke the LLM with the lookup result so the next reply can quote it.
    5. Merge the additional response and context back in.

The flow was implemented near-byte-identically by ``USPSTFLookupTool``,
``PaRequirementLookupTool``, and (in spirit) the medicaid tools. This base
class consolidates the boilerplate so each subclass is reduced to the
tool-specific bits: pattern, name, the lookup function, and (optionally) a
follow-up-prompt builder.
"""

from abc import abstractmethod
from typing import Any, Awaitable, Callable, List, Optional, Tuple

import json
import re

from loguru import logger

from .base_tool import BaseTool


class JsonFollowupTool(BaseTool):
    """Base class for JSON-payload tools that re-invoke the LLM with a result.

    Subclasses must override:
      * ``pattern`` — the regex matching this tool's invocation token
      * ``name`` — short human-readable name (used in status/log messages)
      * ``run(params, current_message_for_llm)`` — execute the lookup and
        return ``(note_for_llm, status_message)``. Either may be empty.

    Subclasses may override:
      * ``build_followup_prompt`` — customize the prompt sent back to the LLM
      * ``append_note_to_context`` — when True, the raw note is also appended
        to the running context so downstream tools can reuse it.
    """

    detect_flags: int = re.DOTALL | re.IGNORECASE
    append_note_to_context: bool = False

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        call_llm_callback: Optional[
            Callable[..., Awaitable[Tuple[Optional[str], Optional[str]]]]
        ] = None,
    ):
        super().__init__(send_status_message)
        self.call_llm_callback = call_llm_callback

    @abstractmethod
    async def run(
        self, params: dict, *, current_message_for_llm: str = ""
    ) -> Tuple[str, str]:
        """Run the lookup. Return ``(note_for_llm, status_message)``.

        ``note_for_llm`` is empty when the lookup found nothing useful — in
        that case the tool short-circuits and skips the LLM follow-up.
        """

    def build_followup_prompt(self, note: str, current_message_for_llm: str) -> str:
        """Override to customize the prompt sent to the LLM after lookup."""
        return f"{note}\n\n -- use this when answering: {current_message_for_llm}"

    async def execute(
        self,
        match: re.Match[str],
        response_text: str,
        context: str,
        model_backends: Any = None,
        current_message_for_llm: str = "",
        history_for_llm: Optional[List[dict]] = None,
        depth: int = 0,
        is_logged_in: bool = False,
        is_professional: bool = False,
        **kwargs,
    ) -> Tuple[str, str]:
        all_matches = self.detect_all(response_text)
        cleaned_response = self.clean_all_matches(response_text, all_matches)

        if len(all_matches) > 1:
            logger.warning(
                f"Found {len(all_matches)} {self.name} tool calls; "
                "processing only the first one."
            )

        json_data = match.group(1).strip()
        try:
            params = json.loads(json_data)
        except json.JSONDecodeError as e:
            logger.warning(
                f"Invalid JSON in {self.name} tool call (length={len(json_data)}): {e}"
            )
            await self.send_status_message(
                f"Could not parse {self.name} parameters: invalid JSON."
            )
            return (
                f"I couldn't parse the {self.name} request. Please try again with a "
                "valid JSON payload.",
                context,
            )

        if not isinstance(params, dict):
            await self.send_status_message(
                f"Error processing {self.name}: expected a JSON object."
            )
            return cleaned_response, context

        try:
            note, status = await self.run(
                params, current_message_for_llm=current_message_for_llm
            )
        except Exception as e:
            logger.opt(exception=True).warning(f"{self.name} lookup failed: {e}")
            await self.send_status_message(
                f"{self.name} lookup failed; continuing without it."
            )
            return cleaned_response, context

        if status:
            await self.send_status_message(status)

        if not note:
            return cleaned_response, context

        if self.call_llm_callback and model_backends:
            if history_for_llm is not None:
                history_for_llm.append(
                    {"role": "user", "content": current_message_for_llm}
                )
                history_for_llm.append({"role": "agent", "content": response_text})

            (
                additional_response,
                additional_context,
            ) = await self.call_llm_callback(
                model_backends,
                self.build_followup_prompt(note, current_message_for_llm),
                "",
                history_for_llm,
                depth=depth + 1,
                is_logged_in=is_logged_in,
                is_professional=is_professional,
            )

            if cleaned_response and additional_response:
                cleaned_response = f"{cleaned_response}\n\n{additional_response}"
            elif additional_response:
                cleaned_response = additional_response

            if context and additional_context:
                context = f"{context}\n\n{additional_context}"
            elif additional_context:
                context = additional_context

        if self.append_note_to_context:
            context = f"{context}\n\n{note}" if context else note

        return cleaned_response, context

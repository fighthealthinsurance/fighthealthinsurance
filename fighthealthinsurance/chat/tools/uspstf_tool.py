"""
USPSTF preventive-services lookup tool for the chat interface.

When the LLM emits a ``uspstf_lookup`` tool call, this handler looks up
recommendations from the cached USPSTF Prevention TaskForce data and feeds
them back into the conversation. USPSTF A/B graded services generally must
be covered without cost-sharing under the ACA, so the tool is most useful
for preventive-care denial appeals.
"""

import json
import re
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from asgiref.sync import sync_to_async
from loguru import logger

from .base_tool import BaseTool
from .patterns import USPSTF_LOOKUP_REGEX


class USPSTFLookupTool(BaseTool):
    """
    Tool handler for USPSTF preventive-service lookups.

    Expected tool-call format:
        **uspstf_lookup {"query": "colorectal cancer", "grade": "A", "limit": 3}**

    All JSON keys are optional. The tool returns matching recommendations
    formatted for the LLM to incorporate into appeal language.
    """

    pattern = USPSTF_LOOKUP_REGEX
    name = "USPSTF Lookup"

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        call_llm_callback: Optional[
            Callable[..., Awaitable[Tuple[Optional[str], Optional[str]]]]
        ] = None,
    ):
        super().__init__(send_status_message)
        self.call_llm_callback = call_llm_callback

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
                f"Found {len(all_matches)} USPSTF tool calls, processing only the first"
            )

        json_data = match.group(1).strip()
        logger.debug(f"USPSTF tool call payload: {json_data}")

        try:
            params = json.loads(json_data)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON in uspstf_lookup token: {json_data} - {e}")
            await self.send_status_message(
                "Error processing USPSTF lookup: invalid JSON format."
            )
            return (
                "I couldn't parse the USPSTF lookup request. Please try again "
                'with a JSON payload like {"query": "colon cancer", "grade": "A"}.',
                context,
            )

        if not isinstance(params, dict):
            await self.send_status_message(
                "Error processing USPSTF lookup: expected an object."
            )
            return cleaned_response, context

        await self.send_status_message(
            "Looking up USPSTF preventive recommendations..."
        )

        try:
            # Import locally to avoid circular imports during app startup.
            from fighthealthinsurance.uspstf_api import get_uspstf_info

            info = await sync_to_async(get_uspstf_info)(params)
        except Exception as e:
            logger.opt(exception=True).warning(f"USPSTF lookup failed: {e}")
            await self.send_status_message(
                "USPSTF lookup failed; continuing without it."
            )
            return cleaned_response, context

        if not info:
            return cleaned_response, context

        descriptor = (
            params.get("query")
            or params.get("topic")
            or params.get("grade")
            or "USPSTF"
        )
        await self.send_status_message(f"USPSTF results for {descriptor} ready.")

        prompt_for_llm = (
            "USPSTF preventive-service recommendations relevant to the user's "
            f"question (descriptor: {descriptor}):\n\n{info}\n\n"
            "Use this information to answer their question. When the cited "
            "recommendation is grade A or B, remind the user that under the ACA "
            "non-grandfathered private plans, the marketplace, and Medicaid "
            "expansion populations generally must cover the service without "
            "cost-sharing. Always link to the USPSTF source URL when citing a "
            "specific recommendation."
        )

        if self.call_llm_callback and model_backends:
            if history_for_llm is not None:
                history_for_llm.append(
                    {"role": "user", "content": current_message_for_llm}
                )
                history_for_llm.append({"role": "agent", "content": response_text})

            additional_response, additional_context = await self.call_llm_callback(
                model_backends,
                prompt_for_llm,
                "USPSTF preventive-services lookup",
                history_for_llm,
                depth=depth + 1,
                is_logged_in=is_logged_in,
                is_professional=is_professional,
            )

            if cleaned_response and additional_response:
                cleaned_response += additional_response
            elif additional_response:
                cleaned_response = additional_response

            if context and additional_context:
                context = context + additional_context
            elif additional_context:
                context = additional_context

        # Always append the raw lookup to the running context so downstream
        # tools (e.g. appeal generation) can reuse it.
        context = (context + "\n\n" + info) if context else info
        return cleaned_response, context

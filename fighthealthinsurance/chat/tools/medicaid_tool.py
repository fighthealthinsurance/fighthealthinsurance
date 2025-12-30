"""
Medicaid tool handlers for the chat interface.

Handles medicaid_info and medicaid_eligibility tool calls from the LLM
to look up Medicaid information and check eligibility.
"""

import json
import re
from typing import Optional, Tuple, Callable, Awaitable, Any, List
from loguru import logger

from .base_tool import BaseTool
from .patterns import MEDICAID_INFO_REGEX, MEDICAID_ELIGIBILITY_REGEX


class MedicaidInfoTool(BaseTool):
    """
    Tool handler for Medicaid information lookups.

    When the LLM includes a medicaid_info call in its response, this tool:
    1. Extracts the JSON parameters (state, topic, limit)
    2. Calls the Medicaid API
    3. Returns context for the LLM to incorporate
    """

    pattern = MEDICAID_INFO_REGEX
    name = "Medicaid Info"

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        call_llm_callback: Optional[Callable[..., Awaitable[Tuple[str, str]]]] = None,
    ):
        """
        Initialize the Medicaid info tool.

        Args:
            send_status_message: Async function to send status updates
            call_llm_callback: Callback to call LLM with additional context
        """
        super().__init__(send_status_message)
        self.call_llm_callback = call_llm_callback

    def detect_all(self, text: str) -> List[re.Match[str]]:
        """
        Find all medicaid_info tool calls in the text.

        Args:
            text: The LLM response text to check

        Returns:
            List of all match objects
        """
        return list(re.finditer(self.pattern, text, flags=re.DOTALL | re.IGNORECASE))

    def clean_all_matches(self, text: str, matches: List[re.Match[str]]) -> str:
        """
        Remove all tool calls from the response text.

        Args:
            text: Original response text
            matches: List of regex match objects for tool calls

        Returns:
            Response text with all tool calls removed
        """
        cleaned = text
        for match in matches:
            cleaned = cleaned.replace(match.group(0), "")
        return cleaned.strip()

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
        """
        Execute Medicaid info lookup and incorporate results.

        Args:
            match: Regex match containing JSON parameters
            response_text: Current LLM response
            context: Current context string
            model_backends: LLM backends for follow-up calls
            current_message_for_llm: The user's current message
            history_for_llm: Chat history (will be modified)
            depth: Current recursion depth
            is_logged_in: Whether user is logged in
            is_professional: Whether user is a professional

        Returns:
            Tuple of (updated_response, updated_context)
        """
        # Find all matches and clean all of them
        all_matches = self.detect_all(response_text)
        cleaned_response = self.clean_all_matches(response_text, all_matches)

        if len(all_matches) > 1:
            logger.warning(
                f"Found {len(all_matches)} Medicaid tool calls, processing only the first one"
            )

        logger.debug(f"Medicaid tool call detected: {match.group(0)}")

        json_data = match.group(1).strip()
        logger.debug(f"Extracted JSON data: {json_data}")

        try:
            medicaid_info_data = json.loads(json_data)
            logger.debug(f"Parsed JSON data: {medicaid_info_data}")

            await self.send_status_message("Processing Medicaid info lookup data...")

            # Import here to avoid circular imports
            from fighthealthinsurance.medicaid_api import get_medicaid_info

            medicaid_info = get_medicaid_info(medicaid_info_data)
            logger.debug(
                f"Got Medicaid info response: {medicaid_info[:200] if medicaid_info else 'None'}..."
            )

            if medicaid_info:
                await self.send_status_message(
                    "Medicaid info lookup completed successfully."
                )

                state_name = medicaid_info_data.get("state", "the state")
                medicaid_info_text = (
                    f"Here's the official Medicaid information for {state_name}:\n\n"
                    f"{medicaid_info}\n\n"
                    f" -- use it to answer the question {current_message_for_llm}"
                )

                # Call LLM with the Medicaid info context
                if self.call_llm_callback and model_backends:
                    # Update history with current exchange
                    if history_for_llm is not None:
                        history_for_llm.append(
                            {"role": "user", "content": current_message_for_llm}
                        )
                        history_for_llm.append(
                            {"role": "agent", "content": response_text}
                        )

                    additional_response, additional_context = (
                        await self.call_llm_callback(
                            model_backends,
                            medicaid_info_text,
                            "",  # Empty previous context summary
                            history_for_llm,
                            depth=depth + 1,
                            is_logged_in=is_logged_in,
                            is_professional=is_professional,
                        )
                    )

                    logger.debug(
                        f"Medicaid with intro/conclusion: {medicaid_info[:200]}..."
                    )

                    if cleaned_response and additional_response:
                        cleaned_response += additional_response
                    elif additional_response:
                        cleaned_response = additional_response

                    if context and additional_context:
                        context = context + additional_context
                    elif additional_context:
                        context = additional_context

                return cleaned_response, context

            else:
                await self.send_status_message(
                    "No Medicaid info found for the provided data."
                )
                return (
                    "I couldn't find Medicaid information for the requested state. "
                    "Please check the state name and try again.",
                    context,
                )

        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON data in medicaid_info token: {json_data}")
            await self.send_status_message(
                "Error processing Medicaid info data: Invalid JSON format."
            )
            raise

        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error processing Medicaid info data: {e}"
            )
            await self.send_status_message(
                f"Error processing Medicaid info data: {str(e)}"
            )
            raise


class MedicaidEligibilityTool(BaseTool):
    """
    Tool handler for Medicaid eligibility checks.

    When the LLM includes a medicaid_eligibility call in its response, this tool:
    1. Extracts the JSON parameters (income, household size, state, etc.)
    2. Calls the eligibility checker
    3. Returns context for the LLM to incorporate
    """

    pattern = MEDICAID_ELIGIBILITY_REGEX
    name = "Medicaid Eligibility"

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        call_llm_callback: Optional[Callable[..., Awaitable[Tuple[str, str]]]] = None,
    ):
        """
        Initialize the Medicaid eligibility tool.

        Args:
            send_status_message: Async function to send status updates
            call_llm_callback: Callback to call LLM with additional context
        """
        super().__init__(send_status_message)
        self.call_llm_callback = call_llm_callback

    def detect_all(self, text: str) -> List[re.Match[str]]:
        """
        Find all medicaid_eligibility tool calls in the text.

        Args:
            text: The LLM response text to check

        Returns:
            List of all match objects
        """
        return list(re.finditer(self.pattern, text, flags=re.DOTALL | re.IGNORECASE))

    def clean_all_matches(self, text: str, matches: List[re.Match[str]]) -> str:
        """
        Remove all tool calls from the response text.

        Args:
            text: Original response text
            matches: List of regex match objects for tool calls

        Returns:
            Response text with all tool calls removed
        """
        cleaned = text
        for match in matches:
            cleaned = cleaned.replace(match.group(0), "")
        return cleaned.strip()

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
        """
        Execute Medicaid eligibility check and incorporate results.

        Args:
            match: Regex match containing JSON parameters
            response_text: Current LLM response
            context: Current context string
            model_backends: LLM backends for follow-up calls
            current_message_for_llm: The user's current message
            history_for_llm: Chat history (will be modified)
            depth: Current recursion depth
            is_logged_in: Whether user is logged in
            is_professional: Whether user is a professional

        Returns:
            Tuple of (updated_response, updated_context)
        """
        logger.debug("Medicaid eligibility check.")

        # Find all matches and clean all of them
        all_matches = self.detect_all(response_text)
        cleaned_response = self.clean_all_matches(response_text, all_matches)

        if len(all_matches) > 1:
            logger.warning(
                f"Found {len(all_matches)} Medicaid eligibility tool calls, "
                "processing only the first one"
            )

        # Try to parse JSON from matches
        loaded = None
        for ematch in all_matches:
            if loaded is None:
                try:
                    loaded = json.loads(ematch.group(1).strip())
                except Exception:
                    pass

        if len(cleaned_response) > 1:
            await self.send_status_message(
                f"Looking up medicaid eligibility, please wait. "
                f"Remaining information: {cleaned_response}"
            )

        if loaded is None:
            loaded = {}

        if not isinstance(loaded, dict):
            raise TypeError(
                f"Expected dict, got {type(loaded).__name__} while loading tool call params."
            )

        try:
            # Import here to avoid circular imports
            from fighthealthinsurance.medicaid_api import is_eligible

            await self.send_status_message("Processing Medicaid eligibility data")

            (
                eligible_2025,
                eligible_2026,
                medicare,
                alternatives,
                missing,
            ) = is_eligible(**loaded)

            info_text = self._build_eligibility_info(
                eligible_2025, eligible_2026, medicare, alternatives, missing
            )

            action_text = (
                "Use this info to ask the user any follow up questions or deliver "
                "the news of our determiniation and alternatives. Always be careful "
                "to indicate that this is an approximation and they should contact "
                "the state to know for sure (you can use the state tool call to get "
                "more info to provide to the user). Remember to use the panda emoji "
                "and context."
            )

            await self.send_status_message("Formatting response...")

            # Call LLM with eligibility info
            if self.call_llm_callback and model_backends:
                if history_for_llm is not None:
                    history_for_llm.append(
                        {"role": "user", "content": current_message_for_llm}
                    )
                    history_for_llm.append({"role": "agent", "content": response_text})

                additional_response, additional_context = await self.call_llm_callback(
                    model_backends,
                    info_text + action_text,
                    "Medicaid eligibility investigation",
                    history_for_llm,
                    depth=depth + 1,
                    is_logged_in=is_logged_in,
                    is_professional=is_professional,
                )

                if additional_response and len(additional_response) > 1:
                    response_text = additional_response

                if context:
                    if additional_context:
                        context += additional_context
                else:
                    context = additional_context

                return response_text, context

            return cleaned_response, context

        except Exception as e:
            logger.opt(exception=True).debug(
                f"Error parsing params for medicaid eligibility tool: {e}"
            )
            return (
                "Something went wrong trying to figure out eligibility. "
                "Please contact your state for more info.",
                context,
            )

    def _build_eligibility_info(
        self,
        eligible_2025: bool,
        eligible_2026: bool,
        medicare: bool,
        alternatives: List[str],
        missing: List[str],
    ) -> str:
        """
        Build the eligibility information text.

        Args:
            eligible_2025: Whether eligible under 2025 rules
            eligible_2026: Whether eligible under 2026 rules
            medicare: Whether eligible for Medicare
            alternatives: List of alternative suggestions
            missing: List of missing information needed

        Returns:
            Formatted information text
        """
        info_text = (
            "We're helping figure out if someone is likely eligible for Medicaid. "
            "Be clear this is an approximation and they'll need to confirm with "
            "the state to be sure."
        )

        if len(missing) > 0:
            info_text += (
                f"To figure out if they're eligible we have {missing} questions to ask."
            )
        else:
            if eligible_2025:
                info_text += (
                    "Our data so far suggests they could be eligible for medicaid "
                    "under the 2025 rules."
                )
            else:
                info_text += (
                    "Our data so far suggests they may not be eligible for medicaid "
                    "under the 2025 rules."
                )

            if eligible_2026:
                info_text += (
                    "Our data so far suggests they could be eligible for medicaid "
                    "under the 2026 rules."
                )
            else:
                info_text += (
                    "Our data so far suggests they may not be eligible for medicaid "
                    "under the 2026 rules."
                )

            if medicare:
                info_text += "Our data suggests they may be eligible for medicare."

        if len(alternatives) > 0:
            info_text += (
                f"Some possible alternative suggestions to help are {alternatives}."
            )

        return info_text

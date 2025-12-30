"""
Base class for chat tool handlers.

Tool handlers process specific "tool calls" that the LLM includes in responses.
Each tool can detect its pattern in text, execute the tool action, and format results.
"""

from abc import ABC, abstractmethod
from typing import Optional, Tuple, Any, Callable, Awaitable
import re
from loguru import logger


class BaseTool(ABC):
    """
    Abstract base class for chat tool handlers.

    Subclasses implement specific tools like PubMed search, Medicaid lookup, etc.
    """

    # Regex pattern to detect this tool in LLM output
    pattern: str = ""

    # Human-readable name for status messages
    name: str = "Tool"

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
    ):
        """
        Initialize the tool handler.

        Args:
            send_status_message: Async function to send status updates to the user
        """
        self.send_status_message = send_status_message

    def detect(self, text: str) -> Optional[re.Match[str]]:
        """
        Check if this tool's pattern is present in the text.

        Args:
            text: The LLM response text to check

        Returns:
            Match object if pattern found, None otherwise
        """
        if not self.pattern:
            return None
        return re.search(self.pattern, text, flags=re.IGNORECASE)

    def clean_response(self, text: str, match: re.Match[str]) -> str:
        """
        Remove the tool call from the response text.

        Args:
            text: Original response text
            match: The regex match object for the tool call

        Returns:
            Response text with the tool call removed
        """
        return text.replace(match.group(0), "").strip()

    @abstractmethod
    async def execute(
        self, match: re.Match[str], response_text: str, context: str, **kwargs
    ) -> Tuple[str, str]:
        """
        Execute the tool action.

        Args:
            match: The regex match object containing tool parameters
            response_text: The current LLM response text
            context: The current context string
            **kwargs: Additional arguments specific to the tool

        Returns:
            Tuple of (updated_response_text, updated_context)
        """
        pass

    async def handle(
        self, response_text: str, context: str, **kwargs
    ) -> Tuple[str, str, bool]:
        """
        Detect and handle this tool if present in the response.

        Args:
            response_text: The LLM response text
            context: The current context string
            **kwargs: Additional arguments passed to execute()

        Returns:
            Tuple of (updated_response_text, updated_context, was_handled)
        """
        try:
            match = self.detect(response_text)
            if not match:
                return response_text, context, False

            logger.debug(f"{self.name} tool detected in response")
            updated_response, updated_context = await self.execute(
                match, response_text, context, **kwargs
            )
            return updated_response, updated_context, True

        except Exception as e:
            logger.opt(exception=True).warning(f"Error executing {self.name} tool: {e}")
            await self.send_status_message(
                f"Error processing {self.name} request. Continuing with original response."
            )
            return response_text, context, False

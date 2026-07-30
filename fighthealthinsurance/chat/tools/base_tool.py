"""
Base class for chat tool handlers.

Tool handlers process specific "tool calls" that the LLM includes in responses.
Each tool can detect its pattern in text, execute the tool action, and format results.
"""

import re
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, List, Optional, Set, Tuple

from loguru import logger

# Identity/audit fields that LLM-supplied tool payloads must never overwrite,
# even though they are concrete editable columns.
_TOOL_FIELD_DENYLIST = {
    "id",
    "pk",
    "uuid",
    "semi_sekret",
    "follow_up_semi_sekret",
    "session_key",
}


def settable_model_fields(model_cls) -> Set[str]:
    """Field names an LLM tool payload may set on ``model_cls``.

    Concrete, editable, non-primary-key, non-relation fields minus the
    denylist. Relations (and their ``*_id`` attribute forms) are excluded so
    a crafted payload can't re-point an appeal/prior auth at another user's
    denial or chat; identity fields are excluded so it can't overwrite
    lookup keys.
    """
    fields: Set[str] = set()
    for f in model_cls._meta.concrete_fields:
        if f.primary_key or f.is_relation or not getattr(f, "editable", True):
            continue
        if f.name in _TOOL_FIELD_DENYLIST:
            continue
        fields.add(f.name)
    return fields


def is_safe_tool_field(key: str, allowed: Set[str]) -> bool:
    """Whether an LLM payload key may be applied to a model instance."""
    if not key or key.startswith("_") or key.endswith("_id"):
        return False
    return key in allowed


class BaseTool(ABC):
    """
    Abstract base class for chat tool handlers.

    Subclasses implement specific tools like PubMed search, Medicaid lookup, etc.
    """

    # Regex pattern to detect this tool in LLM output
    pattern: str = ""

    # Regex flags used for detection (subclasses can override)
    detect_flags: int = re.IGNORECASE

    # Regex flags used by detect_all() — defaults match the JSON-payload tools
    # (medicaid, doc fetcher, USPSTF, PA requirement) which need DOTALL for
    # multi-line JSON.
    detect_all_flags: int = re.DOTALL | re.IGNORECASE

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
        return re.search(self.pattern, text, flags=self.detect_flags)

    def detect_all(self, text: str) -> List[re.Match[str]]:
        """Find every tool-call match in the text.

        Useful when an LLM emits multiple invocations of the same tool in a
        single response and the handler needs to enumerate them.
        """
        if not self.pattern:
            return []
        return list(re.finditer(self.pattern, text, flags=self.detect_all_flags))

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

    def clean_all_matches(self, text: str, matches: List[re.Match[str]]) -> str:
        """Strip every tool-call match from the response text."""
        cleaned = text
        for match in matches:
            cleaned = cleaned.replace(match.group(0), "")
        return cleaned.strip()

    @staticmethod
    def merge_strings(existing: Optional[str], addition: Optional[str]) -> str:
        """Concatenate two strings with a blank-line separator.

        Returns ``""`` if both are empty/None. Used to glue an LLM follow-up
        response onto the original response (or follow-up context onto the
        original context) so adjacent paragraphs don't run together.

        Only newline characters are stripped at the join — leading spaces /
        tabs are preserved so markdown indentation, nested-list alignment,
        and code-block leading whitespace aren't accidentally clobbered.
        """
        if not existing:
            return (addition or "").lstrip("\n")
        if not addition:
            return existing
        return existing.rstrip("\n") + "\n\n" + addition.lstrip("\n")

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
            try:
                await self.send_status_message(
                    f"Error processing {self.name} request. Continuing with original response."
                )
            except Exception:
                logger.debug(f"{self.name}: could not send tool-error status")
            # Best-effort strip of the raw tool syntax before handing the text
            # back: the user should never see `**create_or_update_appeal**
            # {...}` in the chat because a tool blew up mid-execution.
            cleaned_response = response_text
            try:
                stripped = re.sub(
                    self.pattern, "", response_text, flags=self.detect_all_flags
                ).strip()
                if stripped:
                    cleaned_response = stripped
            except Exception:
                logger.debug(f"{self.name}: could not strip tool syntax on error")
            return cleaned_response, context, True

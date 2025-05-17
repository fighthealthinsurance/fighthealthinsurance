"""Mock models for chat testing."""

from typing import Tuple, Optional, Dict, Any


class MockChatModel:
    """A mock model that returns predefined responses for chat."""

    def __init__(self):
        """Initialize the mock model with a default response."""
        self._next_response = (
            "This is a standard mock response to your question.",
            str({"summary": "Standard mock response context."}),
        )

    def set_next_response(self, response_text: str, context_summary: str):
        """
        Set the next response that the mock model will return.

        Args:
            response_text: The text response to return
            context_summary: The context summary to return
        """
        self._next_response = (response_text, context_summary)

    async def generate_chat_response(
        self,
        message: str,
        previous_context_summary: Optional[Dict[str, Any]] = None,
        history: Optional[list[dict[str, str]]] = None,
    ) -> Tuple[str, str]:
        """
        Generate a mock response to a chat message.

        Args:
            message: The user's message
            previous_context_summary: Optional context from previous interactions
            history: Optional history of messages

        Returns:
            A tuple of (response_text, updated_context)
        """
        # Use the next response that was set or the default
        response, context = self._next_response

        # Reset to default for subsequent calls
        self._next_response = (
            "This is a standard mock response to your question.",
            str(
                {
                    "summary": f"User asked: {message[:50]}... I provided a standard response."
                }
            ),
        )

        return response, context

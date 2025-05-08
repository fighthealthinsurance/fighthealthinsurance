"""Mock models for chat testing."""

from typing import Tuple, Optional, Dict, Any


class MockChatModel:
    """A mock model that returns predefined responses for chat."""

    async def generate_chat_response(
        self,
        message: str,
        previous_context_summary: Optional[Dict[str, Any]] = None,
        history: Optional[list[dict[str, str]]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Generate a mock response to a chat message.

        Args:
            message: The user's message
            previous_context_summary: Optional context from previous interactions
            history: Optional history of messages

        Returns:
            A tuple of (response_text, updated_context)
        """
        # Return a standard response regardless of the input
        response = "This is a standard mock response to your question."

        # Generate a simple context summary
        context = {
            "summary": f"User asked: {message[:50]}... I provided a standard response."
        }

        return response, context

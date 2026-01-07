"""Mock models for chat testing."""

from typing import Tuple, Optional, Dict, Any


class MockChatModel:
    """A mock model that returns predefined responses for chat."""

    def __init__(self, external: bool = False):
        """Initialize the mock model with a default response."""
        self._next_response = (
            "This is a standard mock response to your question.",
            str({"summary": "Standard mock response context."}),
        )
        self._external = external
        self._persistent = False  # If True, don't reset after each call

    @property
    def external(self) -> bool:
        """Return whether this is an external model."""
        return self._external

    def quality(self):
        return 100

    def set_next_response(self, response_text: str, context_summary: str):
        """
        Set the next response that the mock model will return (resets after use).

        Args:
            response_text: The text response to return
            context_summary: The context summary to return
        """
        self._next_response = (response_text, context_summary)
        self._persistent = False

    def set_persistent_response(self, response_text: str, context_summary: str):
        """
        Set a persistent response that won't reset after each call.
        Useful for simulating a model that always fails.

        Args:
            response_text: The text response to return
            context_summary: The context summary to return
        """
        self._next_response = (response_text, context_summary)
        self._persistent = True

    async def generate_chat_response(
        self,
        message: str,
        previous_context_summary: Optional[Dict[str, Any]] = None,
        history: Optional[list[dict[str, str]]] = None,
        is_professional: Optional[bool] = True,
        is_logged_in: Optional[bool] = True,
    ) -> Tuple[str, str]:
        """
        Generate a mock response to a chat message.

        Args:
            message: The user's message
            previous_context_summary: Optional context from previous interactions
            history: Optional history of messages
            is_professional: Optional boolean indicating if the user is a professional
            is_logged_in: Optional boolean indicating if the user is logged in

        Returns:
            A tuple of (response_text, updated_context)
        """
        # Use the next response that was set or the default
        response, context = self._next_response

        # Reset to default for subsequent calls (unless persistent)
        if not self._persistent:
            self._next_response = (
                "This is a standard mock response to your question.",
                str(
                    {
                        "summary": f"User asked: {message[:50]}... I provided a standard response."
                    }
                ),
            )

        return response, context

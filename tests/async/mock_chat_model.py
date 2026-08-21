"""Mock models for chat testing."""

from typing import Tuple, Optional


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
        current_message_for_llm: str,
        previous_context_summary: Optional[str] = None,
        history: Optional[list[dict[str, str]]] = None,
        temperature: float = 0.7,
        is_professional: Optional[bool] = True,
        is_logged_in: Optional[bool] = True,
        is_medicaid_related: Optional[bool] = None,
        allow_repeated_reply: bool = False,
    ) -> Tuple[str, str]:
        """
        Generate a mock response to a chat message.

        The parameter names, order, and types mirror
        ``RemoteModelLike.generate_chat_response`` — callers (like the
        chooser) pass them by keyword, so a mismatched mock signature would
        hide real TypeErrors. ``temperature`` sits before the audience flags
        for the same reason (anti-repeat retries pass a raised value).

        Args:
            current_message_for_llm: The user's message
            previous_context_summary: Optional context from previous interactions
            history: Optional history of messages
            temperature: Sampling temperature (ignored by the mock)
            is_professional: Optional boolean indicating if the user is a professional
            is_logged_in: Optional boolean indicating if the user is logged in
            is_medicaid_related: Optional Medicaid-topic hint (ignored by the
                mock; present so the mock accepts every keyword the real
                signature does)
            allow_repeated_reply: True when the user explicitly asked for a
                repeat (ignored by the mock; accepted because the call
                builders always pass it)

        Returns:
            A tuple of (response_text, updated_context)
        """
        message = current_message_for_llm
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

"""
Appeal creation/update tool handler for the chat interface.

Handles create_or_update_appeal tool calls from the LLM to create
or update appeal records linked to the current chat.
"""

import json
import re
from typing import Any, Awaitable, Callable, Optional, Tuple

from loguru import logger

from fighthealthinsurance.utils import aget_related

from .base_tool import (
    BaseTool,
    is_safe_tool_field,
    parse_anchored_json_payload,
    settable_model_fields,
    strip_anchored_calls,
)
from .patterns import CREATE_OR_UPDATE_APPEAL_REGEX


class AppealTool(BaseTool):
    """
    Tool handler for creating or updating appeal records.

    When the LLM includes a create_or_update_appeal call in its response, this tool:
    1. Extracts the JSON parameters (appeal fields)
    2. Creates or updates an Appeal record linked to the chat
    3. Updates the associated Denial record
    4. Returns a response with the appeal link
    """

    pattern = CREATE_OR_UPDATE_APPEAL_REGEX
    detect_flags: int = re.DOTALL | re.MULTILINE | re.IGNORECASE
    # The pattern is ^...$-anchored, so every scan needs MULTILINE -- the
    # base default (no MULTILINE) made the on-error strip in
    # BaseTool.handle miss a call that follows a line of prose, leaking raw
    # tool syntax (and its JSON payload) into the chat when execute raised.
    detect_all_flags: int = re.DOTALL | re.MULTILINE | re.IGNORECASE
    name = "Appeal"
    # Models legitimately emit several update calls in one reply; since
    # execute() replaces only the exact call span, each remaining call must
    # get its own pass or it renders as raw tool syntax.
    max_calls_per_reply: int = 3

    def strip_calls_on_error(self, response_text: str) -> str:
        """Span-bounded on-error strip: the greedy DOTALL pattern would also
        delete the text (and any other pending tool call) between two calls."""
        return strip_anchored_calls(self, response_text)

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        send_error_message: Optional[Callable[[str], Awaitable[None]]] = None,
        domain: str = "",
    ):
        """
        Initialize the Appeal tool.

        Args:
            send_status_message: Async function to send status updates
            send_error_message: Async function to send error messages
            domain: The domain URL for generating appeal links
        """
        super().__init__(send_status_message)
        self.send_error_message = send_error_message or send_status_message
        self.domain = domain

    async def execute(
        self,
        match: re.Match[str],
        response_text: str,
        context: str,
        chat: Any = None,
        **kwargs,
    ) -> Tuple[str, str]:
        """
        Execute appeal creation/update.

        Args:
            match: Regex match containing JSON parameters
            response_text: Current LLM response
            context: Current context string
            chat: The OngoingChat object to link the appeal to

        Returns:
            Tuple of (updated_response, updated_context)
        """
        if not chat:
            logger.warning("AppealTool called without chat object")
            await self.send_error_message("Cannot create appeal: no chat context")
            return response_text, context

        try:
            # Precise payload + span: the greedy anchored pattern can
            # over-capture into a later tool call on another line (see
            # parse_anchored_json_payload); replacing call_span rather than
            # match.group(0) keeps that later call intact for its own handler.
            appeal_data, call_span = parse_anchored_json_payload(response_text, match)
            await self.send_status_message("Processing update appeal data...")

            appeal, denial = await self._get_or_create_appeal(chat, appeal_data)

            if appeal and denial:
                await self._update_appeal_fields(appeal, denial, appeal_data)
                await appeal.asave()
                await denial.asave()

                # count=1: byte-identical duplicate calls each get their own
                # pass via max_calls_per_reply instead of one replacement
                # landing at every occurrence.
                cleaned_response = response_text.replace(
                    call_span,
                    f"I've created/updated [Appeal #{appeal.id}]({self.domain}/appeals/{appeal.id}) for you.",
                    1,
                )
                await self.send_status_message(
                    f"Appeal #{appeal.id} has been created/updated successfully."
                )
                return cleaned_response, context
            else:
                cleaned_response = response_text.replace(
                    call_span,
                    "I couldn't create or update the appeal.",
                    1,
                )
                await self.send_status_message("Failed to create or update appeal.")
                return cleaned_response, context

        except json.JSONDecodeError as e:
            # No payload content in the log or the error frame: the appeal
            # JSON carries medical/claim details (PHI) -- sizes only.
            logger.warning(
                f"Invalid JSON in create_or_update_appeal token "
                f"({len(match.group(1))} chars): {e.msg} at pos {e.pos}"
            )
            await self.send_error_message(
                "Error processing appeal data: the appeal details were not "
                "valid JSON. Please try again."
            )
            raise

        except Exception as e:
            logger.opt(exception=True).warning(f"Error processing appeal data: {e}")
            await self.send_error_message(f"Error processing appeal data: {str(e)}")
            raise

    async def _get_or_create_appeal(
        self, chat: Any, appeal_data: dict
    ) -> Tuple[Any, Any]:
        """
        Get existing appeal or create a new one.

        Args:
            chat: The OngoingChat object
            appeal_data: Dictionary of appeal field values

        Returns:
            Tuple of (appeal, denial) objects
        """
        # Import here to avoid circular imports
        from fighthealthinsurance.models import Appeal, Denial

        appeal = None
        denial = None

        if await chat.appeals.aexists():
            # select_related caches for_denial so the attribute access below
            # stays async-safe without a bridge.
            appeal = await chat.appeals.select_related("for_denial").afirst()
            if appeal:
                await self.send_status_message(f"Updating existing Appeal #{appeal.id}")
                denial = appeal.for_denial
        else:
            pro_user = await aget_related(chat, "professional_user")
            denial = await Denial.objects.acreate(creating_professional=pro_user)
            appeal = await Appeal.objects.acreate(
                chat=chat, creating_professional=pro_user, for_denial=denial
            )

            # Add hashed email if not provided
            if "hashed_email" not in appeal_data:
                if chat.hashed_email:
                    appeal_data["hashed_email"] = chat.hashed_email
                elif chat.user_id is not None:
                    user = await aget_related(chat, "user")
                    if user and user.email:
                        appeal_data["hashed_email"] = Denial.get_hashed_email(
                            user.email
                        )
                elif appeal_data.get("email"):
                    appeal_data["hashed_email"] = Denial.get_hashed_email(
                        appeal_data["email"]
                    )

        return appeal, denial

    async def _update_appeal_fields(
        self, appeal: Any, denial: Any, appeal_data: dict
    ) -> None:
        """
        Update appeal and denial fields from the data dictionary.

        Args:
            appeal: The Appeal object to update
            denial: The Denial object to update
            appeal_data: Dictionary of field values
        """
        # Allowlist, not hasattr: hasattr/setattr accepted ANY attribute name,
        # letting a crafted (or just confused) LLM payload overwrite pks,
        # relation ids, methods, or private state on both models.
        appeal_allowed = settable_model_fields(type(appeal))
        denial_allowed = settable_model_fields(type(denial))
        for key, value in appeal_data.items():
            set_field = False

            if is_safe_tool_field(key, appeal_allowed):
                set_field = True
                setattr(appeal, key, value)

            if is_safe_tool_field(key, denial_allowed):
                set_field = True
                setattr(denial, key, value)

            if not set_field:
                logger.warning(
                    f"Key {key} not settable on Appeal or Denial model. Skipping."
                )
                await self.send_status_message(
                    f"Key {key} not found in Appeal or Denial model. "
                    f"The value {value} is not synced back yet."
                )

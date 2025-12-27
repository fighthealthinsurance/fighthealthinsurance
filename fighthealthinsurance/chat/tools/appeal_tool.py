"""
Appeal creation/update tool handler for the chat interface.

Handles create_or_update_appeal tool calls from the LLM to create
or update appeal records linked to the current chat.
"""
import json
import re
from typing import Optional, Tuple, Callable, Awaitable, Any
from asgiref.sync import sync_to_async
from loguru import logger

from .base_tool import BaseTool
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
    name = "Appeal"

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
        **kwargs
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

        json_data = match.group(1).strip()

        try:
            appeal_data = json.loads(json_data)
            await self.send_status_message("Processing update appeal data...")

            appeal, denial = await self._get_or_create_appeal(chat, appeal_data)

            if appeal and denial:
                await self._update_appeal_fields(appeal, denial, appeal_data)
                await appeal.asave()
                await denial.asave()

                cleaned_response = response_text.replace(
                    match.group(0),
                    f"I've created/updated [Appeal #{appeal.id}]({self.domain}/appeals/{appeal.id}) for you.",
                )
                await self.send_status_message(
                    f"Appeal #{appeal.id} has been created/updated successfully."
                )
                return cleaned_response, context
            else:
                cleaned_response = response_text.replace(
                    match.group(0),
                    "I couldn't create or update the appeal.",
                )
                await self.send_status_message("Failed to create or update appeal.")
                return cleaned_response, context

        except json.JSONDecodeError as e:
            logger.warning(
                f"Invalid JSON data {e} in create_or_update_appeal token: {json_data}"
            )
            await self.send_error_message(
                f"Error processing appeal data: Invalid JSON format {e} -- {json_data}"
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
            appeal = await chat.appeals.afirst()
            if appeal:
                await self.send_status_message(
                    f"Updating existing Appeal #{appeal.id}"
                )
                denial = await sync_to_async(lambda x: x.denial)(appeal)
        else:
            pro_user = await sync_to_async(lambda: chat.professional_user)()
            denial = await Denial.objects.acreate(creating_professional=pro_user)
            appeal = await Appeal.objects.acreate(
                chat=chat, creating_professional=pro_user, for_denial=denial
            )

            # Add hashed email if not provided and user exists
            if (
                "hashed_email" not in appeal_data
                and hasattr(chat, "user")
                and chat.user
            ):
                user_email = await sync_to_async(lambda: chat.user.email)()
                if user_email:
                    appeal_data["hashed_email"] = Denial.get_hashed_email(user_email)

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
        for key, value in appeal_data.items():
            set_field = False

            if hasattr(appeal, key):
                set_field = True
                setattr(appeal, key, value)

            if hasattr(denial, key):
                set_field = True
                setattr(denial, key, value)

            if not set_field:
                logger.warning(
                    f"Key {key} not found in Appeal or Denial model. Skipping."
                )
                await self.send_status_message(
                    f"Key {key} not found in Appeal or Denial model. "
                    f"The value {value} is not synced back yet."
                )

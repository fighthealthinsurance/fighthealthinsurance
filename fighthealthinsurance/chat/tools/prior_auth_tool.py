"""
Prior authorization creation/update tool handler for the chat interface.

Handles create_or_update_prior_auth tool calls from the LLM to create
or update prior authorization records linked to the current chat.
"""

import json
import re
from typing import Optional, Tuple, Callable, Awaitable, Any
from loguru import logger

from .base_tool import BaseTool
from .patterns import CREATE_OR_UPDATE_PRIOR_AUTH_REGEX


class PriorAuthTool(BaseTool):
    """
    Tool handler for creating or updating prior authorization records.

    When the LLM includes a create_or_update_prior_auth call in its response, this tool:
    1. Extracts the JSON parameters (prior auth fields)
    2. Creates or updates a PriorAuthRequest record linked to the chat
    3. Returns a response with the prior auth link
    """

    pattern = CREATE_OR_UPDATE_PRIOR_AUTH_REGEX
    name = "Prior Auth"

    # Field name mappings for normalization
    FIELD_MAPPINGS = {
        "medication": "treatment",
        "medication_name": "treatment",
    }

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        send_error_message: Optional[Callable[[str], Awaitable[None]]] = None,
        domain: str = "",
    ):
        """
        Initialize the Prior Auth tool.

        Args:
            send_status_message: Async function to send status updates
            send_error_message: Async function to send error messages
            domain: The domain URL for generating prior auth links
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
        Execute prior authorization creation/update.

        Args:
            match: Regex match containing JSON parameters
            response_text: Current LLM response
            context: Current context string
            chat: The OngoingChat object to link the prior auth to

        Returns:
            Tuple of (updated_response, updated_context)
        """
        if not chat:
            logger.warning("PriorAuthTool called without chat object")
            await self.send_error_message("Cannot create prior auth: no chat context")
            return response_text, context

        json_data = match.group(1).strip()

        try:
            prior_auth_data = json.loads(json_data)
            await self.send_status_message(
                "Processing prior authorization update/create data..."
            )

            prior_auth = await self._get_or_create_prior_auth(chat)

            if prior_auth:
                await self._update_prior_auth_fields(prior_auth, prior_auth_data)
                await prior_auth.asave()

                cleaned_response = response_text.replace(
                    match.group(0),
                    f"I've created/updated [Prior Auth Request #{prior_auth.id}]"
                    f"({self.domain}/prior-auths/view/{prior_auth.id}) for you.",
                )
                await self.send_status_message(
                    f"Prior Auth Request #{prior_auth.id} has been created/updated "
                    "successfully."
                )
                return cleaned_response, context
            else:
                cleaned_response = response_text.replace(
                    match.group(0),
                    "I couldn't create or update the prior authorization request.",
                )
                await self.send_status_message(
                    "Failed to create or update prior authorization request."
                )
                return cleaned_response, context

        except json.JSONDecodeError:
            logger.warning(
                f"Invalid JSON data in create_or_update_prior_auth token: {json_data}"
            )
            await self.send_status_message(
                "Error processing prior auth data: Invalid JSON format."
            )
            raise

        except Exception as e:
            logger.opt(exception=True).warning(f"Error processing prior auth data: {e}")
            await self.send_status_message(
                f"Error processing prior auth data: {str(e)}"
            )
            raise

    async def _get_or_create_prior_auth(self, chat: Any) -> Any:
        """
        Get existing prior auth or create a new one.

        Args:
            chat: The OngoingChat object

        Returns:
            PriorAuthRequest object
        """
        # Import here to avoid circular imports
        from fighthealthinsurance.models import PriorAuthRequest

        prior_auth = None

        if await chat.prior_auths.aexists():
            prior_auth = await chat.prior_auths.afirst()
            if prior_auth:
                await self.send_status_message(
                    f"Updating existing Prior Auth Request #{prior_auth.id}"
                )
        else:
            prior_auth = await PriorAuthRequest.objects.acreate(
                chat=chat,
                creator_professional_user=chat.professional_user,
            )

        return prior_auth

    async def _update_prior_auth_fields(
        self, prior_auth: Any, prior_auth_data: dict
    ) -> None:
        """
        Update prior auth fields from the data dictionary.

        Args:
            prior_auth: The PriorAuthRequest object to update
            prior_auth_data: Dictionary of field values
        """
        for key, value in prior_auth_data.items():
            # Normalize the key
            key = key.lower().strip()

            # Apply field mappings
            if key in self.FIELD_MAPPINGS:
                key = self.FIELD_MAPPINGS[key]

            if hasattr(prior_auth, key):
                setattr(prior_auth, key, value)
            else:
                logger.warning(f"Key {key} not found in Prior Auth model. Skipping.")

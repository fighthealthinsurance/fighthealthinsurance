import json
import uuid
from loguru import logger
import asyncio
from django.utils import timezone
from asgiref.sync import sync_to_async
from typing import Optional

from channels.generic.websocket import AsyncWebsocketConsumer

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.models import (
    PriorAuthRequest,
    ProposedPriorAuth,
    OngoingChat,
    ProfessionalUser,
)
from fighthealthinsurance.generate_prior_auth import prior_auth_generator
from .chat_interface import ChatInterface


class StreamingAppealsBackend(AsyncWebsocketConsumer):
    """Streaming back the appeals as json :D"""

    async def connect(self):
        logger.debug("Accepting connection for appeals")
        await self.accept()

    async def disconnect(self, close_code):
        logger.debug("Disconnecting appeals")
        pass

    async def receive(self, text_data):
        data = json.loads(text_data)
        logger.debug("Starting generation of appeals...")
        aitr = common_view_logic.AppealsBackendHelper.generate_appeals(data)
        # We do a try/except here to log since the WS framework swallow exceptions sometimes
        try:
            await asyncio.sleep(1)
            await self.send("\n")
            async for record in aitr:
                await asyncio.sleep(0)
                await self.send("\n")
                await asyncio.sleep(0)
                logger.debug(f"Sending record {record}")
                await self.send(record)
                await asyncio.sleep(0)
                await self.send("\n")
        except Exception as e:
            logger.opt(exception=True).debug(f"Error sending back appeals: {e}")
            raise e
        finally:
            await asyncio.sleep(1)
            await self.close()
        logger.debug("All sent")


class StreamingEntityBackend(AsyncWebsocketConsumer):
    """Streaming Entity Extraction"""

    async def connect(self):
        logger.debug("Accepting connection for entity extraction")
        await self.accept()

    async def disconnect(self, close_code):
        logger.debug("Disconnecting entity extraction")
        pass

    async def receive(self, text_data):
        data = json.loads(text_data)
        aitr = common_view_logic.DenialCreatorHelper.extract_entity(data["denial_id"])

        try:
            async for record in aitr:
                logger.debug(f"Sending record {record}")
                await self.send(record)
                await asyncio.sleep(0)
                await self.send("\n")
            await asyncio.sleep(1)
            logger.debug(f"Sent all records")
        except Exception as e:
            logger.opt(exception=True).debug(f"Error sending back entity: {e}")
            raise e
        finally:
            await asyncio.sleep(1)
            await self.close()
            logger.debug("Closed connection")


class PriorAuthConsumer(AsyncWebsocketConsumer):
    """Streaming back the proposed prior authorizations as JSON."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pag = prior_auth_generator
        self.groups = []

    async def connect(self):
        logger.debug("Accepting connection for prior auth streaming")
        await self.accept()

    async def disconnect(self, close_code):
        logger.debug(f"Disconnecting prior auth streaming with code {close_code}")
        pass

    async def receive(self, text_data):
        data = json.loads(text_data)
        logger.debug("Starting generation of prior auth proposals...")

        # Validate the token and ID
        token = data.get("token")
        prior_auth_id = data.get("id")

        if not token or not prior_auth_id:
            await self.send(json.dumps({"error": "Missing token or prior auth ID"}))
            await self.close()
            return

        # Get the prior auth request
        try:
            prior_auth = await self._get_prior_auth_request(prior_auth_id, token)
            if not prior_auth:
                await self.send(json.dumps({"error": "Invalid token or prior auth ID"}))
                await self.close()
                return

            # Generate prior auth proposals
            await self.send(
                json.dumps(
                    {
                        "status": "generating",
                        "message": "Starting to generate prior authorization proposals",
                    }
                )
            )

            # Update status
            await self._update_prior_auth_status(prior_auth, "prior_auth_requested")

            # Generate proposals
            generator = self.pag.generate_prior_auth_proposals(prior_auth)

            # We do a try/except here to log since the WS framework may swallow exceptions
            try:
                await asyncio.sleep(1)
                async for proposal in generator:
                    await asyncio.sleep(0)  # Allow other tasks to run
                    if "proposed_id" in proposal:
                        logger.debug(f"Sending proposal {proposal['proposed_id']}")
                        await self.send(json.dumps(proposal))
                        logger.debug(f"Sent!")
                    elif "error" in proposal:
                        logger.error(f"Error in proposal: {proposal['error']}")
                    else:
                        logger.debug(f"Malformed proposal {proposal}")
                    await asyncio.sleep(0)
            except Exception as e:
                logger.opt(exception=True).debug(
                    f"Error sending back prior auth proposals: {e}"
                )
                raise e
            finally:
                await asyncio.sleep(1)
                await self.close()

            logger.debug("All prior auth proposals sent")
        except Exception as e:
            logger.opt(exception=True).debug(f"Error in prior auth consumer: {e}")
            await self.send(json.dumps({"error": f"Server error: {str(e)}"}))
            await self.close()

    async def _get_prior_auth_request(self, prior_auth_id, token):
        """Get the prior auth request and validate the token."""
        try:
            prior_auth = await PriorAuthRequest.objects.select_related(
                "creator_professional_user",
                "created_for_professional_user",
                "created_for_professional_user",
                "created_for_professional_user__user",
                "creator_professional_user__user",
                "creator_professional_user",
            ).aget(id=prior_auth_id, token=token)
            return prior_auth
        except PriorAuthRequest.DoesNotExist:
            return None

    async def _update_prior_auth_status(self, prior_auth, status):
        """Update the status of the prior auth request."""
        prior_auth.status = status
        await prior_auth.asave()
        return prior_auth


class OngoingChatConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for ongoing chat with LLM for pro users."""

    chat_interface: Optional[ChatInterface] = None

    async def connect(self):
        logger.debug("Accepting connection for ongoing chat")
        await self.accept()

    async def send_json_message(self, data: dict):
        """Helper to send JSON data to the client."""
        await self.send(text_data=json.dumps(data))

    async def disconnect(self, close_code):
        logger.debug(f"Disconnecting ongoing chat with code {close_code}")
        pass

    async def receive(self, text_data):
        data = json.loads(text_data)
        logger.debug(f"Received message for ongoing chat {data}")

        # Get the required data -- note the message should be sent as "content"
        # but we also accept "message" for backward compatibility.
        message = data.get("message", data.get("content", None))
        chat_id = data.get("chat_id", None)
        replay_requested = data.get("replay", False)
        iterate_on_appeal = data.get("iterate_on_appeal")
        iterate_on_prior_auth = data.get("iterate_on_prior_auth")

        logger.debug(f"Message: {message} replay {replay_requested} chat_id {chat_id} iterate_on_appeal {iterate_on_appeal} iterate_on_prior_auth {iterate_on_prior_auth}")

        # Validate we have the required data
        if replay_requested and not chat_id:
            await self.send_json_message(
                {"error": "Chat ID is required for replay."}
            )  # Use helper
            return

        # Get the user from scope (authenticated by Django Channels)
        user = self.scope.get("user")

        if not user or not user.is_authenticated:
            await self.send_json_message(
                {"error": "User not authenticated."}
            )  # Use helper
            await self.close()
            return

        try:
            professional_user = await sync_to_async(self._get_professional_user)(user)
            if not professional_user:
                await self.send_json_message(
                    {"error": "Professional user not found or not active."}
                )  # Use helper
                return

            chat = await self._get_or_create_chat(professional_user, chat_id)
            if (
                not hasattr(self, "chat_interface")
                or self.chat_interface is None
                or chat.id != self.chat_interface.chat.id
            ):
                self.chat_interface = ChatInterface(
                    send_json_message_func=self.send_json_message, chat=chat
                )

            logger.debug(f"Chat: {chat.id}")

            if not replay_requested:
                if not message:
                    await self.send_json_message(
                        {"error": "Message content is required."}
                    )  # Use helper
                    return
                # Delegate to ChatInterface, passing new linking fields
                await self.chat_interface.handle_chat_message(
                    message,
                    iterate_on_appeal=iterate_on_appeal,
                    iterate_on_prior_auth=iterate_on_prior_auth,
                    user=user,
                )
            else:
                # Delegate replay to ChatInterface
                await self.chat_interface.replay_chat_history()

        except Exception as e:
            logger.opt(exception=True).debug(f"Error in ongoing chat: {e}")
            await self.send_json_message(
                {"error": f"Server error: {str(e)}"}
            )  # Use helper

    def _get_professional_user(self, user):
        """Get the professional user from the Django user."""
        try:
            return ProfessionalUser.objects.get(user=user, active=True)
        except ProfessionalUser.DoesNotExist:
            return None

    async def _get_or_create_chat(self, professional_user, chat_id=None):
        """Get an existing chat or create a new one."""
        if chat_id:
            try:
                return await OngoingChat.objects.aget(
                    id=chat_id, professional_user=professional_user
                )
            except OngoingChat.DoesNotExist:
                # Fall through to create a new chat
                logger.warning(
                    f"Chat with id {chat_id} not found for user {professional_user.id}. Creating new chat."
                )
                pass  # Fall through to create a new one

        # Create a new chat
        logger.info(f"Creating new chat for user {professional_user.id}")
        return await OngoingChat.objects.acreate(
            professional_user=professional_user,
            chat_history=[],
            summary_for_next_call=[],
        )

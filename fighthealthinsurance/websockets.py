import json
import uuid
from loguru import logger
import asyncio
from django.utils import timezone
from asgiref.sync import sync_to_async

from channels.generic.websocket import AsyncWebsocketConsumer

from fighthealthinsurance import common_view_logic
from fighthealthinsurance.models import (
    PriorAuthRequest,
    ProposedPriorAuth,
    OngoingChat,
    ProfessionalUser,
)
from fighthealthinsurance.generate_prior_auth import prior_auth_generator


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

    async def connect(self):
        logger.debug("Accepting connection for ongoing chat")
        await self.accept()

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

        logger.debug(f"Message: {message} replay {replay_requested} chat_id {chat_id}")

        # Validate we have the required data
        if replay_requested and not chat_id:
            await self.send(
                json.dumps({"error": "chat_id is required for replay requests"})
            )
            await self.close()
            return

        # Get the user from scope (authenticated by Django Channels)
        user = self.scope.get("user")

        if not user or not user.is_authenticated:
            await self.send(json.dumps({"error": "Authentication required"}))
            await self.close()
            return

        try:
            # Get or create professional user
            professional_user = await sync_to_async(self._get_professional_user)(user)

            if not professional_user:
                await self.send(json.dumps({"error": "Professional user not found"}))
                await self.close()
                return

            # Get or create the chat session
            chat = await self._get_or_create_chat(professional_user, chat_id)

            logger.debug(f"Chat: {chat.id}")

            if not replay_requested:
                logger.debug(f"Generating response for message: {message}")
                # Generate response (this also updates chat history)
                response = await self._generate_llm_response(chat, message)
                logger.debug(f"Response: {response} to send")

                # Send response to the client
                await self.send(
                    json.dumps(
                        {
                            "chat_id": str(chat.id),
                            "role": "assistant",
                            "content": response,
                        }
                    )
                )
                logger.debug(f"Sent response: {response}")

            else:
                await self.send(
                    json.dumps({"messages": chat.chat_history, "chat_id": str(chat.id)})
                )

        except Exception as e:
            logger.opt(exception=True).debug(f"Error in ongoing chat: {e}")
            await self.send(json.dumps({"error": f"Server error: {str(e)}"}))

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
                pass

        # Create a new chat
        return await OngoingChat.objects.acreate(
            professional_user=professional_user,
            chat_history=[],
            summary_for_next_call=[],
        )

    async def _generate_llm_response(self, chat, message):
        """Generate a response from the LLM."""
        from fighthealthinsurance.ml.ml_router import ml_router

        # Get the available *internal* text generation model
        models = ml_router.get_chat_backends(use_external=False)
        if not models:
            return "Sorry, no language models are currently available."

        context = None
        if chat.summary_for_next_call and len(chat.summary_for_next_call) > 0:
            context = chat.summary_for_next_call[-1]

        new_chat = False
        for model in models:
            try:
                # Add our current chat message to the chat history
                if not chat.chat_history:
                    chat.chat_history = []  # type: ignore
                if not chat.summary_for_next_call:
                    chat.summary_for_next_call = []  # type: ignore
                if not chat.chat_history and not chat.summary_for_next_call:
                    new_chat = True
                # Generate the response using the model
                intro_opt = ""
                if new_chat:
                    pro_user_info = await sync_to_async(
                        chat.summarize_professional_user
                    )()
                    intro_opt = (
                        f"You are a helpful assistant talking with a professional user, {pro_user_info}. "
                        f"You are helping a professional user with their ongoing chat. You likely do not need to immeditely generate a prior auth or appeal instead you'll have a chat with the professional, {pro_user_info}, about their needs. Now here is what they said to start the conversation:"
                    )
                (response_text, context_part) = await model.generate_chat_response(
                    intro_opt + message,
                    previous_context_summary=context,
                    history=chat.chat_history,
                )
                if response_text and response_text.strip() != "":
                    # Add the user's message to the chat history
                    # We intentionally do this after the model call so we
                    # avoid loading a message which breaks the model
                    # (also since we try multiple models so we don't load
                    # the message multiple times)
                    chat.chat_history.append(
                        {
                            "role": "user",
                            "content": message,
                            "timestamp": timezone.now().isoformat(),
                        }
                    )
                    # Save the context summary if present.
                    if context_part:
                        chat.summary_for_next_call.append(context_part)
                    # Add the assistant's response to the chat history
                    chat.chat_history.append(
                        {
                            "role": "assistant",
                            "content": response_text,
                            "timestamp": timezone.now().isoformat(),
                        }
                    )
                    await chat.asave()
                    return response_text.strip()

            except Exception as e:
                await asyncio.sleep(1)
                logger.opt(exception=True).debug(f"Error generating LLM response: {e}")
        logger.debug(
            f"Failed to generate response for message: {message} in chat {chat.id}"
        )
        return "Sorry, I encountered an error while processing your request."

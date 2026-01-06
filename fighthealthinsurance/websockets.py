import asyncio
import json
import re
import uuid
from typing import AsyncIterator, Optional

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from loguru import logger

from fhi_users.audit import TrackingInfo, extract_tracking_info_from_scope
from fighthealthinsurance import common_view_logic
from fighthealthinsurance.generate_prior_auth import prior_auth_generator
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import (
    ChatLeads,
    Denial,
    OngoingChat,
    PriorAuthRequest,
    ProfessionalUser,
    ProposedPriorAuth,
)

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
        logger.debug("Waiting for message....")
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON received in appeals websocket: {e}")
            await self.send(json.dumps({"error": "Invalid JSON format"}))
            await self.close()
            return
        logger.debug("Starting generation of appeals...")
        aitr: AsyncIterator[str] = (
            common_view_logic.AppealsBackendHelper.generate_appeals(data)
        )
        # We do a try/except here to log since the WS framework swallow exceptions sometimes
        try:
            await asyncio.sleep(1)
            await self.send("\n")
            count = 0
            async for record in aitr:
                count = count + 1
                await asyncio.sleep(0)
                await self.send("\n")
                await asyncio.sleep(0)
                logger.debug(f"Sending record {record}")
                await self.send(record)
                await asyncio.sleep(0)
                await self.send("\n")
            logger.debug(f"All records, {count} in total, sent.")
        except Exception as e:
            logger.opt(exception=True).debug(f"Error sending back appeals: {e}")
            raise e
        finally:
            logger.debug("Yielding before closing connection")
            await asyncio.sleep(0.1)
            try:
                logger.debug("Closing connection...")
                await self.close()
                logger.debug("Closed")
            except Exception:
                logger.debug("Error closing connection")
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
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON received in entity extraction websocket: {e}")
            await self.send(json.dumps({"error": "Invalid JSON format"}))
            await self.close()
            return
        if "denial_id" not in data:
            logger.warning("Missing denial_id in entity extraction request")
            await self.send(json.dumps({"error": "Missing denial_id"}))
            await self.close()
            return
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
            logger.debug("Yielding before closing connection")
            await asyncio.sleep(0.1)
            try:
                logger.debug("Prepairing to close connection")
                await self.close()
                logger.debug("Closed connection")
            except Exception:
                logger.debug("Error closing connection")


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
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON received in prior auth websocket: {e}")
            await self.send(json.dumps({"error": "Invalid JSON format"}))
            await self.close()
            return
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
                        "type": "status",
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
    """WebSocket consumer for ongoing chat with LLMs for both pro users and patients."""

    chat_interface: Optional[ChatInterface] = None
    chat_id: Optional[str] = None

    async def connect(self):
        logger.debug("Accepting connection for ongoing chat")
        await self.accept()

    async def send_json_message(self, data: dict):
        """Helper to send JSON data to the client."""
        await self.send(text_data=json.dumps(data))

    async def disconnect(self, close_code):
        logger.debug(f"Disconnecting ongoing chat with code {close_code}")
        # If a chat was active, trigger analysis of denied items
        if self.chat_interface and self.chat_id:
            await self._analyze_denied_items(self.chat_id)

    async def _analyze_denied_items(self, chat_id: str):
        """
        Analyzes the chat history to identify denied items and reasons.
        This is run when the websocket disconnects to avoid blocking the chat flow.
        Adds logging for the prompt and guardrails to avoid storing unclear denials.
        """
        try:
            chat: OngoingChat = await OngoingChat.objects.aget(id=chat_id)

            # Only process this if we don't already have denied item information
            if not chat.denied_item or not chat.denied_reason:
                logger.info(f"Analyzing denied items for chat {chat_id}")

                # Get a backend model to analyze the chat
                models = ml_router.get_chat_backends(use_external=False)
                if not models:
                    logger.warning("No models available for denied item analysis")
                    return

                model = models[0]

                # Create a prompt to analyze the denied items
                history_text = ""
                if chat.chat_history and len(chat.chat_history) > 0:
                    for msg in chat.chat_history:
                        if msg.get("role") and msg.get("content"):
                            history_text += f"{msg['role']}: {msg['content']}\n\n"

                analysis_prompt = (
                    "Based on the conversation above, extract the following information:\n"
                    "1. What specific healthcare item or service was denied by the insurance company?\n"
                    "2. What was the reason given for the denial?\n"
                    "Respond in JSON format with two fields: 'denied_item' and 'denied_reason'.\n"
                    "If you cannot determine either field, respond with null for that field."
                )

                full_prompt = f"{history_text}\n{analysis_prompt}"
                logger.info(
                    f"Prompt for denied item extraction (chat {chat_id}):\n{full_prompt}"
                )

                # Get the analysis from the model
                response_text, _ = await model.generate_chat_response(
                    full_prompt,
                    is_professional=await sync_to_async(chat.is_professional_user)(),
                    is_logged_in=await sync_to_async(chat.is_logged_in_user)(),
                )

                if response_text:
                    # Extract JSON from response
                    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
                    if json_match:
                        try:
                            analysis_data = json.loads(json_match.group(0))
                            denied_item = analysis_data.get("denied_item")
                            denied_reason = analysis_data.get("denied_reason")

                            logger.info(
                                f"[DEBUG] chat_id={chat_id} denied_item={denied_item!r} denied_reason={denied_reason!r} (raw analysis_data={analysis_data!r})"
                            )

                            # Guardrails: Only store if both are non-empty, not null, and not generic/unclear
                            def is_clear(val):
                                if not val:
                                    return False
                                val_str = str(val).strip().lower()

                                unclear_phrases = [
                                    "not clear",
                                    "unknown",
                                    "unclear",
                                    "n/a",
                                    "none",
                                    "null",
                                    "",
                                    "no denial",
                                    "could not determine",
                                ]
                                return val_str not in unclear_phrases

                            updated = False
                            if is_clear(denied_item):
                                chat.denied_item = denied_item
                                updated = True
                                logger.info(
                                    f"Updated chat {chat_id} with denied item: {denied_item}"
                                )
                            if is_clear(denied_reason):
                                chat.denied_reason = denied_reason
                                updated = True
                                logger.info(
                                    f"Updated chat {chat_id} with denied reason: {denied_reason}"
                                )
                            if updated:
                                await chat.asave()
                            else:
                                logger.info(
                                    f"Did not store denied item or reason for chat {chat_id} due to unclear or missing values."
                                )
                        except json.JSONDecodeError:
                            logger.warning(
                                f"Could not parse JSON from analysis response: {response_text}"
                            )
                    else:
                        logger.warning(
                            f"No JSON found in analysis response: {response_text}"
                        )
                else:
                    logger.warning(f"No response from model for denied item analysis")
            else:
                logger.info(f"Chat {chat_id} already has denied item information")
        except Exception as e:
            logger.opt(exception=True).error(
                f"Error analyzing denied items for chat {chat_id}: {e}"
            )

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON received in ongoing chat websocket: {e}")
            await self.send(json.dumps({"error": "Invalid JSON format"}))
            await self.close()
            return
        logger.debug(f"Received message for ongoing chat {data}")

        # Get the required data -- note the message should be sent as "content"
        # but we also accept "message" for backward compatibility.
        message = data.get("message", data.get("content", None))
        chat_id = data.get("chat_id", self.chat_id)
        replay_requested = data.get("replay", False)
        iterate_on_appeal = data.get("iterate_on_appeal")
        iterate_on_prior_auth = data.get("iterate_on_prior_auth")
        is_patient = data.get(
            "is_patient", False
        )  # New parameter to identify patient users
        email = data.get("email", None)  # Email for patient users so we can delete data
        session_key = data.get("session_key", None)  # Session key for anonymous users
        microsite_slug = data.get(
            "microsite_slug", None
        )  # Microsite slug if coming from a microsite
        # Allow users to opt-in to using external/public models as fallback
        # Only used when FHI models fail or timeout
        use_external_models = data.get("use_external_models", False)

        # Validate microsite_slug if provided
        if microsite_slug:
            from fighthealthinsurance.microsites import get_microsite

            if not get_microsite(microsite_slug):
                logger.warning(f"Invalid microsite_slug received: {microsite_slug}")
                microsite_slug = None

        logger.debug(
            f"Message: {message} replay {replay_requested} chat_id {chat_id} "
            f"iterate_on_appeal {iterate_on_appeal} iterate_on_prior_auth {iterate_on_prior_auth} "
            f"is_patient {is_patient} session_key {session_key} microsite_slug {microsite_slug} "
            f"use_external_models {use_external_models}"
        )

        # Validate we have the required data
        if replay_requested and not chat_id:
            await self.send_json_message({"error": "Chat ID is required for replay."})
            return

        # Get the user from scope (authenticated by Django Channels)
        user = self.scope.get("user")
        is_authenticated = user and user.is_authenticated

        # For anonymous professional users, we need a session key
        if not is_authenticated and not session_key:
            await self.send_json_message(
                {"error": "Session key is required for anonymous professional users."}
            )
            return

        # For patients we need the e-mail to allow data deletion since we don't have
        # accounts or lead objects to link to.
        if is_patient:
            if not email:
                await self.send_json_message(
                    {"error": "Email is required for patient users."}
                )
                return
            try:
                # Validate the email format
                validate_email(email)
            except ValidationError:
                await self.send_json_message({"error": "Invalid email format."})
                return

        try:
            # Handle different user types
            if not is_authenticated:
                # Anonymous user with session key
                professional_user = None

                # Check if this is a trial chat (verify session_id exists in ChatLeads)
                if session_key:
                    if await ChatLeads.objects.filter(session_id=session_key).aexists():
                        is_patient = False
                    # This is a trial professional chat with a valid ChatLead entry
                    logger.info(f"Trial professional chat for session {session_key}")
                else:
                    is_patient = True  # Default to patient if we can't find the chat lead & non-auth
            elif is_patient:
                # Patient user (authenticated) -- not yet supported.
                professional_user = None
            else:
                # Professional user
                professional_user = await sync_to_async(self._get_professional_user)(
                    user
                )
                if not professional_user:
                    # Instead of an error message, set as patient user when pro user not found
                    logger.info(
                        f"User {user.username} is not a professional user, treating as patient"
                    )
                    professional_user = None
                    is_patient = True
            # Extract tracking info from websocket scope (privacy-aware)
            tracking_info = extract_tracking_info_from_scope(
                scope=self.scope, is_professional=(professional_user is not None)
            )

            chat = await self._get_or_create_chat(
                user,
                professional_user,
                is_patient,
                chat_id,
                session_key,
                email=email,
                microsite_slug=microsite_slug,
                tracking_info=tracking_info,
            )
            self.chat_id = chat.id
            if (
                not hasattr(self, "chat_interface")
                or self.chat_interface is None
                or chat.id != self.chat_interface.chat.id
            ):
                self.chat_interface = ChatInterface(
                    send_json_message_func=self.send_json_message,
                    chat=chat,
                    user=user,
                    is_patient=is_patient,
                    use_external_models=use_external_models,
                )
            elif use_external_models != self.chat_interface.use_external_models:
                # User changed their preference for external models mid-chat
                self.chat_interface.use_external_models = use_external_models

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

    async def _get_or_create_chat(
        self,
        user,
        professional_user=None,
        is_patient=False,
        chat_id=None,
        session_key=None,
        email=None,  # Email for patient users so we can handle data deletion later
        microsite_slug=None,  # Microsite slug if coming from a microsite
        tracking_info: Optional[TrackingInfo] = None,
    ):
        """Get an existing chat or create a new one."""
        if chat_id:
            # Chat ids should be secure they're random UUIDs.
            try:
                chat = (
                    await OngoingChat.objects.prefetch_related()
                    .select_related()
                    .aget(id=chat_id)
                )
                # But let's also check session key and user just to be safe.
                if chat.session_key and session_key != chat.session_key:
                    raise OngoingChat.DoesNotExist("Session key mismatch")
                if chat.user and chat.user != user:
                    raise OngoingChat.DoesNotExist("User mismatch")

                # Update microsite_slug if provided and not already set
                # Only save on first message to avoid excessive database writes
                if microsite_slug and not chat.microsite_slug:
                    await OngoingChat.objects.filter(id=chat.id).aupdate(
                        microsite_slug=microsite_slug
                    )
                    # Refresh the chat object to ensure consistency
                    await chat.arefresh_from_db()

                return chat
            except OngoingChat.DoesNotExist as e:
                logger.warning(
                    f"Chat with id {chat_id} not found. Creating new chat.", e
                )
                pass  # Fall through to create a new one

        # Create a new chat
        if session_key:
            # Anonymous user
            logger.info(f"Creating new anonymous chat for session {session_key[:8]}")

            # Check if this is a trial professional chat
            is_trial_professional = False
            try:
                await ChatLeads.objects.aget(session_id=session_key)
                is_trial_professional = True
            except ChatLeads.DoesNotExist:
                # Regular anonymous chat
                pass

            # Also store hashed_email for trial professionals
            hashed_email = None
            if not is_trial_professional and email:
                hashed_email = Denial.get_hashed_email(email)

            # Build tracking kwargs
            tracking_kwargs = tracking_info.to_model_kwargs() if tracking_info else {}

            return await OngoingChat.objects.acreate(
                session_key=session_key,
                chat_history=[],
                summary_for_next_call=[],
                is_patient=not is_trial_professional,  # Not a patient if it's a trial professional
                hashed_email=hashed_email,
                microsite_slug=microsite_slug,
                **tracking_kwargs,
            )
        elif is_patient and user and user.is_authenticated:
            # Patient user
            logger.info(f"Creating new patient chat for user {user.id}")
            # Build tracking kwargs
            tracking_kwargs = tracking_info.to_model_kwargs() if tracking_info else {}
            return await OngoingChat.objects.acreate(
                user=user,
                is_patient=True,
                chat_history=[],
                summary_for_next_call=[],
                hashed_email=Denial.get_hashed_email(email) if email else None,
                microsite_slug=microsite_slug,
                **tracking_kwargs,
            )
        else:
            # Professional user
            logger.info(
                f"Creating new professional chat for user {professional_user.id}"
            )
            # Build tracking kwargs
            tracking_kwargs = tracking_info.to_model_kwargs() if tracking_info else {}
            return await OngoingChat.objects.acreate(
                professional_user=professional_user,
                chat_history=[],
                summary_for_next_call=[],
                microsite_slug=microsite_slug,
                **tracking_kwargs,
            )

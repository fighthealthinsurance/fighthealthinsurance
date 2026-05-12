import asyncio
import json
import os
import re
import uuid
from typing import AsyncIterator, Callable, Optional, Tuple, cast

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from loguru import logger

from fhi_users.audit import TrackingInfo, extract_tracking_info_from_scope
from fighthealthinsurance import common_view_logic
from fighthealthinsurance.ml.bad_output_utils import strip_boilerplate_service
from fighthealthinsurance.generate_prior_auth import prior_auth_generator
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import (
    ChatLeads,
    ChatType,
    Denial,
    OngoingChat,
    PriorAuthRequest,
    ProfessionalUser,
    ProposedAppeal,
    ProposedPriorAuth,
)

from .chat_interface import ChatInterface


def _get_client_ip_from_scope(scope: Optional[dict] = None) -> Optional[str]:
    """Extract client IP from websocket scope, handling common proxy headers."""
    if scope is None:
        return None

    headers = cast(dict[bytes, bytes], dict(scope.get("headers", [])))
    if b"x-forwarded-for" in headers:
        forwarded = headers[b"x-forwarded-for"].decode("utf-8", errors="ignore")
        return str(forwarded.split(",")[0].strip())
    if b"x-real-ip" in headers:
        return str(headers[b"x-real-ip"].decode("utf-8", errors="ignore").strip())

    client = scope.get("client")
    if isinstance(client, (list, tuple)) and client:
        first_value = client[0]
        if isinstance(first_value, str):
            return first_value
        if first_value is not None:
            return str(first_value)

    logger.warning("Unable to determine client IP from websocket scope")
    return None


async def log_zero_appeal_diagnostics(
    denial_id: Optional[object],
    status_count: int,
    last_status_phase: Optional[str],
    transport: str,
    stream_error: Optional[str] = None,
) -> None:
    """Emit a structured ERROR when an appeal session ends with 0 appeals delivered.

    Looks up persisted ProposedAppeal rows for the denial so we can
    distinguish 'server generated nothing' from 'server generated N
    appeals but they never made it to the client'. The latter case is
    invisible to the client-side error report.

    `transport` is "websocket" or "rest" so the same log surfaces both
    delivery paths in alerting.
    """
    # Coerce arbitrary JSON values to int for the FK/AutoField lookup.
    # Anything we can't coerce skips the DB cross-reference and just
    # gets logged as-is so we still surface the failure. bool is a
    # subclass of int in Python, so a JSON `true`/`false` would
    # otherwise coerce to 1/0 and point diagnostics at the wrong
    # denial record.
    denial_id_int: Optional[int] = None
    if isinstance(denial_id, bool):
        denial_id_int = None
    elif isinstance(denial_id, int):
        denial_id_int = denial_id
    elif isinstance(denial_id, str):
        try:
            denial_id_int = int(denial_id)
        except ValueError:
            denial_id_int = None

    persisted_count = -1
    denial_attempts: Optional[int] = None
    try:
        if denial_id_int is not None:
            # `denial_id` is Denial's PK so `for_denial_id=N` matches
            # the FK column directly. Using `for_denial__denial_id=`
            # would add a needless JOIN to the diagnostic path.
            persisted_count = await ProposedAppeal.objects.filter(
                for_denial_id=denial_id_int
            ).acount()
            denial = await Denial.objects.filter(denial_id=denial_id_int).afirst()
            if denial is not None:
                denial_attempts = denial.gen_attempts
    except Exception as lookup_error:
        logger.opt(exception=True).warning(
            f"Failed to look up persisted appeal count for denial "
            f"{denial_id}: {lookup_error}"
        )
    error_suffix = f" stream_error={stream_error}" if stream_error else ""
    if persisted_count > 0:
        logger.error(
            f"[{transport}] Appeal session sent 0 appeals to client BUT "
            f"server has {persisted_count} ProposedAppeal row(s) for "
            f"denial {denial_id}. This is a delivery/wire failure, not "
            f"a generation failure. status_frames={status_count} "
            f"last_phase={last_status_phase} gen_attempts={denial_attempts}"
            f"{error_suffix}"
        )
    elif persisted_count == 0:
        logger.error(
            f"[{transport}] Appeal session completed with 0 appeals sent "
            f"AND 0 ProposedAppeal rows persisted for denial {denial_id}. "
            f"Generation produced nothing. status_frames={status_count} "
            f"last_phase={last_status_phase} "
            f"gen_attempts={denial_attempts}{error_suffix}"
        )
    else:
        # persisted_count == -1: lookup never ran (no/invalid denial_id)
        # or DB call raised. Don't pretend we know the persisted total.
        logger.error(
            f"[{transport}] Appeal session sent 0 appeals to client; "
            f"persisted-count lookup unavailable for denial {denial_id} "
            f"(coerced={denial_id_int!r}). status_frames={status_count} "
            f"last_phase={last_status_phase} "
            f"gen_attempts={denial_attempts}{error_suffix}"
        )


# Test hook: when truthy, StreamingAppealsBackend accepts the
# connection and closes immediately without yielding any appeal
# payloads. Lets tests exercise the REST fallback path without
# injecting a real transport-level failure. Should never be set in
# production.
SUPPRESS_APPEAL_WS_DELIVERY = False


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
            # Match the streaming protocol shape so the client's
            # type==='error' branch in processResponseChunk fires
            # instead of treating this as an unrecognized frame.
            await self.send(
                json.dumps({"type": "error", "message": "Invalid JSON format"})
            )
            await self.close()
            return
        # Test hook: simulate a silent WS death so the JS client falls
        # back to REST. We close cleanly with no payload so the client
        # sees the same shape as a real broken-pipe / proxy-drop.
        # Double-gate: the module flag alone isn't enough — also
        # require os.environ["TESTING"] == "True" (set only by the
        # TestSync settings class) so a misconfigured production
        # process that somehow toggled the flag still serves real
        # users.
        if SUPPRESS_APPEAL_WS_DELIVERY and os.environ.get("TESTING") == "True":
            logger.warning(
                "WS appeal delivery suppressed by test flag for denial "
                f"{data.get('denial_id')}"
            )
            await self.close()
            return
        logger.debug("Starting generation of appeals...")
        denial_id = data.get("denial_id")
        aitr: AsyncIterator[str] = (
            common_view_logic.AppealsBackendHelper.generate_appeals(data)
        )
        # We do a try/except here to log since the WS framework swallow exceptions sometimes
        appeal_count = 0
        status_count = 0
        last_status_phase: Optional[str] = None
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
                # Count only actual appeal payloads (not status/keepalive frames)
                stripped = record.strip()
                if stripped:
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, dict):
                            if "content" in parsed:
                                appeal_count += 1
                            elif parsed.get("type") == "status":
                                status_count += 1
                                last_status_phase = parsed.get("phase")
                    except (json.JSONDecodeError, TypeError):
                        pass
            if appeal_count == 0:
                # Cross-reference: did the server actually generate appeals
                # for this denial that just didn't make it down the wire?
                # This is the key signal that distinguishes a server-side
                # generation failure from a client-side delivery failure.
                await log_zero_appeal_diagnostics(
                    denial_id=denial_id,
                    status_count=status_count,
                    last_status_phase=last_status_phase,
                    transport="websocket",
                )
            else:
                logger.debug(f"All appeals sent, {appeal_count} payloads total.")
        except Exception as e:
            logger.opt(exception=True).error(
                f"Error sending back appeals for denial {denial_id} after "
                f"{appeal_count} appeals and {status_count} status frames "
                f"(last phase={last_status_phase}): {e}"
            )
            if appeal_count == 0:
                await log_zero_appeal_diagnostics(
                    denial_id=denial_id,
                    status_count=status_count,
                    last_status_phase=last_status_phase,
                    transport="websocket",
                    stream_error=str(e),
                )
            raise
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


class StreamingEscalationBackend(AsyncWebsocketConsumer):
    """Streaming back regulator/executive escalation letters as JSON.

    Same envelope as StreamingAppealsBackend: client sends a single JSON
    blob with {denial_id, email, semi_sekret}; server streams status and
    `letter` payloads, one per recipient.
    """

    async def connect(self):
        logger.debug("Accepting connection for escalation packet")
        await self.accept()

    async def disconnect(self, close_code):
        logger.debug("Disconnecting escalation packet")
        pass

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON received in escalation websocket: {e}")
            await self.send(
                json.dumps({"type": "error", "message": "Invalid JSON format"})
            )
            await self.close()
            return
        aitr: AsyncIterator[str] = (
            common_view_logic.EscalationPacketHelper.generate_escalation_letters(data)
        )
        try:
            await asyncio.sleep(0)
            await self.send("\n")
            async for record in aitr:
                await asyncio.sleep(0)
                await self.send("\n")
                await self.send(record)
                await asyncio.sleep(0)
                await self.send("\n")
        except Exception as e:
            logger.opt(exception=True).error(
                f"Error sending back escalation letters: {e}"
            )
            try:
                await self.send(
                    json.dumps(
                        {
                            "type": "error",
                            "message": "Server error while drafting letters.",
                        }
                    )
                )
            except Exception:
                pass
        finally:
            await asyncio.sleep(0.1)
            try:
                await self.close()
            except Exception:
                logger.debug("Error closing escalation connection")


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
                logger.debug("Preparing to close connection")
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


async def resolve_chat_type(
    user,
    is_authenticated: bool,
    session_key: Optional[str],
    get_professional_user_fn: Callable,
) -> Tuple[ChatType, Optional["ProfessionalUser"]]:
    """Determine chat_type and professional_user entirely server-side.

    For anonymous users, checks ChatLeads to distinguish trial professionals from patients.
    Leads with a non-empty drug field are treated as patients (drug-specific leads).
    For authenticated users, always checks ProfessionalUser regardless of client payload.

    Returns:
        (chat_type, professional_user) tuple.
    """
    if not is_authenticated:
        if session_key:
            lead = (
                await ChatLeads.objects.filter(session_id=session_key)
                .order_by("-created_at")
                .afirst()
            )
            if lead and not lead.drug:
                logger.info(f"Trial professional chat for session {session_key}")
                return ChatType.TRIAL_PROFESSIONAL, None
            # lead with drug or no lead → patient
        return ChatType.PATIENT, None

    # Authenticated user — always check ProfessionalUser
    professional_user = await sync_to_async(get_professional_user_fn)(user)
    if professional_user:
        return ChatType.PROFESSIONAL, professional_user

    logger.info(f"User {user.username} is not a professional user, treating as patient")
    return ChatType.PATIENT, None


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

                            # Guardrails: Only store if non-empty, not null, and not generic/unclear
                            _UNCLEAR_PHRASES = frozenset(
                                {
                                    "not clear",
                                    "unknown",
                                    "unclear",
                                    "n/a",
                                    "none",
                                    "null",
                                    "",
                                    "no denial",
                                    "could not determine",
                                }
                            )

                            def is_clear(val):
                                if not val:
                                    return False
                                return str(val).strip().lower() not in _UNCLEAR_PHRASES

                            updated = False
                            # Cap to the column max_length to avoid DB
                            # DataError when models return overly long text.
                            _MAX_LEN = 2000
                            # Strip boilerplate from denied_item, then
                            # clarity-check the *stripped* result.
                            if denied_item:
                                denied_item = strip_boilerplate_service(
                                    str(denied_item)
                                )
                            if is_clear(denied_item):
                                chat.denied_item = denied_item[:_MAX_LEN]
                                updated = True
                                logger.info(
                                    f"Updated chat {chat_id} with denied item: {denied_item}"
                                )
                            # denied_reason: boilerplate phrases like
                            # "not medically necessary" are valid reasons,
                            # so no stripping — just the clarity check.
                            if is_clear(denied_reason):
                                denied_reason = str(denied_reason).strip()
                            else:
                                denied_reason = None
                            if denied_reason:
                                chat.denied_reason = denied_reason[:_MAX_LEN]
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
        log_meta = {k: v for k, v in data.items() if k not in ("content", "message")}
        content = data.get("message", data.get("content", ""))
        log_meta["content_length"] = len(content) if isinstance(content, str) else 0
        logger.debug(f"Received message for ongoing chat {log_meta}")

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
        # Document upload flags from frontend
        is_document = data.get("is_document", False)
        document_name = data.get("document_name", None)

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

        try:
            # Determine chat_type and professional_user entirely server-side.
            # This must run BEFORE email validation so we use the server-derived is_patient.
            chat_type, professional_user = await resolve_chat_type(
                user=user,
                is_authenticated=is_authenticated,
                session_key=session_key,
                get_professional_user_fn=self._get_professional_user,
            )

            # Use server-derived is_patient for all subsequent logic
            is_patient = chat_type == ChatType.PATIENT

            # For anonymous patients we need the e-mail to allow data deletion since
            # we don't have accounts or lead objects to link to.
            # Authenticated patients already have an email on their account.
            if is_patient:
                if not is_authenticated:
                    if not email:
                        await self.send_json_message(
                            {"error": "Email is required for patient users."}
                        )
                        return
                    try:
                        validate_email(email)
                    except ValidationError:
                        await self.send_json_message({"error": "Invalid email format."})
                        return
                else:
                    # Authenticated patient — derive email from user account
                    email = user.email

            # Extract tracking info from websocket scope (privacy-aware)
            tracking_info = extract_tracking_info_from_scope(
                scope=self.scope, is_professional=(chat_type == ChatType.PROFESSIONAL)
            )
            if str(email or "").strip().lower() == "testing@example.com":
                tracking_info.ip_address = _get_client_ip_from_scope(self.scope)

            chat = await self._get_or_create_chat(
                user,
                professional_user,
                chat_type,
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
                    use_external_models=use_external_models,
                )
            elif use_external_models != self.chat_interface.use_external_models:
                # User changed their preference for external models mid-chat
                self.chat_interface.use_external_models = use_external_models

            logger.debug(f"Chat: {chat.id}")

            if not replay_requested:
                # Allow empty message when linking an appeal or prior auth
                if not message and not iterate_on_appeal and not iterate_on_prior_auth:
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
                    is_document=is_document,
                    document_name=document_name,
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
        chat_type=ChatType.PATIENT,
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
                chat = await OngoingChat.objects.select_related(
                    "user", "professional_user"
                ).aget(id=chat_id)
                # But let's also check session key and user just to be safe.
                if chat.session_key and session_key != chat.session_key:
                    raise OngoingChat.DoesNotExist("Session key mismatch")
                if chat.user_id and (not user or chat.user_id != user.pk):
                    raise OngoingChat.DoesNotExist("User mismatch")

                # Reconcile resolved identity with stored values so
                # ChatInterface always sees up-to-date audience flags.
                is_patient = chat_type == ChatType.PATIENT
                updates = {}
                if chat.chat_type != chat_type:
                    updates["chat_type"] = chat_type
                if chat.is_patient != is_patient:
                    updates["is_patient"] = is_patient
                if (
                    professional_user
                    and chat.professional_user_id != professional_user.pk
                ):
                    updates["professional_user"] = professional_user
                if user and user.is_authenticated and chat.user_id != user.pk:
                    updates["user"] = user
                if microsite_slug and not chat.microsite_slug:
                    updates["microsite_slug"] = microsite_slug

                if updates:
                    await OngoingChat.objects.filter(id=chat.id).aupdate(**updates)
                    await chat.arefresh_from_db()

                return chat
            except OngoingChat.DoesNotExist as e:
                logger.warning(
                    f"Chat with id {chat_id} not found. Creating new chat.", e
                )
                pass  # Fall through to create a new one

        # Create a new chat
        is_patient = chat_type == ChatType.PATIENT
        tracking_kwargs = tracking_info.to_model_kwargs() if tracking_info else {}

        # Hash email for patient users (privacy-preserving lookup for data deletion)
        hashed_email = None
        if is_patient and email:
            hashed_email = Denial.get_hashed_email(email)

        chat_user = user if (user and user.is_authenticated) else None

        logger.info(
            f"Creating new {chat_type} chat"
            f"{' for session ' + session_key[:8] if session_key else ''}"
            f"{' for user ' + str(chat_user.id) if chat_user else ''}"
        )

        return await OngoingChat.objects.acreate(
            user=chat_user,
            professional_user=professional_user,
            session_key=session_key,
            chat_type=chat_type,
            is_patient=is_patient,
            chat_history=[],
            summary_for_next_call=[],
            hashed_email=hashed_email,
            microsite_slug=microsite_slug,
            **tracking_kwargs,
        )

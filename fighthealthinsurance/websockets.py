import asyncio
import json
import os
import re
import uuid
from typing import AsyncIterator, Callable, Optional, Tuple, cast

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

# channels' database_sync_to_async (NOT asgiref's sync_to_async): consumers run
# outside the HTTP request cycle, so only its close_old_connections wrapping
# ever closes the DB connections these calls open. aclose_old_connections is
# called at receive() entry via _aclose_if_socket_was_idle: native async ORM
# (aget/afirst/...) binds connections to this consumer's long-lived thread, and
# the server's idle_session_timeout kills them while a socket sits idle --
# closing first makes the next ORM call reconnect instead of raising
# OperationalError.
from channels.db import aclose_old_connections, database_sync_to_async
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

# Only sweep connections after a real idle gap. idle_session_timeout is 30min,
# so anything over a minute of socket silence earns a sweep; on an active
# socket the sweep is pure churn -- close_if_unusable_or_obsolete reconnects
# initialized-but-closed wrappers just to read autocommit, so a per-message
# sweep opens and closes a connection per frame, which hammers the shared
# in-memory test database (lock-contention flakes) for zero benefit.
_IDLE_SWEEP_THRESHOLD_SECONDS = 60.0


async def _aclose_if_socket_was_idle(consumer: AsyncWebsocketConsumer) -> None:
    """Drop idle-killed DB connections, but only after a real idle gap.

    The first message after connect skips the sweep: the consumer's thread is
    fresh then, and the incident this guards against (Sentry: "terminating
    connection due to idle-session timeout") comes from sockets going quiet
    MID-conversation. From the second message on, any gap over the threshold
    sweeps before touching the ORM.
    """
    loop = asyncio.get_running_loop()
    now = loop.time()
    last = getattr(consumer, "_last_receive_monotonic", None)
    consumer._last_receive_monotonic = now  # type: ignore[attr-defined]
    if last is not None and (now - last) >= _IDLE_SWEEP_THRESHOLD_SECONDS:
        await aclose_old_connections()


async def enqueue_denied_items_analysis(*, chat_id: str) -> None:
    """Dispatch denied-items analysis as fire-and-forget background work.

    No dedup/job-state layer on purpose: ``_analyze_denied_items`` is already
    idempotent (it no-ops when the chat already has a denied item/reason), and
    Prod's per-process LocMemCache made any cross-process job-state tracking
    write-only anyway. The disconnect handler just hands the chat id to the
    detached Ray actor.
    """
    from fighthealthinsurance.denied_items_analysis_actor_ref import (
        denied_items_analysis_actor_ref,
    )

    denied_items_analysis_actor_ref.get.run_analysis.remote(chat_id=chat_id)


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


async def _parse_json_or_close(
    consumer: AsyncWebsocketConsumer,
    text_data: Optional[str],
    *,
    consumer_name: str,
    streaming_protocol: bool = False,
) -> Optional[dict]:
    """Parse a single WebSocket text frame as JSON, closing on bad input.

    Returns the decoded dict on success, or ``None`` when the frame was
    malformed (in which case an error frame has already been sent and the
    socket closed). ``streaming_protocol=True`` selects the
    ``{"type": "error", "message": ...}`` envelope expected by the
    appeals/escalation streaming clients and appends a trailing newline
    so the client's line-buffered parser flushes the frame before the
    socket close arrives; otherwise the simpler ``{"error": ...}`` shape
    used by entity/prior-auth/chat consumers (which read whole frames).

    Non-object JSON (``[]``, ``"x"``, ``123``, ``null``) is rejected so
    callers can ``.get()`` on the result without an AttributeError.
    """

    async def _send_err(message: str) -> None:
        if streaming_protocol:
            # Trailing newline: appeal_fetcher.ts buffers ws.onmessage
            # data and only parses lines terminated by "\n". Without it
            # the close arrives before the error frame is parsed and
            # the client falls through to its generic error path.
            await consumer.send(
                json.dumps({"type": "error", "message": message}) + "\n"
            )
        else:
            await consumer.send(json.dumps({"error": message}))

    if text_data is None:
        logger.warning(f"Empty/None text frame received in {consumer_name} websocket")
        await _send_err("Empty message")
        await consumer.close()
        return None
    try:
        data = json.loads(text_data)
    except json.JSONDecodeError as e:
        logger.warning(f"Invalid JSON received in {consumer_name} websocket: {e}")
        await _send_err("Invalid JSON format")
        await consumer.close()
        return None
    if not isinstance(data, dict):
        logger.warning(
            f"Non-object JSON received in {consumer_name} websocket: "
            f"{type(data).__name__}"
        )
        await _send_err("Expected a JSON object")
        await consumer.close()
        return None
    return data


# Test hook: when truthy, StreamingAppealsBackend accepts the
# connection and closes immediately without yielding any appeal
# payloads. Lets tests exercise the REST fallback path without
# injecting a real transport-level failure. Should never be set in
# production.
SUPPRESS_APPEAL_WS_DELIVERY = False


class StreamingAppealsBackend(AsyncWebsocketConsumer):
    """Streaming back the appeals as json :D"""

    async def connect(self):
        logger.debug("appeals ws: connect")
        await self.accept()

    async def disconnect(self, close_code):
        logger.debug(f"appeals ws: disconnect code={close_code}")

    async def receive(self, text_data):
        # Drop connections the server's idle-session timeout may have
        # killed while this socket sat idle (see channels.db import note).
        await _aclose_if_socket_was_idle(self)
        # streaming_protocol=True so the client's type==='error' branch
        # in processResponseChunk fires instead of treating this as an
        # unrecognized frame. The helper also rejects non-object JSON
        # so .get(...) below can't raise AttributeError on `[]` /
        # `"x"` / `123`.
        data = await _parse_json_or_close(
            self,
            text_data,
            consumer_name="appeals",
            streaming_protocol=True,
        )
        if data is None:
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
        denial_id = data.get("denial_id")
        logger.debug(f"appeals ws: starting generation for denial {denial_id}")
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
                logger.debug(
                    f"appeals ws: sent {appeal_count} payloads for denial {denial_id}"
                )
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
            await asyncio.sleep(0.1)
            try:
                await self.close()
            except Exception:
                logger.debug("appeals ws: error closing connection")


class StreamingEscalationBackend(AsyncWebsocketConsumer):
    """Streaming back regulator/executive escalation letters as JSON.

    Same envelope as StreamingAppealsBackend: client sends a single JSON
    blob with {denial_id, email, semi_sekret}; server streams status and
    `letter` payloads, one per recipient.
    """

    async def connect(self):
        logger.debug("escalation ws: connect")
        await self.accept()

    async def disconnect(self, close_code):
        logger.debug(f"escalation ws: disconnect code={close_code}")

    async def receive(self, text_data):
        # Drop connections the server's idle-session timeout may have
        # killed while this socket sat idle (see channels.db import note).
        await _aclose_if_socket_was_idle(self)
        data = await _parse_json_or_close(
            self,
            text_data,
            consumer_name="escalation",
            streaming_protocol=True,
        )
        if data is None:
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
        logger.debug("entity ws: connect")
        await self.accept()

    async def disconnect(self, close_code):
        logger.debug(f"entity ws: disconnect code={close_code}")

    async def receive(self, text_data):
        # Drop connections the server's idle-session timeout may have
        # killed while this socket sat idle (see channels.db import note).
        await _aclose_if_socket_was_idle(self)
        data = await _parse_json_or_close(
            self,
            text_data,
            consumer_name="entity extraction",
        )
        if data is None:
            return
        if "denial_id" not in data:
            logger.warning("Missing denial_id in entity extraction request")
            await self.send(json.dumps({"error": "Missing denial_id"}))
            await self.close()
            return
        aitr = common_view_logic.DenialCreatorHelper.extract_entity(data["denial_id"])

        try:
            async for record in aitr:
                await self.send(record)
                await asyncio.sleep(0)
                await self.send("\n")
            await asyncio.sleep(1)
        except Exception as e:
            logger.opt(exception=True).debug(f"Error sending back entity: {e}")
            raise e
        finally:
            await asyncio.sleep(0.1)
            try:
                await self.close()
            except Exception:
                logger.debug("entity ws: error closing connection")


class PriorAuthConsumer(AsyncWebsocketConsumer):
    """Streaming back the proposed prior authorizations as JSON."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pag = prior_auth_generator
        self.groups = []

    async def connect(self):
        logger.debug("prior-auth ws: connect")
        await self.accept()

    async def disconnect(self, close_code):
        logger.debug(f"prior-auth ws: disconnect code={close_code}")

    async def receive(self, text_data):
        # Drop connections the server's idle-session timeout may have
        # killed while this socket sat idle (see channels.db import note).
        await _aclose_if_socket_was_idle(self)
        data = await _parse_json_or_close(
            self,
            text_data,
            consumer_name="prior auth",
        )
        if data is None:
            return
        logger.debug("prior-auth ws: starting generation")

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
                        logger.debug(
                            f"prior-auth ws: sending proposal {proposal['proposed_id']}"
                        )
                        await self.send(json.dumps(proposal))
                    elif "error" in proposal:
                        logger.error(f"Error in proposal: {proposal['error']}")
                    else:
                        logger.debug(f"prior-auth ws: malformed proposal {proposal}")
                    await asyncio.sleep(0)
            except Exception as e:
                logger.opt(exception=True).debug(
                    f"Error sending back prior auth proposals: {e}"
                )
                raise e
            finally:
                await asyncio.sleep(1)
                await self.close()
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
                logger.debug(f"Trial professional chat for session {session_key}")
                return ChatType.TRIAL_PROFESSIONAL, None
            # lead with drug or no lead → patient
        return ChatType.PATIENT, None

    # Authenticated user — always check ProfessionalUser
    professional_user = await database_sync_to_async(get_professional_user_fn)(user)
    if professional_user:
        return ChatType.PROFESSIONAL, professional_user

    logger.debug(
        f"User {user.username} is not a professional user, treating as patient"
    )
    return ChatType.PATIENT, None


class OngoingChatConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for ongoing chat with LLMs for both pro users and patients."""

    chat_interface: Optional[ChatInterface] = None
    chat_id: Optional[str] = None

    async def connect(self):
        logger.debug("chat ws: connect")
        await self.accept()

    async def send_json_message(self, data: dict):
        """Helper to send JSON data to the client."""
        await self.send(text_data=json.dumps(data))

    async def disconnect(self, close_code):
        logger.debug(f"chat ws: disconnect code={close_code}")
        # If a chat was active, enqueue denied-item analysis asynchronously.
        # Disconnect stays non-blocking; analysis is eventually consistent.
        if self.chat_interface and self.chat_id:
            try:
                await enqueue_denied_items_analysis(chat_id=self.chat_id)
            except Exception as e:
                logger.opt(exception=True).warning(
                    f"Failed to enqueue denied-item analysis for chat {self.chat_id}: {e}"
                )

    @classmethod
    async def run_denied_items_analysis(cls, *, chat_id: str) -> None:
        """Actor entry point: run the (idempotent) denied-items analysis.

        Instantiated bare because ``_analyze_denied_items`` only reads/writes
        the DB — it doesn't use websocket scope or connection state. Errors are
        logged inside ``_analyze_denied_items``; nothing here needs to track
        job status (see ``enqueue_denied_items_analysis``).
        """
        await cls()._analyze_denied_items(chat_id)

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
                logger.debug(
                    f"Prompt for denied item extraction (chat {chat_id}):\n{full_prompt}"
                )

                # Get the analysis from the model
                response_text, _ = await model.generate_chat_response(
                    full_prompt,
                    is_professional=await database_sync_to_async(
                        chat.is_professional_user
                    )(),
                    is_logged_in=await database_sync_to_async(chat.is_logged_in_user)(),
                )

                if response_text:
                    # Extract JSON from response
                    json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
                    if json_match:
                        try:
                            analysis_data = json.loads(json_match.group(0))
                            denied_item = analysis_data.get("denied_item")
                            denied_reason = analysis_data.get("denied_reason")

                            logger.debug(
                                f"Analysis for chat {chat_id}: denied_item={denied_item!r} denied_reason={denied_reason!r} raw={analysis_data!r}"
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
                            if updated:
                                await database_sync_to_async(
                                    self._persist_denied_items_transactional
                                )(
                                    chat.id,
                                    denied_item[:_MAX_LEN] if denied_item else None,
                                    denied_reason[:_MAX_LEN] if denied_reason else None,
                                )
                                logger.info(
                                    f"Chat {chat_id}: stored denied item/reason "
                                    f"(item_len={len(denied_item) if denied_item else 0}, "
                                    f"reason_len={len(denied_reason) if denied_reason else 0})"
                                )
                            else:
                                logger.info(
                                    f"Chat {chat_id}: skipped storing denied item/reason (unclear or missing)"
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

    @staticmethod
    def _persist_denied_items_transactional(
        chat_id: uuid.UUID, denied_item: Optional[str], denied_reason: Optional[str]
    ) -> None:
        with transaction.atomic():
            chat = OngoingChat.objects.select_for_update().get(id=chat_id)
            if denied_item:
                chat.denied_item = denied_item
            if denied_reason:
                chat.denied_reason = denied_reason
            chat.save(update_fields=["denied_item", "denied_reason"])

    async def receive(self, text_data):
        # Drop connections the server's idle-session timeout may have
        # killed while this socket sat idle (see channels.db import note).
        await _aclose_if_socket_was_idle(self)
        data = await _parse_json_or_close(
            self,
            text_data,
            consumer_name="ongoing chat",
        )
        if data is None:
            return
        # Get the required data -- note the message should be sent as "content"
        # but we also accept "message" for backward compatibility. Coerce
        # any None/missing/non-string value to "" so the downstream
        # detect_crisis_keywords/detect_delete_data_request calls (which
        # do an unguarded regex .search on the message) can't crash on
        # an iterate-only request.
        message_raw = data.get("message", data.get("content", None))
        message: str = message_raw if isinstance(message_raw, str) else ""
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
            f"chat ws: msg_len={len(message) if isinstance(message, str) else 0} "
            f"replay={replay_requested} chat_id={chat_id} "
            f"iterate_on_appeal={iterate_on_appeal} iterate_on_prior_auth={iterate_on_prior_auth} "
            f"is_patient={is_patient} session_key={session_key} microsite_slug={microsite_slug} "
            f"use_external_models={use_external_models}"
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

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from django.utils import timezone

from asgiref.sync import sync_to_async
from loguru import logger

from fhi_users.models import User
from fighthealthinsurance import settings
from fighthealthinsurance.chat.context_manager import (
    MISSING_CONTEXT_PREFIX,
    background_generate_summary,
    make_placeholder,
    prepare_history_for_llm,
    should_store_summary,
)
from fighthealthinsurance.chat.llm_client import build_llm_calls, create_response_scorer
from fighthealthinsurance.chat.retry_handler import (
    retry_llm_with_fallback,
    should_retry_response,
)
from fighthealthinsurance.chat.safety_filters import (
    CRISIS_RESOURCES,
    detect_crisis_keywords,
)
from fighthealthinsurance.chat.tools import (
    AppealTool,
    DocFetcherTool,
    MedicaidEligibilityTool,
    MedicaidInfoTool,
    PriorAuthTool,
    PubMedTool,
)
from fighthealthinsurance.extralink_context_helper import ExtraLinkContextHelper
from fighthealthinsurance.rag_client import get_rag_context_for_denial
from fighthealthinsurance.ml.ml_models import (
    RemoteModelLike,
    remove_repeated_blocks,
    remove_repeated_sentences,
)
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import (
    Appeal,
    ChatType,
    OngoingChat,
    PriorAuthRequest,
)
from fighthealthinsurance.microsites import get_microsite
from fighthealthinsurance.prompt_templates import get_intro_template
from fighthealthinsurance.pubmed_tools import PubMedTools
from fighthealthinsurance.utils import (
    best_within_timelimit,
    fire_and_forget_in_new_threadpool,
)


class ChatInterface:
    def __init__(
        self,
        send_json_message_func: Callable[[Dict[str, Any]], Awaitable[None]],
        chat: OngoingChat,
        user: User,
        use_external_models: bool = False,
    ):
        def wrap_send_json_message_func(message: Dict[str, Any]) -> Awaitable[None]:
            """Wraps the send_json_message_func to ensure it's always awaited."""
            if "chat_id" not in message:
                message["chat_id"] = str(chat.id)
            return send_json_message_func(message)

        self.send_json_message_func = wrap_send_json_message_func
        self.pubmed_tools = PubMedTools()
        self.chat: OngoingChat = chat
        self.user: User = user
        self.use_external_models: bool = use_external_models
        self._doc_fetch_count: list[int] = [0]

    @staticmethod
    def _append_to_history(chat, role: str, content: str):
        """Append a message to chat history with a timestamp."""
        if not chat.chat_history:
            chat.chat_history = []
        chat.chat_history.append(
            {
                "role": role,
                "content": content,
                "timestamp": timezone.now().isoformat(),
            }
        )

    @property
    def is_patient(self) -> bool:
        return self.chat.chat_type == ChatType.PATIENT

    @property
    def is_professional(self) -> bool:
        return self.chat.chat_type == ChatType.PROFESSIONAL

    @property
    def is_trial_professional(self) -> bool:
        return self.chat.chat_type == ChatType.TRIAL_PROFESSIONAL

    async def send_error_message(self, message: str):
        """Sends an error message to the client."""
        await self.send_json_message_func(
            {"error": message, "chat_id": str(self.chat.id)}
        )

    async def send_status_message(self, message: str):
        """Sends a status message to the client."""
        logger.debug(f"Updating status message.")
        await self.send_json_message_func(
            {"status": message, "chat_id": str(self.chat.id)}
        )

    async def send_message_to_client(self, message: str):
        """Sends a message to the client."""
        await self.send_json_message_func(
            {"content": message, "chat_id": str(self.chat.id), "role": "assistant"}
        )

    async def _get_user_info(self) -> str:
        """Generates a descriptive string for the user (either professional or patient)."""
        try:
            return await sync_to_async(self.chat.summarize_user)()
        except Exception as e:
            logger.warning(f"Could not generate detailed user info: {e}")
            return "a user"

    async def _merge_summary_from_db(self, chat: OngoingChat) -> None:
        """
        Merge the latest summary_for_next_call from the DB into the in-memory chat object.

        This handles race conditions where a background task (like fetch_microsite_context)
        may have updated summary_for_next_call via a fresh DB query, but the main flow's
        in-memory chat object is stale. We reload from DB and merge any microsite context
        that exists in the DB but not in the in-memory object.
        """
        try:
            db_chat = await OngoingChat.objects.aget(id=chat.id)
            db_summary: Optional[List[str]] = db_chat.summary_for_next_call

            if not db_summary:
                return

            # 1. Always check if a background summary task has replaced a
            #    placeholder in the DB — this must run for ALL chats (not just
            #    microsite ones) so that the in-memory WebSocket object picks up
            #    summaries generated by background_generate_summary().
            microsite_marker = "Microsite context:"
            if not chat.summary_for_next_call:
                chat.summary_for_next_call = []
            in_memory_summary = chat.summary_for_next_call
            for i, entry in enumerate(in_memory_summary):
                entry_str = str(entry)
                if MISSING_CONTEXT_PREFIX in entry_str:
                    # Check if the DB has a real value at this position
                    if i < len(db_summary) and MISSING_CONTEXT_PREFIX not in str(
                        db_summary[i]
                    ):
                        # If the in-memory entry was corrupted with appended
                        # microsite context (from older code paths), preserve
                        # that microsite context by re-appending it.
                        microsite_text = None
                        ms_pos = entry_str.find(microsite_marker)
                        if ms_pos >= 0:
                            microsite_text = entry_str[ms_pos:]
                        new_val = db_summary[i]
                        if microsite_text and microsite_marker not in str(
                            new_val or ""
                        ):
                            new_val = f"{new_val}\n\n{microsite_text}"
                        chat.summary_for_next_call[i] = new_val
                        logger.debug(
                            f"Merged background summary from DB into chat {chat.id}"
                        )

            # 2. Merge microsite context from DB into in-memory chat if missing
            db_has_microsite = any(
                microsite_marker in (str(entry) if entry else "")
                for entry in db_summary
            )

            if not db_has_microsite:
                return

            # Check if in-memory chat already has this microsite context
            in_memory_summary = chat.summary_for_next_call or []
            in_memory_has_microsite = any(
                microsite_marker in (str(entry) if entry else "")
                for entry in in_memory_summary
            )

            if in_memory_has_microsite:
                return

            # Find the DB entry with microsite context and add it as its own
            # entry so we don't corrupt any placeholder strings.
            for db_entry in db_summary:
                db_entry_str = str(db_entry) if db_entry else ""
                if db_entry_str and microsite_marker in db_entry_str:
                    # Extract just the microsite portion
                    microsite_start = db_entry_str.find(microsite_marker)
                    microsite_text = db_entry_str[microsite_start:]

                    if not chat.summary_for_next_call:
                        chat.summary_for_next_call = []
                    # Append as a separate entry to avoid corrupting placeholders
                    chat.summary_for_next_call.append(microsite_text)
                    logger.debug(
                        f"Merged microsite context from DB into chat {chat.id}"
                    )
                    break
        except OngoingChat.DoesNotExist:
            logger.warning(f"Chat {chat.id} not found in DB during summary merge")
        except Exception as e:
            logger.warning(f"Error merging summary from DB for chat {chat.id}: {e}")

    async def _call_llm_with_actions(
        self,
        model_backends: List[RemoteModelLike],
        current_message_for_llm: str,
        previous_context_summary: Optional[str],
        history_for_llm: List[Dict[str, str]],
        depth: int = 0,
        is_logged_in: bool = True,
        is_professional: bool = True,
        fallback_backends: Optional[List[RemoteModelLike]] = None,
        full_history: Optional[List[Dict[str, str]]] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Calls the LLM, handles PubMed query requests if present and returns the response.
        Also processes special tokens for creating or updating Appeals and PriorAuthRequests.

        Args:
            model_backends: List of model backends to try
            current_message_for_llm: The current message to send
            previous_context_summary: Summary of previous context
            history_for_llm: Truncated history (last 20 messages)
            depth: Recursion depth for tool handling
            is_logged_in: Whether user is logged in
            is_professional: Whether user is a professional
            fallback_backends: Backup models to try if primary fails
            full_history: Full untruncated history (optional) - will also try with this
                         if provided and model context allows it
        """
        if depth > 3:
            return None, None
        chat = self.chat
        history = history_for_llm

        # Build LLM calls using the extracted module
        calls, call_scores = build_llm_calls(
            model_backends=model_backends,
            current_message=current_message_for_llm,
            previous_context_summary=previous_context_summary,
            history=history,
            is_professional=is_professional,
            is_logged_in=is_logged_in,
            full_history=full_history,
        )

        # Create scoring function using the extracted module
        score_fn = create_response_scorer(
            call_scores,
            primary_calls=calls,
            chat_history=chat.chat_history,
            current_message=current_message_for_llm,
        )

        try:
            response_text, context_part = await best_within_timelimit(
                calls,
                score_fn,
                timeout=30.0,
            )
        except Exception as e:
            logger.warning(f"Primary models all failed: {e}")
            response_text = None
            context_part = None

        response_text = response_text or ""

        # If primary models failed, use retry handler with fallback
        if should_retry_response(response_text):
            retry_response, retry_context = await retry_llm_with_fallback(
                model_backends=model_backends,
                current_message=current_message_for_llm,
                previous_context_summary=previous_context_summary,
                history=history,
                is_professional=is_professional,
                is_logged_in=is_logged_in,
                fallback_backends=fallback_backends,
                timeout=35.0,
                status_callback=self.send_status_message,
                chat_history=chat.chat_history,
            )
            if retry_response and len(retry_response.strip()) > 5:
                response_text = retry_response
                context_part = retry_context

        logger.debug(f"Using best result {response_text:.20}...")

        if not response_text:
            logger.debug("Got empty response from LLM")
            return None, None

        # Process tool calls via modular tool handlers
        context = context_part or ""

        # Shared kwargs for recursive tools (medicaid, pubmed, doc fetcher)
        tool_kwargs = dict(
            model_backends=model_backends,
            current_message_for_llm=current_message_for_llm,
            previous_context_summary=previous_context_summary,
            history_for_llm=history_for_llm,
            depth=depth,
            is_logged_in=is_logged_in,
            is_professional=is_professional,
        )

        # Non-recursive tools: appeal, prior auth
        appeal_tool = AppealTool(self.send_status_message, self.send_error_message)
        response_text, context, _ = await appeal_tool.handle(
            response_text, context, chat=chat
        )

        prior_auth_tool = PriorAuthTool(
            self.send_status_message, self.send_error_message
        )
        response_text, context, _ = await prior_auth_tool.handle(
            response_text, context, chat=chat
        )

        # Recursive tools: medicaid eligibility, medicaid info, pubmed, doc fetcher
        medicaid_elig_tool = MedicaidEligibilityTool(
            self.send_status_message, self._call_llm_with_actions
        )
        response_text, context, _ = await medicaid_elig_tool.handle(
            response_text, context, **tool_kwargs
        )

        medicaid_info_tool = MedicaidInfoTool(
            self.send_status_message, self._call_llm_with_actions
        )
        response_text, context, _ = await medicaid_info_tool.handle(
            response_text, context, **tool_kwargs
        )

        pubmed_tool = PubMedTool(
            self.send_status_message,
            pubmed_tools=self.pubmed_tools,
            call_llm_callback=self._call_llm_with_actions,
        )
        response_text, context, _ = await pubmed_tool.handle(
            response_text, context, **tool_kwargs
        )

        doc_fetcher_tool = DocFetcherTool(
            self.send_status_message,
            call_llm_callback=self._call_llm_with_actions,
            fetch_count=self._doc_fetch_count,
        )
        response_text, context, _ = await doc_fetcher_tool.handle(
            response_text, context, **tool_kwargs
        )

        logger.debug(f"Return with context {context}.")
        return response_text, context

    async def handle_chat_message(
        self,
        user_message: str,
        iterate_on_appeal: Optional[str] = None,
        iterate_on_prior_auth: Optional[str] = None,
        user: Optional[User] = None,
    ):
        """
        Handles an incoming chat message, interacts with LLMs, and manages chat history.
        """
        chat = self.chat

        # SAFETY: Check for crisis/self-harm indicators first
        # If detected, provide crisis resources immediately alongside any response
        crisis_detected = detect_crisis_keywords(user_message)
        if crisis_detected:
            logger.warning(
                f"Crisis keywords detected in chat {chat.id}, providing resources"
            )
            # Send crisis resources as a status/system message first
            # This ensures the user sees help resources immediately
            crisis_response = f"I noticed you might be going through a difficult time. Before we continue, I want to make sure you have access to support:\n\n{CRISIS_RESOURCES}\n\nI'm here to help with health insurance questions, but please reach out to these resources if you need immediate support."
            await self.send_message_to_client(crisis_response)
            # Log the crisis detection for monitoring
            logger.info(f"Crisis resources provided for chat {chat.id}")
            # Add to chat history
            self._append_to_history(chat, "user", user_message)
            self._append_to_history(chat, "assistant", crisis_response)
            await chat.asave()
            # Don't continue with normal processing - let the user respond
            return

        # Check if this is a new chat BEFORE any linking modifies chat_history
        is_new_chat = not bool(chat.chat_history)

        # Load microsite context on first interaction (before linking may return early)
        if is_new_chat and chat.microsite_slug:
            try:
                microsite = get_microsite(chat.microsite_slug)
                if microsite:

                    async def fetch_microsite_context():
                        """Fetch microsite context including RAG evidence."""
                        try:
                            # Send status message if PubMed search is available
                            if microsite.pubmed_search_terms:
                                safe_procedure = str(microsite.default_procedure)[:100]
                                await self.send_status_message(
                                    f"Searching medical literature for {safe_procedure}..."
                                )

                            # Fetch combined context (extralinks + PubMed) and RAG in parallel
                            combined_awaitable = microsite.get_combined_context(
                                pubmed_tools=(
                                    self.pubmed_tools
                                    if microsite.pubmed_search_terms
                                    else None
                                ),
                                max_extralink_docs=5,
                                max_extralink_chars=2000,
                                max_pubmed_terms=3,
                                max_pubmed_articles=20,
                            )

                            # Build a denial-like query from microsite info
                            rag_query_parts = [microsite.default_procedure]
                            if microsite.default_condition:
                                rag_query_parts.append(microsite.default_condition)
                            if microsite.common_denial_reasons:
                                rag_query_parts.append(
                                    microsite.common_denial_reasons[0]
                                )
                            rag_query = " - ".join(rag_query_parts)

                            # Try to get state from a linked denial if available
                            rag_state = None
                            if await chat.appeals.aexists():
                                appeal = await chat.appeals.afirst()
                                if appeal:
                                    linked_denial = await sync_to_async(
                                        lambda x: x.denial
                                    )(appeal)
                                    if linked_denial:
                                        rag_state = linked_denial.state

                            rag_awaitable = get_rag_context_for_denial(
                                denial_text=rag_query,
                                state=rag_state,
                            )

                            results = await asyncio.gather(
                                combined_awaitable,
                                rag_awaitable,
                                return_exceptions=True,
                            )

                            combined_context = (
                                results[0] if isinstance(results[0], str) else None
                            )
                            rag_context = (
                                results[1] if isinstance(results[1], str) else None
                            )

                            all_context_parts = []
                            if combined_context:
                                all_context_parts.append(combined_context)
                            if rag_context:
                                all_context_parts.append(
                                    f"Medical guidelines and regulations:\n{rag_context}"
                                )
                                logger.info(
                                    f"RAG context added to chat for microsite {microsite.slug}"
                                )

                            if all_context_parts:
                                full_context = "\n\n".join(all_context_parts)
                                microsite_entry = f"Microsite context:\n{full_context}"
                                # Store microsite context in chat summary
                                chat_obj = await OngoingChat.objects.aget(id=chat.id)
                                if not chat_obj.summary_for_next_call:
                                    chat_obj.summary_for_next_call = []

                                # Don't append to an entry that is a placeholder
                                # — that would corrupt the placeholder tag and
                                # prevent background_generate_summary from
                                # matching it.  Store microsite context as its
                                # own entry instead.
                                last_entry = (
                                    str(chat_obj.summary_for_next_call[-1])
                                    if chat_obj.summary_for_next_call
                                    else ""
                                )
                                if (
                                    not chat_obj.summary_for_next_call
                                    or MISSING_CONTEXT_PREFIX in last_entry
                                ):
                                    chat_obj.summary_for_next_call.append(
                                        microsite_entry
                                    )
                                else:
                                    # Safe to append to the last entry
                                    chat_obj.summary_for_next_call[-1] = (
                                        f"{last_entry}\n\n{microsite_entry}"
                                        if last_entry
                                        else microsite_entry
                                    )
                                await chat_obj.asave()

                                logger.info(
                                    f"Stored microsite context for {microsite.slug} in chat"
                                )

                            # Send completion message if PubMed search was performed
                            if microsite.pubmed_search_terms:
                                await self.send_status_message(
                                    "Medical literature search complete"
                                )
                        except Exception as e:
                            logger.opt(exception=True).warning(
                                f"Error loading microsite context: {e}"
                            )

                    await fire_and_forget_in_new_threadpool(fetch_microsite_context())
                else:
                    logger.warning(
                        f"Could not find microsite for slug {chat.microsite_slug}"
                    )
            except Exception as e:
                logger.warning(f"Error loading microsite for chat {chat.id}: {e}")

        # Handle chat ↔ appeal/prior auth linking if requested

        link_message = None
        user_facing_message = None
        # Note: We intentionally do NOT send the link message to the LLM/model immediately.
        # This allows the user to drive the next step, and avoids confusing the model with system state changes.
        # Also we require their is a pro user to enable linking.
        if iterate_on_appeal and user:
            await self.send_status_message("Linking appeal into chat")
            appeal = await sync_to_async(Appeal.get_optional_for_user)(
                user, id=iterate_on_appeal
            )
            if not appeal:
                await self.send_error_message(
                    "Appeal not found, or you do not have permission to access it."
                )
                return
            appeal_details = await sync_to_async(appeal.details)()
            if appeal.chat_id != chat.id:
                appeal.chat = chat
                await appeal.asave()
                link_message = f"Linked this chat to Appeal #{appeal.id} -- help the user iterate on {appeal_details}"
                user_facing_message = "I've linked this chat to your appeal. How can I help you iterate on it?"
            else:
                link_message = f"This chat is already linked to Appeal #{appeal.id} -- the current appeal text is {appeal_details}, help the user iterate on it"
                user_facing_message = "This chat is already linked to your appeal. How can I help you with it?"
        if iterate_on_prior_auth and user:
            await self.send_status_message(
                "Linking prior authorization request into chat"
            )
            prior_auth = await sync_to_async(PriorAuthRequest.get_optional_for_user)(
                user, id=iterate_on_prior_auth
            )
            if not prior_auth:
                await self.send_error_message(
                    "Prior Auth Request not found or you do not have permission to access it."
                )
                return
            prior_auth_details = await sync_to_async(prior_auth.details)()
            if prior_auth.chat_id != chat.id:
                prior_auth.chat = chat
                await prior_auth.asave()
                link_message = f"Linked this chat to Prior Auth Request #{prior_auth.id}, details are {prior_auth_details}"
                user_facing_message = "I've linked this chat to your prior authorization request. How can I help you with it?"
            else:
                link_message = f"This chat is already linked to Prior Auth Request #{prior_auth.id}, current details are {prior_auth_details}"
                user_facing_message = "This chat is already linked to your prior authorization request. How can I help you with it?"
        if link_message and user_facing_message:
            await asyncio.sleep(0.01)
            self._append_to_history(chat, "user", link_message)
            self._append_to_history(chat, "assistant", user_facing_message)
            # Merge any microsite context from DB before saving to avoid overwriting
            # background task updates
            await self._merge_summary_from_db(chat)
            await asyncio.gather(
                chat.asave(), self.send_message_to_client(user_facing_message)
            )
            return

        # Get primary and fallback models based on user preference
        primary_models, fallback_models = ml_router.get_chat_backends_with_fallback(
            use_external=self.use_external_models
        )
        if not primary_models:
            await self.send_error_message(
                "Sorry, no language models are currently available."
            )
            return

        # Merge any DB updates (background summaries, microsite context) before
        # reading summary_for_next_call so they are available for this LLM call.
        await self._merge_summary_from_db(chat)

        current_llm_context = None
        if chat.summary_for_next_call and len(chat.summary_for_next_call) > 0:
            current_llm_context = chat.summary_for_next_call[-1]
            # Include the previous context summary so the model has continuity
            # across summarization boundaries (e.g. microsite context, PubMed).
            if len(chat.summary_for_next_call) > 1:
                prev_summary = chat.summary_for_next_call[-2]
                if prev_summary and str(prev_summary).strip():
                    current_llm_context = (
                        f"Previous context summary:\n{prev_summary}\n\n"
                        f"Most recent context summary:\n{current_llm_context}"
                    )

        llm_input_message = user_message

        if is_new_chat:
            # If this is a trial professional user, add a banner message
            if self.is_trial_professional:
                trial_banner = {
                    "role": "system",
                    "content": "⚠️ You're using a free trial version. Responses may be slower, and features like linked appeals and prior auths require a full professional account.\n\nWant full access? [Create a free account →](/signup)",
                    "timestamp": timezone.now().isoformat(),
                }

                # We don't add the trial banner to the history since it needs to go user -> agent -> user.

                # Send the trial banner to the client
                await self.send_json_message_func(trial_banner)

            user_info_str = await self._get_user_info()
            template = get_intro_template(self.chat.chat_type)
            llm_input_message = template.format(
                user_info=user_info_str, message=user_message
            )

        # Prepare history for LLM using the context manager
        # This handles truncation, summarization, and full history preservation
        (
            history_for_llm,
            full_history_for_llm,
            summarized_context,
        ) = await prepare_history_for_llm(
            chat_history=chat.chat_history,
            existing_summary=current_llm_context,
            summarize_callback=self.send_status_message,
        )

        # Store the updated summary if it changed
        if should_store_summary(chat.summary_for_next_call, summarized_context):
            if not chat.summary_for_next_call:
                chat.summary_for_next_call = []
            chat.summary_for_next_call.append(summarized_context)
            logger.info(f"Stored updated summary for chat {chat.id}")

        final_response_text = None
        final_context_part = None

        if self.is_trial_professional:
            await asyncio.sleep(0.5)  # Half a second delay for trial users.

        # Note: Medicaid queries are now handled through the tool calling system
        # The model can call the medicaid_info tool when needed using the format:
        # **medicaid_info {"state": "StateName", "topic": "", "limit": 5}**
        # (The double asterisks around the entire tool call are required)

        try:
            response_text, context_part = await self._call_llm_with_actions(
                primary_models,
                llm_input_message,
                summarized_context,
                history_for_llm,
                is_logged_in=(self.user is not None and self.user.is_authenticated),
                is_professional=self.is_professional or self.is_trial_professional,
                fallback_backends=fallback_models if fallback_models else None,
                full_history=full_history_for_llm,  # Also try with full history if model supports it
            )

            if response_text and response_text.strip():
                cleaned = remove_repeated_blocks(response_text.strip())
                cleaned = remove_repeated_sentences(cleaned) or cleaned
                final_response_text = cleaned
                final_context_part = context_part
        except Exception as e:
            await asyncio.sleep(0.1)
            logger.opt(exception=True).debug(
                f"Error with model on chat {primary_models}"
            )

        if final_response_text:
            if not chat.chat_history:
                chat.chat_history = []

            # Check for duplicate messages - don't add the same user message twice in a row
            is_duplicate = False
            if chat.chat_history and len(chat.chat_history) > 0:
                last_message = chat.chat_history[-1]
                if (
                    last_message.get("role") == "user"
                    and last_message.get("content") == user_message
                ):
                    is_duplicate = True
                    logger.info(
                        f"Duplicate message detected in chat {chat.id}, not adding to history again"
                    )

            # Check for messages that should be merged - if the last message is from the user and there's been no response
            should_merge = False
            merged_message = user_message
            if not is_duplicate and chat.chat_history and len(chat.chat_history) > 0:
                last_message = chat.chat_history[-1]
                if last_message.get("role") == "user":
                    # User sent two messages in a row with no assistant response in between
                    # Merge them together
                    should_merge = True
                    merged_message = f"{last_message.get('content')} {user_message}"
                    logger.info(f"Merging consecutive user messages in chat {chat.id}")
                    # Remove the last message since we're merging it
                    chat.chat_history = chat.chat_history[:-1]

            # Add the user message to history (unless it's a duplicate)
            if not is_duplicate:
                self._append_to_history(
                    chat,
                    "user",
                    merged_message if should_merge else user_message,
                )

            if should_store_summary(chat.summary_for_next_call, final_context_part):
                if not chat.summary_for_next_call:
                    chat.summary_for_next_call = []
                chat.summary_for_next_call.append(final_context_part)

            self._append_to_history(chat, "assistant", final_response_text)

            # If model didn't return a context summary, store a placeholder and
            # fire a background task to generate one
            background_summary_task = None
            if final_response_text and not final_context_part:
                if not chat.summary_for_next_call:
                    chat.summary_for_next_call = []
                history_len = len(chat.chat_history) if chat.chat_history else 0
                placeholder = make_placeholder(history_len)
                chat.summary_for_next_call.append(placeholder)
                background_summary_task = background_generate_summary(
                    chat.id, placeholder
                )

            # Merge any microsite context from DB before saving to avoid overwriting
            # background task updates
            await self._merge_summary_from_db(chat)
            await chat.asave()

            # Launch background summary after save so the task can read the latest history
            if background_summary_task is not None:
                await fire_and_forget_in_new_threadpool(background_summary_task)
            await self.send_message_to_client(final_response_text)
        else:
            # Provide more helpful error message based on context
            err_msg = (
                "Sorry, all available models (including backup models) are currently "
                "experiencing issues. Please try again in a few moments. "
                "If the problem persists, try refreshing the page or starting a new chat."
            )
            if not self.use_external_models:
                err_msg = (
                    "Sorry, our primary models are currently busy or experiencing issues. "
                    "You can enable 'Use backup models' in settings to allow fallback to "
                    "additional model providers when our primary models are unavailable."
                )
            logger.error(
                f"Failed to generate response for user_message: '{user_message}' in chat {chat.id} "
                f"after trying all models. use_external_models={self.use_external_models}"
            )
            await self.send_error_message(err_msg)

    async def replay_chat_history(self):
        """Sends the existing chat history to the client."""
        chat = self.chat
        history: Optional[List[Dict[str, Any]]] = chat.chat_history
        await self.send_json_message_func({"messages": chat.chat_history})

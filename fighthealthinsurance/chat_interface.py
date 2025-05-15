import re
import asyncio
import json
from asgiref.sync import sync_to_async
from django.utils import timezone
from loguru import logger
from typing import Optional, Callable, Awaitable, List, Dict, Tuple, Any  # Added Any

from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.ml.ml_models import RemoteModelLike
from fighthealthinsurance.models import OngoingChat
from fighthealthinsurance.pubmed_tools import PubMedTools


class ChatInterface:
    def __init__(
        self,
        send_json_message_func: Callable[[Dict[str, Any]], Awaitable[None]],
        chat: OngoingChat,
    ):  # Changed to Dict[str, Any]
        def wrap_send_json_message_func(message: Dict[str, Any]) -> Awaitable[None]:
            """Wraps the send_json_message_func to ensure it's always awaited."""
            if "chat_id" not in message:
                message["chat_id"] = str(chat.id)
            return send_json_message_func(message)

        self.send_json_message_func = wrap_send_json_message_func
        self.pubmed_tools = PubMedTools()
        self.chat: OngoingChat = chat

    async def send_error_message(self, message: str):
        """Sends an error message to the client."""
        await self.send_json_message_func(
            {"error": message, "chat_id": str(self.chat.id)}
        )

    async def send_status_message(self, message: str):
        """Sends a status message to the client."""
        await self.send_json_message_func(
            {"status": message, "chat_id": str(self.chat.id)}
        )

    async def send_message_to_client(self, message: str):
        """Sends a message to the client."""
        await self.send_json_message_func(
            {"content": message, "chat_id": str(self.chat.id), "role": "assistant"}
        )

    async def _get_professional_user_info(self) -> str:
        """Generates a descriptive string for the professional user."""
        try:
            return await sync_to_async(self.chat.summarize_professional_user)()
        except Exception as e:
            logger.warning(f"Could not generate detailed professional user info: {e}")
            return "a professional user"

    async def _call_llm_with_optional_pubmed(
        self,
        model_backend: RemoteModelLike,
        current_message_for_llm: str,
        previous_context_summary: Optional[str],
        history_for_llm: List[Dict[str, str]],
        depth: int = 0,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Calls the LLM, handles PubMed query requests if present and returns the response.
        """
        if depth > 2:
            return None, None
        chat = self.chat
        response_text, context_part = await model_backend.generate_chat_response(
            current_message_for_llm,
            previous_context_summary=previous_context_summary,
            history=history_for_llm,
        )

        pubmed_context_str = ""
        pubmed_query_terms_regex = r"[\[\*]{1,4}pubmed[ _]?query:{0,1}\s*(.*?)\s*[\*\]]{1,4}"
        if not response_text:
            logger.debug("Got empty response from LLM")
            return None, None
        try:
            # Extract the PubMedQuery terms using regex
            match = re.search(
                pubmed_query_terms_regex, response_text, flags=re.IGNORECASE
            )
            if match:
                pubmed_query_terms = match.group(1).strip()
                cleaned_response = response_text.replace(match.group(0), "").strip()
                if "your search terms" in pubmed_query_terms:
                    logger.debug(f"Got bad PubMed Query {pubmed_query_terms}")
                    return cleaned_response, context_part
                # Short circuit if no query terms
                if len(pubmed_query_terms.strip()) == 0:
                    return (cleaned_response, context_part)
                await self.send_status_message(
                    f"Searching PubMed for: {pubmed_query_terms}..."
                )


                recent_article_ids_awaitable = self.pubmed_tools.find_pubmed_article_ids_for_query(
                    query=pubmed_query_terms, since="2024", timeout=30.0
                )
                all_article_ids_awaitable = self.pubmed_tools.find_pubmed_article_ids_for_query(
                    query=pubmed_query_terms, timeout=30.0
                )
                (recent_article_ids, all_article_ids) = await asyncio.gather(
                    recent_article_ids_awaitable,
                    all_article_ids_awaitable
                )
                if all_article_ids or recent_article_ids:
                    article_ids = []
                    if not all_article_ids:
                        article_ids = recent_article_ids
                    elif recent_article_ids and all_article_ids:
                        article_ids = list(set(recent_article_ids[:6] + all_article_ids[:6]))
                    else:
                        article_ids = all_article_ids[:6]
                    await self.send_status_message(
                        f"Found {len(all_article_ids)} articles. Looking at {len(article_ids)} for context."
                    )
                    articles_data = await self.pubmed_tools.get_articles(
                        article_ids
                    )
                    summaries = []
                    for art in articles_data:
                        summary_text = art.abstract if art.abstract else art.text
                        if art.title and summary_text:
                            await self.send_status_message(
                                f"Found article: {art.title}"
                            )
                            summaries.append(
                                f"Title: {art.title}\\nAbstract: {summary_text[:500]}..."
                            )  # Truncate abstract
                    if summaries:
                        pubmed_context_str = (
                            "\\n\\nWe got back pubmedcontext:[:\\n" + "\\n\\n".join(summaries) +"]. If you reference them make sure to include the title and journal.\\n"
                        )
                        additional_response_text, additional_context_part = (
                            await self._call_llm_with_optional_pubmed(
                                model_backend,
                                pubmed_context_str,
                                previous_context_summary,
                                history_for_llm,
                                depth = depth + 1,
                            )
                        )
                        if cleaned_response and additional_response_text:
                            cleaned_response += additional_response_text
                        elif additional_response_text:
                            cleaned_response = additional_response_text
                        context_part = (
                            context_part + additional_context_part
                            if context_part and additional_context_part
                            else additional_context_part
                        )
                        response_text = cleaned_response
                else:
                    await self.send_status_message(
                        "No detailed information found for the articles from PubMed."
                    )
        except Exception as e:
            logger.warning(
                f"Error while processing PubMed query: {e}. Continuing with the original response."
            )
            await self.send_status_message(
                "Error while processing PubMed query. Continuing with the original response."
            )
        context = (
            context_part + pubmed_context_str if context_part else pubmed_context_str
        )
        return response_text, context

    async def handle_chat_message(self, user_message: str, iterate_on_appeal=None, iterate_on_prior_auth=None, user=None):
        """
        Handles an incoming chat message, interacts with LLMs, and manages chat history.
        """
        chat = self.chat
        # Handle chat ↔ appeal/prior auth linking if requested
        from fighthealthinsurance.models import Appeal, PriorAuthRequest

        link_message = None
        user_facing_message = None
        # Note: We intentionally do NOT send the link message to the LLM/model immediately.
        # This allows the user to drive the next step, and avoids confusing the model with system state changes.
        if iterate_on_appeal:
            try:
                appeal = await sync_to_async(Appeal.objects.get)(id=iterate_on_appeal)
            except Appeal.DoesNotExist:
                await self.send_error_message("Appeal not found.")
                return
            allowed_appeals = set(await sync_to_async(list)(Appeal.filter_to_allowed_appeals(user)))
            if appeal not in allowed_appeals:
                await self.send_error_message("You do not have permission to link this appeal.")
                return
            if appeal.chat_id != chat.id:
                appeal.chat = chat
                await sync_to_async(appeal.save)()
                link_message = f"Linked this chat to Appeal #{appeal.id}."
                user_facing_message = "Awesome, I'm happy to help you iterate on this appeal -- what would you like to do next?"
        if iterate_on_prior_auth:
            try:
                prior_auth = await sync_to_async(PriorAuthRequest.objects.get)(id=iterate_on_prior_auth)
            except PriorAuthRequest.DoesNotExist:
                await self.send_error_message("Prior Auth Request not found.")
                return
            allowed_auths = set(await sync_to_async(list)(PriorAuthRequest.filter_to_allowed_requests(user)))
            if prior_auth not in allowed_auths:
                await self.send_error_message("You do not have permission to link this prior auth request.")
                return
            if prior_auth.chat_id != chat.id:
                prior_auth.chat = chat
                await sync_to_async(prior_auth.save)()
                link_message = f"Linked this chat to Prior Auth Request #{prior_auth.id}."
                user_facing_message = "Awesome, I'm happy to help you iterate on this prior auth request -- what would you like to do next?"
        if link_message:
            if not chat.chat_history:
                chat.chat_history = []
            chat.chat_history.append({
                "role": "system",
                "content": link_message,
                "timestamp": timezone.now().isoformat(),
            })
            await chat.asave()
            # Send user-facing message (not to LLM)
            if user_facing_message:
                await self.send_message_to_client(user_facing_message)

        models = ml_router.get_chat_backends(use_external=False)
        if not models:
            await self.send_error_message(
                "Sorry, no language models are currently available."
            )
            return

        current_llm_context = None
        if chat.summary_for_next_call and len(chat.summary_for_next_call) > 0:
            current_llm_context = chat.summary_for_next_call[-1]

        is_new_chat = not bool(chat.chat_history)
        llm_input_message = user_message

        if is_new_chat:
            pro_user_info_str = await self._get_professional_user_info()
            intro_prefix = (
                f"You are a helpful assistant talking with {pro_user_info_str}. "
                f"You are helping them with their ongoing chat. You likely do not need to immediately generate a prior auth or appeal; instead, you'll have a chat with {pro_user_info_str} about their needs. Now, here is what they said to start the conversation:"
            )
            llm_input_message = intro_prefix + "\\n" + user_message

        # History passed to LLM should be the state *before* this user_message
        history_for_llm = list(chat.chat_history) if chat.chat_history else []

        final_response_text = None
        final_context_part = None

        for model_backend in models:
            try:
                response_text, context_part = await self._call_llm_with_optional_pubmed(
                    model_backend,
                    llm_input_message,
                    current_llm_context,
                    history_for_llm,  # Pass current history
                )

                if response_text and response_text.strip():
                    final_response_text = response_text.strip()
                    final_context_part = context_part
                    break
            except Exception as e:
                await asyncio.sleep(0.1)
                model_name = getattr(model_backend, "model_name", "Unknown Model")
                logger.opt(exception=True).debug(
                    f"Error with model {model_name} during chat generation: {e}"
                )

        if final_response_text:
            if not chat.chat_history:
                chat.chat_history = []

            chat.chat_history.append(
                {
                    "role": "user",
                    "content": user_message,  # Original user message
                    "timestamp": timezone.now().isoformat(),
                }
            )

            if final_context_part:
                if not chat.summary_for_next_call:
                    chat.summary_for_next_call = []
                chat.summary_for_next_call.append(final_context_part)

            chat.chat_history.append(
                {
                    "role": "assistant",
                    "content": final_response_text,
                    "timestamp": timezone.now().isoformat(),
                }
            )

            await chat.asave()
            await self.send_message_to_client(final_response_text)
        else:
            err_msg = "Sorry, I encountered an error while processing your request after trying available models."
            logger.error(
                f"Failed to generate response for user_message: '{user_message}' in chat {chat.id} after trying all models."
            )
            await self.send_error_message(err_msg)

    async def replay_chat_history(self):
        """Sends the existing chat history to the client."""
        chat = self.chat
        history: Optional[List[Dict[str, Any]]] = chat.chat_history
        await self.send_json_message_func({"messages": chat.chat_history})

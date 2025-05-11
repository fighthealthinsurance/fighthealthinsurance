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
        self, send_json_message_func: Callable[[Dict[str, Any]], Awaitable[None]]
    ):  # Changed to Dict[str, Any]
        self.send_json_message_func = send_json_message_func
        self.pubmed_tools = PubMedTools()

    async def send_message_to_client(self, content: str, is_error: bool = False):
        """Sends a message or an error to the connected client."""
        # Removed: if self.send_json_message_func: (it's guaranteed by __init__)
        if is_error:
            await self.send_json_message_func({"error": content})
        else:
            await self.send_json_message_func({"message": content})

    async def _get_professional_user_info(self, chat: OngoingChat) -> str:
        """Generates a descriptive string for the professional user."""
        try:
            return await sync_to_async(chat.summarize_professional_user)()
        except Exception as e:
            logger.warning(f"Could not generate detailed professional user info: {e}")
            return "a professional user"

    async def _call_llm_with_optional_pubmed(
        self,
        model_backend: RemoteModelLike,
        chat: OngoingChat,
        current_message_for_llm: str,
        previous_context_summary: Optional[str],
        history_for_llm: List[Dict[str, str]],
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Calls the LLM, handles PubMed query requests if present and returns the response.
        """
        response_text, context_part = await model_backend.generate_chat_response(
            current_message_for_llm,
            previous_context_summary=previous_context_summary,
            history=history_for_llm,
        )

        pubmed_context_str = ""
        if response_text and response_text.contains("**pubmedquery:"):
            # Extract the PubMedQuery terms using regex
            pubmed_query_terms_regex = r"\*\*pubmedquery:(.*?)\*\*"
            match = re.search(pubmed_query_terms_regex, response_text)
            if match:
                pubmed_query_terms = match.group(1).strip()
                cleaned_response = response_text.replace(match.group(0), "").strip()
                # Short circuit if no query terms
                if len(pubmed_query_terms.strip()) == 0:
                    return (cleaned_response, context_part)
                await self.send_message_to_client(
                    f"Searching PubMed for: {pubmed_query_terms}..."
                )

            article_ids = await self.pubmed_tools.find_pubmed_article_ids_for_query(
                query=pubmed_query_terms, timeout=30.0
            )
            if article_ids:
                articles_data = await self.pubmed_tools.get_articles(
                    article_ids[:3]
                )  # Limit to 3 articles
                summaries = []
                for art in articles_data:
                    summary_text = art.abstract if art.abstract else art.text
                    if art.title and summary_text:
                        summaries.append(
                            f"Title: {art.title}\\nAbstract: {summary_text[:500]}..."
                        )  # Truncate abstract
                if summaries:
                    pubmed_context_str = (
                        "\\n\\nPubMed Search Results:\\n" + "\\n\\n".join(summaries)
                    )
                    await self.send_message_to_client(
                        f"Found {len(article_ids)} relevant articles. Incorporating information into response..."
                    )
                    response_text, context_part = (
                        await self._call_llm_with_optional_pubmed(
                            model_backend,
                            chat,
                            pubmed_context_str,
                            previous_context_summary,
                            history_for_llm,
                        )
                    )
                else:
                    await self.send_message_to_client(
                        "No detailed information found for the articles from PubMed."
                    )
            else:
                await self.send_message_to_client(
                    "No articles found for the given query."
                )
        return response_text, context_part + pubmed_context_str

    async def handle_chat_message(self, chat: OngoingChat, user_message: str):
        """
        Handles an incoming chat message, interacts with LLMs, and manages chat history.
        """
        models = ml_router.get_chat_backends(use_external=False)
        if not models:
            await self.send_message_to_client(
                "Sorry, no language models are currently available.", is_error=True
            )
            return

        current_llm_context = None
        if chat.summary_for_next_call and len(chat.summary_for_next_call) > 0:
            current_llm_context = chat.summary_for_next_call[-1]

        is_new_chat = not bool(chat.chat_history)
        llm_input_message = user_message

        if is_new_chat:
            pro_user_info_str = await self._get_professional_user_info(chat)
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
                    chat,
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
            await self.send_message_to_client(err_msg, is_error=True)

    async def replay_chat_history(self, chat: OngoingChat):
        """Sends the existing chat history to the client."""
        # Removed: if not self.send_json_message_func: (guaranteed by __init__)
        # logger.error("send_json_message_func is not set in ChatInterface. Cannot replay history.")
        # return

        history: Optional[List[Dict[str, Any]]] = (
            chat.chat_history
        )  # Type hint for clarity
        if history:
            for entry in history:
                timestamp = entry.get("timestamp")
                timestamp_str: str
                if hasattr(timestamp, "isoformat"):
                    timestamp_str = timestamp.isoformat()
                elif isinstance(timestamp, str):
                    timestamp_str = timestamp
                else:
                    # Fallback, ideally log this unexpected case
                    logger.warning(
                        f"Unexpected timestamp type: {type(timestamp)} in chat {chat.id}. Using current time."
                    )
                    timestamp_str = timezone.now().isoformat()

                message_to_send: Dict[str, Any] = {}
                role = entry.get("role")
                content = entry.get("content")

                if role == "user":
                    message_to_send = {
                        "user_message": content,
                        "timestamp": timestamp_str,
                    }
                elif role == "assistant":
                    message_to_send = {"message": content, "timestamp": timestamp_str}

                if (
                    message_to_send
                ):  # Only send if we have a recognized role and content
                    await self.send_json_message_func(message_to_send)
                    await asyncio.sleep(0.01)  # Small delay between messages

        await self.send_json_message_func({"status": "replay_complete"})

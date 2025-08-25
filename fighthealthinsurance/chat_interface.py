import re
import asyncio
import json
from asgiref.sync import sync_to_async
from django.utils import timezone
from loguru import logger
from typing import (
    Optional,
    Callable,
    Awaitable,
    List,
    Dict,
    Tuple,
    Any,
    Union,
)  # Added Any, Union
from fhi_users.models import User, ProfessionalUser

from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.ml.ml_models import RemoteModelLike
from fighthealthinsurance.models import (
    OngoingChat,
    Denial,
    Appeal,
    PriorAuthRequest,
    ChatLeads,
)
from fighthealthinsurance.pubmed_tools import PubMedTools
from fighthealthinsurance import settings
from fighthealthinsurance.prompt_templates import get_intro_template
from fighthealthinsurance.utils import best_within_timelimit_static


class ChatInterface:
    def __init__(
        self,
        send_json_message_func: Callable[[Dict[str, Any]], Awaitable[None]],
        chat: OngoingChat,
        user: User,
        is_patient: bool,
    ):  # Changed to Dict[str, Any]
        def wrap_send_json_message_func(message: Dict[str, Any]) -> Awaitable[None]:
            """Wraps the send_json_message_func to ensure it's always awaited."""
            if "chat_id" not in message:
                message["chat_id"] = str(chat.id)
            return send_json_message_func(message)

        self.send_json_message_func = wrap_send_json_message_func
        self.pubmed_tools = PubMedTools()
        self.chat: OngoingChat = chat
        self.user: User = user
        self.is_patient: bool = is_patient

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

    async def _get_user_info(self) -> str:
        """Generates a descriptive string for the user (either professional or patient)."""
        try:
            return await sync_to_async(self.chat.summarize_user)()
        except Exception as e:
            logger.warning(f"Could not generate detailed user info: {e}")
            return "a user"

    async def _call_llm_with_actions(
        self,
        model_backend: RemoteModelLike,
        current_message_for_llm: str,
        previous_context_summary: Optional[str],
        history_for_llm: List[Dict[str, str]],
        depth: int = 0,
        is_logged_in: bool = True,
        is_professional: bool = True,
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Calls the LLM, handles PubMed query requests if present and returns the response.
        Also processes special tokens for creating or updating Appeals and PriorAuthRequests.
        """
        if depth > 2:
            return None, None
        chat = self.chat
        history = history_for_llm
        short_history = history_for_llm[
            -2:
        ]  # Only use the last two messages in history
        full_awaitable: Awaitable[Tuple[Optional[str], Optional[str]]] = (
            model_backend.generate_chat_response(
                current_message_for_llm,
                previous_context_summary=previous_context_summary,
                history=history,
                is_professional=not self.is_patient,
                is_logged_in=is_logged_in,
            )
        )
        short_awaitable: Awaitable[Tuple[Optional[str], Optional[str]]] = (
            model_backend.generate_chat_response(
                current_message_for_llm,
                previous_context_summary=previous_context_summary,
                history=short_history,
                is_professional=not self.is_patient,
                is_logged_in=is_logged_in,
            )
        )
        # Possible calls
        calls: Dict[Awaitable[Tuple[Optional[str], Optional[str]]], float] = {
            full_awaitable: 100.0,
            short_awaitable: 50.0,
        }
        response_text, context_part = await best_within_timelimit_static(
            calls,
            timeout=40.0,
        )

        pubmed_context_str = ""
        pubmed_query_terms_regex = (
            r"[\[\*]{0,4}pubmed[ _]?query:{0,1}\s*(.*?)\s*[\*\]]{0,4}"
        )
        # Updated regex to match both formats: **medicaid_info {JSON}** and medicaid_info {JSON}
        medicaid_info_lookup_regex = r"(?:\*\*)?medicaid_info\s*(\{[^}]*\})(?:\*\*)?"

        if not response_text:
            logger.debug("Got empty response from LLM")
            return None, None

        # Process the special tokens for Appeals and PriorAuthRequests
        create_or_update_appeal_regex = (
            r"^\s*\*{0,4}create_or_update_appeal\*{0,4}\s*(\{.*\})\s*$"
        )
        create_or_update_prior_auth_regex = (
            r"^\s*\*{0,4}create_or_update_prior_auth\*{0,4}\s*(\{.*\})\s*$"
        )
        # Use relative links
        domain = ""

        # Process if this is linked to an appeal or prior auth
        try:
            # Process create_or_update_appeal token
            appeal_match = re.search(
                create_or_update_appeal_regex, response_text, re.DOTALL | re.MULTILINE
            )
            if appeal_match:
                json_data = appeal_match.group(1).strip()
                try:
                    appeal_data = json.loads(json_data)
                    await self.send_status_message("Processing update appeal data...")

                    # Find an existing appeal linked to this chat or create a new one
                    from fighthealthinsurance.models import Appeal

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
                        denial = await Denial.objects.acreate(
                            creating_professional=pro_user,
                        )
                        appeal = await Appeal.objects.acreate(
                            chat=chat, creating_professional=pro_user, for_denial=denial
                        )
                        if (
                            "hashed_email" not in appeal_data
                            and hasattr(chat, "user")
                            and chat.user
                        ):
                            user_email = await sync_to_async(lambda: chat.user.email)()  # type: ignore
                            if user_email:
                                appeal_data["hashed_email"] = Denial.get_hashed_email(
                                    user_email
                                )

                    # Update appeal fields
                    if appeal and denial:
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
                                    f"Key {key} not found in Appeal or Denial model. The value {value} is not synced back yet."
                                )

                        await appeal.asave()
                        await denial.asave()

                        # Replace the token and JSON data with a status message
                        cleaned_response = response_text.replace(
                            appeal_match.group(0),
                            f"I've created/updated [Appeal #{appeal.id}]({domain}/appeals/{appeal.id}) for you.",
                        )
                        await self.send_status_message(
                            f"Appeal #{appeal.id} has been created/updated successfully."
                        )
                    else:
                        # Handle the case where appeal is None
                        cleaned_response = response_text.replace(
                            appeal_match.group(0),
                            "I couldn't create or update the appeal.",
                        )
                        await self.send_status_message(
                            "Failed to create or update appeal."
                        )

                    response_text = cleaned_response

                except json.JSONDecodeError as e:
                    logger.warning(
                        f"Invalid JSON data {e} in create_or_update_appeal token: {json_data}"
                    )
                    await self.send_error_message(
                        f"Error processing appeal data: Invalid JSON format {e} -- {json_data}"
                    )
                except Exception as e:
                    logger.opt(exception=True).warning(
                        f"Error processing appeal data: {e}"
                    )
                    await self.send_error_message(
                        f"Error processing appeal data: {str(e)}"
                    )

            # Process create_or_update_prior_auth token
            prior_auth_match = re.search(
                create_or_update_prior_auth_regex,
                response_text,
                re.DOTALL | re.MULTILINE,
            )
            if prior_auth_match:
                json_data = prior_auth_match.group(1).strip()
                try:
                    prior_auth_data = json.loads(json_data)
                    await self.send_status_message(
                        "Processing prior authorization update/create data..."
                    )

                    # Find an existing prior auth linked to this chat or create a new one
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

                    # Update prior auth fields
                    if prior_auth:
                        for key, value in prior_auth_data.items():
                            key = key.lower().strip()
                            if key == "medication" or key == "medication_name":
                                key = "treatment"
                            if hasattr(prior_auth, key):
                                setattr(prior_auth, key, value)
                            else:
                                logger.warning(
                                    f"Key {key} not found in Prior Auth model. Skipping."
                                )

                        await prior_auth.asave()

                        # Replace the token and JSON data with a status message
                        cleaned_response = response_text.replace(
                            prior_auth_match.group(0),
                            f"I've created/updated [Prior Auth Request #{prior_auth.id}]({domain}/prior-auths/view/{prior_auth.id}) for you.",
                        )
                        await self.send_status_message(
                            f"Prior Auth Request #{prior_auth.id} has been created/updated successfully."
                        )
                    else:
                        # Handle the case where prior_auth is None
                        cleaned_response = response_text.replace(
                            prior_auth_match.group(0),
                            "I couldn't create or update the prior authorization request.",
                        )
                        await self.send_status_message(
                            "Failed to create or update prior authorization request."
                        )

                    response_text = cleaned_response

                except json.JSONDecodeError:
                    logger.warning(
                        f"Invalid JSON data in create_or_update_prior_auth token: {json_data}"
                    )
                    await self.send_status_message(
                        "Error processing prior auth data: Invalid JSON format."
                    )
                except Exception as e:
                    logger.opt(exception=True).warning(
                        f"Error processing prior auth data: {e}"
                    )
                    await self.send_status_message(
                        f"Error processing prior auth data: {str(e)}"
                    )
        except Exception as e:
            logger.opt(exception=True).warning(f"Error processing special tokens: {e}")
            await self.send_status_message(f"Error processing special tokens: {str(e)}")

        # Handle Medicaid info lookup first (before PubMed)
        try:
            # Find ALL matches but only process the FIRST one to avoid multiple calls
            medicaid_info_matches = list(
                re.finditer(
                    medicaid_info_lookup_regex,
                    response_text,
                    flags=re.DOTALL | re.IGNORECASE,
                )
            )

            if medicaid_info_matches:
                # Only process the first match
                medicaid_info_match = medicaid_info_matches[0]
                if len(medicaid_info_matches) > 1:
                    logger.warning(
                        f"Found {len(medicaid_info_matches)} Medicaid tool calls, processing only the first one"
                    )

                logger.debug(
                    f"Medicaid tool call detected: {medicaid_info_match.group(0)}"
                )

                # Remove ALL Medicaid tool calls from the response, not just the first one
                cleaned_response = response_text
                for match in medicaid_info_matches:
                    cleaned_response = cleaned_response.replace(match.group(0), "")
                cleaned_response = cleaned_response.strip()

                json_data = medicaid_info_match.group(1).strip()
                logger.debug(f"Extracted JSON data: {json_data}")
                logger.debug(
                    f"Cleaned response after removing tool calls: {cleaned_response[:200]}..."
                )
                try:
                    medicaid_info_data = json.loads(json_data)
                    logger.debug(f"Parsed JSON data: {medicaid_info_data}")

                    await self.send_status_message(
                        "Processing Medicaid info lookup data..."
                    )

                    from fighthealthinsurance.medicaid_api import get_medicaid_info

                    medicaid_info = get_medicaid_info(medicaid_info_data)
                    logger.debug(
                        f"Got Medicaid info response: {medicaid_info[:200] if medicaid_info else 'None'}..."
                    )

                    if medicaid_info:
                        await self.send_status_message(
                            "Medicaid info lookup completed successfully."
                        )
                        # Add brief intro and conclusion to the tool data
                        state_name = medicaid_info_data.get("state", "the state")
                        medicaid_info_text = f"Here's the official Medicaid information for {state_name}:\n\n{medicaid_info}\n\n -- use it to answer the question {current_message_for_llm}"
                        # Pass that info to the model
                        additional_response_text, additional_context_part = (
                            await self._call_llm_with_actions(
                                model_backend,
                                medicaid_info_text,
                                previous_context_summary,
                                history_for_llm,
                                depth=depth+1,
                                is_logged_in=is_logged_in,
                                is_professional=is_professional))
                        # Log the response for debugging
                        logger.debug(
                            f"Medicaid with intro/conclusion: {medicaid_info[:200]}..."
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
                            "No Medicaid info found for the provided data."
                        )
                        response_text = "I couldn't find Medicaid information for the requested state. Please check the state name and try again."

                except json.JSONDecodeError:
                    logger.warning(
                        f"Invalid JSON data in medicaid_info token: {json_data}"
                    )
                    await self.send_status_message(
                        "Error processing Medicaid info data: Invalid JSON format."
                    )
                except Exception as e:
                    logger.opt(exception=True).warning(
                        f"Error processing Medicaid info data: {e}"
                    )
                    await self.send_status_message(
                        f"Error processing Medicaid info data: {str(e)}"
                    )
        except Exception as e:
            logger.opt(exception=True).warning(f"Error in Medicaid lookup block: {e}")
            await self.send_status_message(f"Error in Medicaid lookup block: {str(e)}")

        # Handle pubmed
        try:
            # Extract the PubMedQuery terms using regex
            match = re.search(
                pubmed_query_terms_regex, response_text, flags=re.IGNORECASE
            )
            # If we match on a tool call, remove the tool call from the result we give to the user.
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

                recent_article_ids_awaitable = (
                    self.pubmed_tools.find_pubmed_article_ids_for_query(
                        query=pubmed_query_terms, since="2024", timeout=30.0
                    )
                )
                all_article_ids_awaitable = (
                    self.pubmed_tools.find_pubmed_article_ids_for_query(
                        query=pubmed_query_terms, timeout=30.0
                    )
                )
                article_id_results: tuple[
                    Union[list[str], None, BaseException],
                    Union[list[str], None, BaseException],
                ] = await asyncio.gather(
                    recent_article_ids_awaitable,
                    all_article_ids_awaitable,
                    return_exceptions=True,
                )
                recent_article_ids = article_id_results[0]
                all_article_ids = article_id_results[1]
                if isinstance(all_article_ids, list) or isinstance(
                    recent_article_ids, list
                ):
                    article_ids_set: set[str] = set()
                    # Display the higher of all and recent, generally all will be higher unless it failed.
                    num_articles = 0

                    if isinstance(recent_article_ids, list):
                        article_ids_set = article_ids_set | set(recent_article_ids[:6])
                        num_articles = len(recent_article_ids)
                    if isinstance(all_article_ids, list):
                        article_ids_set = article_ids_set | set(all_article_ids[:2])
                        if num_articles < len(all_article_ids):
                            num_articles = len(all_article_ids)
                    article_ids = list(article_ids_set)
                    await self.send_status_message(
                        f"Found {num_articles} articles. Looking at {len(article_ids)} for context."
                    )
                    articles_data = await self.pubmed_tools.get_articles(article_ids)
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
                            "\\n\\nWe got back pubmedcontext:[:\\n"
                            + "\\n\\n".join(summaries)
                            + "]. If you reference them make sure to include the title and journal.\\n"
                        )
                        additional_response_text, additional_context_part = (
                            await self._call_llm_with_actions(
                                model_backend,
                                pubmed_context_str,
                                previous_context_summary,
                                history_for_llm,
                                depth=depth + 1,
                                is_logged_in=is_logged_in,
                                is_professional=is_professional,
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
            if not chat.chat_history:
                chat.chat_history = []
            chat.chat_history.append(
                {
                    "role": "user",
                    "content": link_message,
                    "timestamp": timezone.now().isoformat(),
                }
            )
            chat.chat_history.append(
                {
                    "role": "assistant",
                    "content": user_facing_message,
                    "timestamp": timezone.now().isoformat(),
                }
            )
            await asyncio.gather(
                chat.asave(), self.send_message_to_client(user_facing_message)
            )
            return

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

        is_trial_professional = False

        # Check if the user is a trial professional
        try:
            if await ChatLeads.objects.filter(session_id=chat.session_key).aexists():
                lead = await ChatLeads.objects.filter(
                    session_id=chat.session_key
                ).afirst()
                drug = None
                if lead:
                    drug = lead.drug
                if not drug or drug == "":
                    # If the lead is not linked to a drug, they are a trial professional
                    if not await ProfessionalUser.objects.filter(user=user).aexists():
                        is_trial_professional = True
                else:
                    logger.debug(
                        f"User is a lead with a drug {drug} -- not a trial professional"
                    )
        except Exception as e:
            logger.warning(f"Error checking if user is a trial professional: {e}")
            is_trial_professional = True

        is_patient = self.is_patient
        if is_new_chat:
            # If this is a trial professional user, add a banner message to the chat history
            if is_trial_professional:
                trial_banner = {
                    "role": "system",
                    "content": "⚠️ You're using a free trial version. Responses may be slower, and features like linked appeals and prior auths require a full professional account.\n\nWant full access? [Create a free account →](/signup)",
                    "timestamp": timezone.now().isoformat(),
                }

                # We don't add the trial banner to the history since it needs to go user -> agent -> user.

                # Send the trial banner to the client
                await self.send_json_message_func(trial_banner)

            user_info_str = await self._get_user_info()
            template = get_intro_template(chat.is_patient)
            llm_input_message = template.format(
                user_info=user_info_str, message=user_message
            )

        # History passed to LLM should be the state *before* this user_message
        history_for_llm = list(chat.chat_history) if chat.chat_history else []

        final_response_text = None
        final_context_part = None

        if is_trial_professional:
            await asyncio.sleep(0.5)  # Half a second delay for trial users.

        # Note: Medicaid queries are now handled through the tool calling system
        # The model can call the medicaid_info tool when needed using the format:
        # **medicaid_info {"state": "StateName", "topic": "", "limit": 5}**
        # (The double asterisks around the entire tool call are required)

        # Fallback: If the model doesn't call the tool but should, we'll detect it here
        medicaid_keywords = [
            "medicaid",
            "medicare",
            "medi-cal",
            "health insurance",
            "healthcare",
        ]
        user_message_lower = user_message.lower()
        is_medicaid_query = any(
            keyword in user_message_lower for keyword in medicaid_keywords
        )

        # Extract state from user message if present
        detected_state = None
        if is_medicaid_query:
            # Simple state detection
            states = [
                "alabama",
                "alaska",
                "arizona",
                "arkansas",
                "california",
                "colorado",
                "connecticut",
                "delaware",
                "florida",
                "georgia",
                "hawaii",
                "idaho",
                "illinois",
                "indiana",
                "iowa",
                "kansas",
                "kentucky",
                "louisiana",
                "maine",
                "maryland",
                "massachusetts",
                "michigan",
                "minnesota",
                "mississippi",
                "missouri",
                "montana",
                "nebraska",
                "nevada",
                "new hampshire",
                "new jersey",
                "new mexico",
                "new york",
                "north carolina",
                "north dakota",
                "ohio",
                "oklahoma",
                "oregon",
                "pennsylvania",
                "rhode island",
                "south carolina",
                "south dakota",
                "tennessee",
                "texas",
                "utah",
                "vermont",
                "virginia",
                "washington",
                "west virginia",
                "wisconsin",
                "wyoming",
            ]
            for state in states:
                if state in user_message_lower:
                    detected_state = state.title()
                    break

        for model_backend in models:
            try:
                response_text, context_part = await self._call_llm_with_actions(
                    model_backend,
                    llm_input_message,
                    current_llm_context,
                    history_for_llm,  # Pass current history
                    is_logged_in=(not is_trial_professional) and not is_patient,
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
                chat.chat_history.append(
                    {
                        "role": "user",
                        "content": merged_message if should_merge else user_message,
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

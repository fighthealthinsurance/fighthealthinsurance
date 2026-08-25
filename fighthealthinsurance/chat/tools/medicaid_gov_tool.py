"""Medicaid.gov (and friends) page-lookup tool for the chat interface.

Handles ``medicaid_gov_lookup`` calls: resolves a curated page, an explicit
allowlisted URL, or a free-text query to a page on Medicaid.gov, fetches it,
and feeds the text back into the conversation.

Distinct from ``fetch_doc``, which fetches an arbitrary URL the USER shared.
This tool only ever reaches a small allowlist of public coverage references
(see ``medicaid_gov_api.ALLOWED_HOSTS``), so the model can go get an
authoritative answer without being handed a general-purpose fetcher.
"""

import json
import re
from typing import Any, Awaitable, Callable, List, Optional, Tuple

from asgiref.sync import sync_to_async
from loguru import logger

from fighthealthinsurance.extralink_fetcher import ExtraLinkFetcher

from .base_tool import BaseTool
from .doc_fetcher_tool import _sanitize_url_for_display, validate_url
from .patterns import MEDICAID_GOV_LOOKUP_REGEX

# Characters of page text handed to the LLM. Medicaid.gov pages are wordy and
# the eligibility-levels table is enormous; this keeps one lookup from eating
# the whole context window.
MAX_PAGE_TEXT_LENGTH = 12_000

# Per-session cap. Separate from fetch_doc's budget: these are allowlisted
# reference pages, not user-supplied URLs, so they get their own (small)
# allowance rather than competing with documents the user actually shared.
MAX_LOOKUPS_PER_SESSION = 4


class MedicaidGovLookupTool(BaseTool):
    """
    Tool handler for Medicaid.gov page lookups.

    Accepted call shapes::

        **medicaid_gov_lookup {"page": "renew_info", "state": "IA"}**
        **medicaid_gov_lookup {"page": "eligibility_levels"}**
        **medicaid_gov_lookup {"query": "renewal paperwork"}**
        **medicaid_gov_lookup {"url": "https://www.medicaid.gov/eligibility"}**
    """

    pattern = MEDICAID_GOV_LOOKUP_REGEX
    name = "Medicaid.gov Lookup"

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        call_llm_callback: Optional[
            Callable[..., Awaitable[Tuple[Optional[str], Optional[str]]]]
        ] = None,
        lookup_count: Optional[list] = None,
    ):
        super().__init__(send_status_message)
        self.call_llm_callback = call_llm_callback
        self.fetcher = ExtraLinkFetcher()
        # One-element list owned by the ChatInterface so the cap survives the
        # tool being rebuilt each turn (same trick as fetch_doc's counter).
        self._lookup_count = lookup_count if lookup_count is not None else [0]

    @staticmethod
    def _resolve_target(params: dict) -> Tuple[Optional[str], List[str], str]:
        """Pick the page to fetch.

        Returns ``(url, other_candidate_urls, how_we_got_there)``.

        BLOCKING: the ``query`` path walks medicaid.gov's sitemap over the
        network on a cold cache. ``execute`` bridges this off the event loop;
        don't call it inline from async code.
        """
        from fighthealthinsurance.medicaid_gov_api import (
            is_allowed_url,
            resolve_curated_source,
            search_medicaid_gov,
            suggest_curated_sources,
        )

        page = params.get("page")
        if isinstance(page, str) and page.strip():
            url = resolve_curated_source(page, params.get("state"))
            if url:
                return url, [], f'curated page "{page}"'

        url_value = params.get("url")
        if isinstance(url_value, str) and url_value.strip():
            candidate = url_value.strip()
            if is_allowed_url(candidate):
                return candidate, [], "the URL provided"
            logger.warning(f"medicaid_gov_lookup refused off-allowlist URL {candidate}")

        query = params.get("query") or params.get("topic")
        if isinstance(query, str) and query.strip():
            # Curated pages first: we already know these answer the question,
            # and several of them are unreachable by slug matching.
            curated = suggest_curated_sources(query)
            others = [source.url for source in curated[1:]]
            hits = search_medicaid_gov(query, limit=4)
            if curated:
                return (
                    curated[0].url,
                    others + [u for u, _ in hits[:3]],
                    (f'search for "{query}"'),
                )
            if hits:
                return hits[0][0], [u for u, _ in hits[1:]], f'search for "{query}"'

        return None, [], ""

    async def execute(
        self,
        match: re.Match,
        response_text: str,
        context: str,
        model_backends: Any = None,
        previous_context_summary: str = "",
        history_for_llm: Any = None,
        depth: int = 0,
        is_logged_in: bool = False,
        is_professional: bool = False,
        **kwargs,
    ) -> Tuple[str, str]:
        from fighthealthinsurance.medicaid_gov_api import curated_source_menu

        cleaned_response = self.clean_response(response_text, match)

        try:
            params = json.loads(match.group(1).strip())
        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON in medicaid_gov_lookup: {e}")
            return cleaned_response, context
        if not isinstance(params, dict):
            logger.warning("medicaid_gov_lookup called with non-object JSON")
            return cleaned_response, context

        # Budget first: resolving a {"query": ...} call walks the sitemap over
        # the network, and a session that has spent its allowance shouldn't
        # pay for a lookup it can't use.
        if self._lookup_count[0] >= MAX_LOOKUPS_PER_SESSION:
            logger.warning("medicaid_gov_lookup session cap reached")
            await self.send_status_message(
                "Medicaid.gov lookup limit reached for this conversation."
            )
            return cleaned_response, context

        # plain sync_to_async, not database_sync_to_async: _resolve_target
        # does network IO (the sitemap walk) and touches no ORM, so the
        # per-call DB connection cleanup would be pure churn. Bridged rather
        # than awaited inline because a cold-cache walk is seconds of blocking
        # requests, which would freeze every OTHER chat on this worker.
        #
        # thread_sensitive=False for the same reason: the default routes every
        # such call through ONE shared worker thread, so a cold sitemap walk
        # would queue up behind (and ahead of) unrelated thread-sensitive work
        # instead of blocking only itself. Safe here precisely because there's
        # no ORM or thread-local state to keep on one thread.
        url, other_urls, how = await sync_to_async(
            self._resolve_target, thread_sensitive=False
        )(params)
        if not url:
            # Tell the model what it *could* have asked for rather than
            # failing silently -- a bad page name is a recoverable mistake.
            return cleaned_response, context + (
                "\n\nThe medicaid_gov_lookup call didn't name a page we could "
                "find. Available pages:\n" + curated_source_menu() + "\n"
                'You can also pass {"query": "..."} to search Medicaid.gov.\n'
            )

        self._lookup_count[0] += 1

        safe_url = _sanitize_url_for_display(url)
        await self.send_status_message(f"Looking up {safe_url}...")

        try:
            full_text, doc_type = await self.fetcher.fetch_and_extract_text(
                url, url_validator=validate_url
            )
        except Exception as e:
            logger.warning(f"medicaid_gov_lookup failed to fetch {safe_url}: {e}")
            await self.send_status_message(f"Couldn't reach {safe_url}.")
            return cleaned_response, context

        if not full_text or not full_text.strip():
            await self.send_status_message(f"No readable content at {safe_url}.")
            return cleaned_response, context

        await self.send_status_message(
            f"Read {len(full_text)} characters from {safe_url} ({doc_type})."
        )

        also_lines = ""
        if other_urls:
            also_lines = "\nOther pages that may be relevant:\n" + "\n".join(
                f"- {u}" for u in other_urls[:3]
            )

        user_question = kwargs.get("current_message_for_llm") or ""
        question_part = f"\nThe user asked: {user_question}\n" if user_question else ""
        page_context = (
            f"\n\nFrom {url} (found via {how}):\n"
            f"{full_text[:MAX_PAGE_TEXT_LENGTH]}\n"
            f"{also_lines}\n{question_part}"
            "Answer the user's question from this page. Link them to the URL "
            "above so they can read it themselves, and say it's the official "
            "source. If the page doesn't actually answer what they asked, say "
            "so rather than stretching it -- and remember state Medicaid "
            "agencies are the authority on any individual's coverage.\n"
        )

        if self.call_llm_callback and model_backends:
            additional_response, additional_context = await self.call_llm_callback(
                model_backends,
                page_context,
                previous_context_summary,
                history_for_llm,
                depth=depth + 1,
                is_logged_in=is_logged_in,
                is_professional=is_professional,
                fallback_backends=kwargs.get("fallback_backends"),
                full_history=kwargs.get("full_history"),
                # Raw user message, so repeat detection in the recursive pass
                # keys off what the USER said rather than this tool payload.
                user_message_for_scoring=kwargs.get("user_message_for_scoring"),
            )
            if cleaned_response and additional_response:
                cleaned_response += additional_response
            elif additional_response:
                cleaned_response = additional_response
            if context and additional_context:
                context += additional_context
            elif additional_context:
                context = additional_context

        return cleaned_response, (context + page_context if context else page_context)

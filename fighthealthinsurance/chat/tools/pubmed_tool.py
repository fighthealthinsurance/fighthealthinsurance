"""
PubMed search tool handler for the chat interface.

Handles pubmed_query tool calls from the LLM to search PubMed for
relevant medical literature.
"""
import asyncio
import re
from typing import Optional, Tuple, Union, Callable, Awaitable, Any
from loguru import logger

from .base_tool import BaseTool
from .patterns import PUBMED_QUERY_REGEX
from fighthealthinsurance.pubmed_tools import PubMedTools


class PubMedTool(BaseTool):
    """
    Tool handler for PubMed literature searches.

    When the LLM includes a pubmed_query in its response, this tool:
    1. Extracts the search terms
    2. Searches PubMed for recent and all-time articles
    3. Retrieves article details
    4. Returns context for the LLM to incorporate
    """

    pattern = PUBMED_QUERY_REGEX
    name = "PubMed"

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
        pubmed_tools: Optional[PubMedTools] = None,
        call_llm_callback: Optional[Callable[..., Awaitable[Tuple[str, str]]]] = None,
    ):
        """
        Initialize the PubMed tool.

        Args:
            send_status_message: Async function to send status updates
            pubmed_tools: PubMedTools instance (created if not provided)
            call_llm_callback: Callback to call LLM with additional context
        """
        super().__init__(send_status_message)
        self.pubmed_tools = pubmed_tools or PubMedTools()
        self.call_llm_callback = call_llm_callback

    async def execute(
        self,
        match: re.Match[str],
        response_text: str,
        context: str,
        model_backends: Any = None,
        previous_context_summary: str = "",
        history_for_llm: Any = None,
        depth: int = 0,
        is_logged_in: bool = False,
        is_professional: bool = False,
        **kwargs
    ) -> Tuple[str, str]:
        """
        Execute PubMed search and incorporate results.

        Args:
            match: Regex match containing search terms
            response_text: Current LLM response
            context: Current context string
            model_backends: LLM backends for follow-up calls
            previous_context_summary: Summary for context
            history_for_llm: Chat history
            depth: Current recursion depth
            is_logged_in: Whether user is logged in
            is_professional: Whether user is a professional

        Returns:
            Tuple of (updated_response, updated_context)
        """
        pubmed_query_terms = match.group(1).strip()
        cleaned_response = self.clean_response(response_text, match)

        # Validate query terms
        if "your search terms" in pubmed_query_terms:
            logger.debug(f"Got bad PubMed Query {pubmed_query_terms}")
            return cleaned_response, context

        if len(pubmed_query_terms.strip()) == 0:
            return cleaned_response, context

        await self.send_status_message(
            f"Searching PubMed for: {pubmed_query_terms}..."
        )

        # Search for both recent and all-time articles in parallel
        article_ids = await self._search_articles(pubmed_query_terms)

        if not article_ids:
            await self.send_status_message(
                "No detailed information found for the articles from PubMed."
            )
            return cleaned_response, context

        # Get article details and build context
        pubmed_context = await self._build_article_context(article_ids)

        if not pubmed_context:
            return cleaned_response, context

        # If we have a callback to call the LLM with the new context, use it
        if self.call_llm_callback and model_backends:
            additional_response, additional_context = await self.call_llm_callback(
                model_backends,
                pubmed_context,
                previous_context_summary,
                history_for_llm,
                depth=depth + 1,
                is_logged_in=is_logged_in,
                is_professional=is_professional,
            )

            if cleaned_response and additional_response:
                cleaned_response += additional_response
            elif additional_response:
                cleaned_response = additional_response

            if context and additional_context:
                context = context + additional_context
            elif additional_context:
                context = additional_context

        # Append PubMed context
        updated_context = context + pubmed_context if context else pubmed_context

        return cleaned_response, updated_context

    async def _search_articles(self, query: str) -> list[str]:
        """
        Search PubMed for articles matching the query.

        Searches both recent (since 2024) and all-time articles,
        then combines the results.

        Args:
            query: Search terms

        Returns:
            List of unique article IDs
        """
        recent_awaitable = self.pubmed_tools.find_pubmed_article_ids_for_query(
            query=query, since="2024", timeout=30.0
        )
        all_awaitable = self.pubmed_tools.find_pubmed_article_ids_for_query(
            query=query, timeout=30.0
        )

        results: tuple[
            Union[list[str], None, BaseException],
            Union[list[str], None, BaseException],
        ] = await asyncio.gather(
            recent_awaitable, all_awaitable, return_exceptions=True
        )

        recent_ids = results[0]
        all_ids = results[1]

        if not isinstance(all_ids, list) and not isinstance(recent_ids, list):
            return []

        article_ids_set: set[str] = set()
        num_articles = 0

        if isinstance(recent_ids, list):
            article_ids_set |= set(recent_ids[:6])
            num_articles = len(recent_ids)

        if isinstance(all_ids, list):
            article_ids_set |= set(all_ids[:2])
            if num_articles < len(all_ids):
                num_articles = len(all_ids)

        article_ids = list(article_ids_set)

        await self.send_status_message(
            f"Found {num_articles} articles. Looking at {len(article_ids)} for context."
        )

        return article_ids

    async def _build_article_context(self, article_ids: list[str]) -> str:
        """
        Fetch article details and build context string.

        Args:
            article_ids: List of PubMed article IDs

        Returns:
            Formatted context string with article summaries
        """
        articles_data = await self.pubmed_tools.get_articles(article_ids)

        summaries = []
        for art in articles_data:
            summary_text = art.abstract if art.abstract else art.text
            if art.title and summary_text:
                await self.send_status_message(f"Found article: {art.title}")
                summaries.append(
                    f"Title: {art.title}\\nAbstract: {summary_text[:500]}..."
                )

        if not summaries:
            return ""

        return (
            "\\n\\nWe got back pubmedcontext:[:\\n"
            + "\\n\\n".join(summaries)
            + "]. If you reference them make sure to include the title and journal.\\n"
        )

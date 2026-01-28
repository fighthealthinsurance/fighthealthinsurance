"""
ExtraLink Context Helper.

Provides helper methods for retrieving extralink context and citations
for use in appeal generation and chat interfaces.
"""

from typing import List

from loguru import logger

from fighthealthinsurance.models import MicrositeExtraLink


class ExtraLinkContextHelper:
    """
    Helper for retrieving extralink context for appeals and chat.

    This class provides methods to:
    - Get formatted context from extralinks for a microsite
    - Get citation strings for inclusion in appeals
    """

    @staticmethod
    async def get_context_for_microsite(
        microsite_slug: str,
        max_docs: int = 5,
        max_chars_per_doc: int = 2000,
    ) -> str:
        """
        Get formatted context from extralink documents for a microsite.

        This context can be included in ML prompts for appeal generation
        or chat conversations to provide additional evidence and guidelines.

        Args:
            microsite_slug: The microsite identifier
            max_docs: Maximum number of documents to include
            max_chars_per_doc: Maximum characters per document

        Returns:
            Formatted markdown context string ready for inclusion in prompts.
            Returns empty string if no extralinks found or on error.
        """
        if not microsite_slug:
            return ""

        try:
            # Get all successfully fetched extralinks for this microsite
            # Ordered by priority (high first) then display_order
            links = []
            async for link in (
                MicrositeExtraLink.objects.filter(
                    microsite_slug=microsite_slug,
                    document__fetch_status="success",
                )
                .select_related("document")
                .order_by("priority", "display_order")[:max_docs]
            ):
                links.append(link)

            if not links:
                logger.debug(f"No extralink documents found for {microsite_slug}")
                return ""

            # Format context
            context_parts = [
                "## Additional Medical Evidence and Guidelines\n",
                "The following resources support the medical necessity of this treatment:\n",
            ]

            for i, link in enumerate(links, 1):
                doc = link.document

                # Use summary if available, otherwise use raw text
                content = doc.summary or doc.raw_text

                if content:
                    # Truncate if needed
                    if len(content) > max_chars_per_doc:
                        content = content[:max_chars_per_doc] + "..."

                    title = link.title or f"Document {i}"

                    context_parts.append(f"\n### {title}")
                    if link.description:
                        context_parts.append(f"*{link.description}*\n")
                    context_parts.append(f"Source: {doc.url}\n")
                    context_parts.append(content)
                    context_parts.append("\n")

            result = "\n".join(context_parts)
            logger.debug(
                f"Generated extralink context for {microsite_slug}: "
                f"{len(links)} docs, {len(result)} chars"
            )

            return result

        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error getting extralink context for {microsite_slug}: {e}"
            )
            return ""

    @staticmethod
    async def get_extralink_citations(
        microsite_slug: str,
        max_citations: int = 3,
    ) -> List[str]:
        """
        Get citation strings from extralinks for inclusion in appeals.

        Citations are formatted as strings that can be appended to
        the citations section of an appeal letter.

        Args:
            microsite_slug: The microsite identifier
            max_citations: Maximum number of citations to return

        Returns:
            List of citation strings. Returns empty list if none found or on error.
        """
        if not microsite_slug:
            return []

        try:
            citations = []

            async for link in (
                MicrositeExtraLink.objects.filter(
                    microsite_slug=microsite_slug,
                    document__fetch_status="success",
                    priority__in=[0, 1],  # 0=highest, 1=medium
                )
                .select_related("document")
                .order_by("priority")[:max_citations]
            ):
                title = link.title or "Medical guideline"
                url = link.document.url

                # Create citation text
                citation = f"{title}. Available at: {url}"
                citations.append(citation)

            if citations:
                logger.debug(
                    f"Generated {len(citations)} extralink citations for {microsite_slug}"
                )

            return citations

        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error getting extralink citations for {microsite_slug}: {e}"
            )
            return []

    @staticmethod
    async def has_extralinks(microsite_slug: str) -> bool:
        """
        Check if a microsite has any successfully fetched extralinks.

        Args:
            microsite_slug: The microsite identifier

        Returns:
            True if extralinks exist, False otherwise
        """
        if not microsite_slug:
            return False

        try:
            count = await MicrositeExtraLink.objects.filter(
                microsite_slug=microsite_slug,
                document__fetch_status="success",
            ).acount()

            return count > 0

        except Exception:
            return False

    @staticmethod
    async def fetch_extralink_context_for_microsite(
        microsite_slug: str,
        max_docs: int = 5,
        max_chars_per_doc: int = 2000,
    ) -> str:
        """
        Fetch extralink context for a microsite with error handling and logging.

        This is a convenience method that wraps get_context_for_microsite with
        additional checking and logging suitable for use in both chat and appeal
        generation workflows.

        Args:
            microsite_slug: The microsite identifier
            max_docs: Maximum number of documents to include
            max_chars_per_doc: Maximum characters per document

        Returns:
            Formatted markdown context string, or empty string if unavailable.
        """
        if not microsite_slug:
            return ""

        try:
            logger.info(f"Loading extralink context for microsite {microsite_slug}")

            extralink_context = await ExtraLinkContextHelper.get_context_for_microsite(
                microsite_slug,
                max_docs=max_docs,
                max_chars_per_doc=max_chars_per_doc,
            )

            if extralink_context:
                logger.info(
                    f"Added {len(extralink_context)} chars of extralink context"
                )
            else:
                logger.debug(f"No extralink context available for {microsite_slug}")

            return extralink_context

        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error loading extralink context for {microsite_slug}: {e}"
            )
            return ""

"""
Helper for extracting and summarizing relevant sections from plan documents.

Uses internal ML models to:
1. Generate search terms based on denial letter and procedure
2. Extract relevant pages/sections from plan documents
3. Summarize relevant sections for use in appeal generation
"""

import asyncio
import re
from typing import Optional, List, Set
from loguru import logger

import pymupdf

from fighthealthinsurance.models import Denial, PlanDocuments
from fighthealthinsurance.ml.ml_router import ml_router


class MLPlanDocHelper:
    """Helper class for ML-powered plan document analysis."""

    # Maximum characters to send to the model for summarization
    MAX_CONTEXT_LENGTH = 8000
    # Maximum time to spend on plan document summarization
    TIMEOUT_SECONDS = 60

    @classmethod
    async def generate_search_terms(
        cls,
        denial_text: str,
        procedure: Optional[str] = None,
        diagnosis: Optional[str] = None,
    ) -> List[str]:
        """
        Generate search terms to find relevant sections in plan documents.

        Uses internal ML models to analyze the denial letter and procedure
        to generate terms that would help find relevant plan sections.

        Args:
            denial_text: The denial letter text
            procedure: The denied procedure (if known)
            diagnosis: The diagnosis (if known)

        Returns:
            List of search terms to use for finding relevant plan sections
        """
        # Build context for the model
        context_parts = []
        if denial_text:
            # Truncate denial text if too long
            context_parts.append(f"Denial letter excerpt: {denial_text[:2000]}")
        if procedure:
            context_parts.append(f"Denied procedure: {procedure}")
        if diagnosis:
            context_parts.append(f"Diagnosis: {diagnosis}")

        if not context_parts:
            return []

        context = "\n".join(context_parts)

        prompt = f"""Based on this health insurance denial, generate a list of search terms
that would help find relevant sections in the insurance plan documents.

{context}

Generate 5-10 specific search terms that would help find:
- Coverage policies for this procedure/treatment
- Medical necessity criteria
- Appeal procedures and timelines
- Exclusions that might apply
- Prior authorization requirements

Return ONLY the search terms, one per line, no numbering or explanations.
Focus on terms that would appear in an insurance plan document."""

        models = ml_router.internal_models_by_cost[:3]

        for model in models:
            try:
                result = await asyncio.wait_for(
                    model._infer_no_context(
                        system_prompts=[
                            "You are an expert at analyzing health insurance documents. "
                            "Generate concise, specific search terms."
                        ],
                        prompt=prompt,
                        temperature=0.3,
                    ),
                    timeout=30,
                )
                if result:
                    # Parse the response into individual terms
                    terms = [
                        line.strip()
                        for line in result.split("\n")
                        if line.strip() and len(line.strip()) > 2
                    ]
                    # Filter out any obviously bad terms
                    terms = [t for t in terms if not t.startswith("-") and len(t) < 100]
                    if terms:
                        logger.debug(f"Generated {len(terms)} search terms: {terms[:5]}")
                        return terms[:10]  # Limit to 10 terms
            except asyncio.TimeoutError:
                logger.warning(f"Timeout generating search terms with {model}")
            except Exception as e:
                logger.debug(f"Error generating search terms with {model}: {e}")

        # Fallback: extract key terms from denial text and procedure
        fallback_terms = cls._extract_fallback_terms(denial_text, procedure, diagnosis)
        return fallback_terms

    @classmethod
    def _extract_fallback_terms(
        cls,
        denial_text: str,
        procedure: Optional[str],
        diagnosis: Optional[str],
    ) -> List[str]:
        """Extract basic search terms without ML if models fail."""
        terms: Set[str] = set()

        # Add procedure and diagnosis as terms
        if procedure:
            terms.add(procedure.lower())
        if diagnosis:
            terms.add(diagnosis.lower())

        # Common insurance terms to search for
        common_terms = [
            "medical necessity",
            "prior authorization",
            "appeal",
            "coverage",
            "exclusion",
            "benefit",
        ]
        terms.update(common_terms)

        # Extract potential medical terms from denial text
        if denial_text:
            # Look for quoted terms or capitalized phrases
            quoted = re.findall(r'"([^"]+)"', denial_text)
            terms.update(t.lower() for t in quoted if 3 < len(t) < 50)

        return list(terms)[:10]

    @classmethod
    async def extract_relevant_text(
        cls, denial_id: int, search_terms: List[str]
    ) -> str:
        """
        Extract text from plan documents that matches search terms.

        Args:
            denial_id: The denial ID to get plan documents for
            search_terms: Terms to search for in the documents

        Returns:
            Combined text from matching sections
        """
        if not search_terms:
            return ""

        relevant_sections: List[str] = []
        total_length = 0

        try:
            plan_docs = PlanDocuments.objects.filter(denial_id=denial_id)
            async for doc in plan_docs:
                try:
                    # Get file path
                    file_field = doc.plan_document_enc or doc.plan_document
                    if not file_field:
                        continue

                    path = file_field.path
                    pages_text = cls._extract_pages_with_terms(path, search_terms)

                    for page_text in pages_text:
                        if total_length + len(page_text) > cls.MAX_CONTEXT_LENGTH:
                            break
                        relevant_sections.append(page_text)
                        total_length += len(page_text)

                    if total_length >= cls.MAX_CONTEXT_LENGTH:
                        break

                except Exception as e:
                    logger.debug(f"Error processing plan document: {e}")

        except Exception as e:
            logger.opt(exception=True).debug(
                f"Error getting plan documents for denial {denial_id}: {e}"
            )

        return "\n\n---\n\n".join(relevant_sections)

    @classmethod
    def _extract_pages_with_terms(
        cls, path: str, search_terms: List[str]
    ) -> List[str]:
        """
        Extract pages from a document that contain any of the search terms.

        Args:
            path: Path to the document
            search_terms: Terms to search for

        Returns:
            List of page texts that contain matching terms
        """
        matching_pages: List[str] = []

        if path.lower().endswith(".pdf"):
            try:
                doc = pymupdf.open(path)
                for page in doc:
                    page_text = page.get_text()
                    page_lower = page_text.lower()

                    # Check if any search term appears in this page
                    matches = sum(1 for term in search_terms if term.lower() in page_lower)
                    if matches > 0:
                        # Include page number for context
                        page_num = page.number + 1
                        matching_pages.append(
                            f"[Page {page_num}]\n{page_text}"
                        )
                doc.close()
            except RuntimeError as e:
                logger.warning(f"Error reading PDF {path}: {e}")
        else:
            # Try to read as text file
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    content_lower = content.lower()
                    if any(term.lower() in content_lower for term in search_terms):
                        matching_pages.append(content)
            except Exception as e:
                logger.debug(f"Could not read {path} as text: {e}")

        return matching_pages

    @classmethod
    async def summarize_relevant_sections(
        cls,
        denial: Denial,
        relevant_text: str,
    ) -> Optional[str]:
        """
        Summarize the relevant plan document sections for use in appeal.

        Args:
            denial: The denial object
            relevant_text: Text extracted from plan documents

        Returns:
            Summary of relevant plan information, or None if summarization fails
        """
        if not relevant_text or len(relevant_text.strip()) < 50:
            return None

        procedure = denial.procedure or denial.candidate_procedure or "the denied treatment"
        diagnosis = denial.diagnosis or denial.candidate_diagnosis or ""

        prompt = f"""Summarize the following insurance plan document excerpts that are relevant
to an appeal for: {procedure}
{f'Diagnosis: {diagnosis}' if diagnosis else ''}

Focus on:
1. Coverage criteria and medical necessity requirements
2. Appeal procedures and deadlines
3. Any exclusions that might apply (and potential exceptions)
4. Prior authorization requirements
5. Relevant definitions

Plan document excerpts:
{relevant_text[:cls.MAX_CONTEXT_LENGTH]}

Provide a concise summary (max 500 words) that would help craft an effective appeal.
Include specific page references where helpful."""

        models = ml_router.internal_models_by_cost[:3]

        for model in models:
            try:
                result = await asyncio.wait_for(
                    model._infer_no_context(
                        system_prompts=[
                            "You are an expert at analyzing health insurance plan documents "
                            "to help patients and providers craft effective appeals. "
                            "Provide clear, actionable summaries focused on what supports the appeal."
                        ],
                        prompt=prompt,
                        temperature=0.3,
                    ),
                    timeout=45,
                )
                if result and len(result) > 50:
                    logger.debug(f"Generated plan document summary ({len(result)} chars)")
                    return result
            except asyncio.TimeoutError:
                logger.warning(f"Timeout summarizing plan docs with {model}")
            except Exception as e:
                logger.debug(f"Error summarizing plan docs with {model}: {e}")

        return None

    @classmethod
    async def generate_plan_documents_summary(cls, denial_id: int) -> Optional[str]:
        """
        Main entry point: Generate a summary of relevant plan document sections.

        This method:
        1. Generates search terms based on the denial
        2. Extracts relevant pages from plan documents
        3. Summarizes those sections for use in appeal generation

        Args:
            denial_id: The denial ID to process

        Returns:
            Summary of relevant plan document sections, or None if no documents or failure
        """
        try:
            denial = await Denial.objects.filter(denial_id=denial_id).aget()

            # Check if we have plan documents
            has_docs = await PlanDocuments.objects.filter(denial_id=denial_id).aexists()
            if not has_docs:
                logger.debug(f"No plan documents for denial {denial_id}")
                return None

            # Check if we already have a summary
            if denial.plan_documents_summary:
                logger.debug(f"Denial {denial_id} already has plan documents summary")
                return denial.plan_documents_summary

            async with asyncio.timeout(cls.TIMEOUT_SECONDS):
                # Step 1: Generate search terms
                search_terms = await cls.generate_search_terms(
                    denial_text=denial.denial_text or "",
                    procedure=denial.procedure or denial.candidate_procedure,
                    diagnosis=denial.diagnosis or denial.candidate_diagnosis,
                )

                if not search_terms:
                    logger.debug(f"No search terms generated for denial {denial_id}")
                    return None

                # Step 2: Extract relevant text from plan documents
                relevant_text = await cls.extract_relevant_text(denial_id, search_terms)

                if not relevant_text:
                    logger.debug(f"No relevant text found in plan docs for denial {denial_id}")
                    return None

                # Step 3: Summarize the relevant sections
                summary = await cls.summarize_relevant_sections(denial, relevant_text)

                if summary:
                    # Save to denial object
                    await Denial.objects.filter(denial_id=denial_id).aupdate(
                        plan_documents_summary=summary
                    )
                    logger.info(
                        f"Generated plan documents summary for denial {denial_id}"
                    )
                    return summary

        except asyncio.TimeoutError:
            logger.warning(
                f"Timeout generating plan documents summary for denial {denial_id}"
            )
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error generating plan documents summary for denial {denial_id}: {e}"
            )

        return None

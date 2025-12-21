"""
Helper for analyzing policy documents to extract relevant coverage information.

Uses internal ML models to:
1. Extract text from policy documents (Summary of Benefits, Medical Policy PDFs)
2. Identify exclusions and inclusions
3. Find appeal-relevant clauses with page references
4. Generate quotable summaries for use in appeals
"""

import asyncio
import json
import re
from typing import Optional, List, Dict, Any
from loguru import logger

import pymupdf

from fighthealthinsurance.models import PolicyDocument, PolicyDocumentAnalysis
from fighthealthinsurance.ml.ml_router import ml_router


# Disclaimer text to include in all outputs
POLICY_ANALYSIS_DISCLAIMER = """**Important Disclaimer:** This analysis is provided for informational purposes only and is not legal or professional advice. Insurance policies are complex documents, and this AI analysis may not capture all nuances. For definitive interpretations, contact your insurance company directly or consult with a qualified professional."""


class MLPolicyDocHelper:
    """Helper class for ML-powered policy document analysis."""

    # Maximum characters to send to the model for analysis
    MAX_CONTEXT_LENGTH = 12000
    # Maximum time to spend on document analysis
    TIMEOUT_SECONDS = 90

    @classmethod
    def extract_text_from_pdf(cls, file_path: str) -> tuple[str, Dict[int, str]]:
        """
        Extract text from a PDF file with page number tracking.

        Args:
            file_path: Path to the PDF file

        Returns:
            Tuple of (full_text, page_dict) where page_dict maps page numbers to text
        """
        full_text = ""
        page_dict: Dict[int, str] = {}

        try:
            with pymupdf.open(file_path) as doc:
                for page_num, page in enumerate(doc, start=1):
                    page_text = page.get_text()
                    if page_text.strip():
                        page_dict[page_num] = page_text
                        full_text += f"\n\n[Page {page_num}]\n{page_text}"
        except Exception as e:
            logger.warning(f"Error extracting text from PDF {file_path}: {e}")

        return full_text, page_dict

    @classmethod
    async def analyze_policy_document(
        cls,
        policy_document: PolicyDocument,
        user_question: Optional[str] = None,
    ) -> Optional[PolicyDocumentAnalysis]:
        """
        Main entry point: Analyze a policy document and store the results.

        Args:
            policy_document: The PolicyDocument model instance
            user_question: Optional specific question from the user

        Returns:
            PolicyDocumentAnalysis instance with the analysis results
        """
        try:
            # Get the file path
            file_field = policy_document.document_enc or policy_document.document
            if not file_field:
                logger.warning(f"No document file for PolicyDocument {policy_document.id}")
                return None

            file_path = file_field.path

            async with asyncio.timeout(cls.TIMEOUT_SECONDS):
                # Step 1: Extract text from PDF
                full_text, page_dict = cls.extract_text_from_pdf(file_path)

                if not full_text or len(full_text.strip()) < 100:
                    logger.warning(f"Insufficient text extracted from {policy_document.filename}")
                    return None

                # Store raw text on the document
                policy_document.raw_text = full_text[:50000]  # Limit storage size
                await policy_document.asave()

                # Step 2: Analyze for exclusions, inclusions, and appeal clauses
                analysis_results = await cls._analyze_document_content(
                    full_text, page_dict, user_question
                )

                if not analysis_results:
                    logger.warning(f"No analysis results for {policy_document.filename}")
                    return None

                # Step 3: Create and save the analysis record
                analysis = await PolicyDocumentAnalysis.objects.acreate(
                    policy_document=policy_document,
                    user_question=user_question or "",
                    exclusions=analysis_results.get("exclusions", []),
                    inclusions=analysis_results.get("inclusions", []),
                    appeal_clauses=analysis_results.get("appeal_clauses", []),
                    summary=analysis_results.get("summary", ""),
                    quotable_sections=analysis_results.get("quotable_sections", []),
                )

                logger.info(
                    f"Created PolicyDocumentAnalysis {analysis.id} for document {policy_document.filename}"
                )
                return analysis

        except asyncio.TimeoutError:
            logger.warning(
                f"Timeout analyzing policy document {policy_document.id}"
            )
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error analyzing policy document {policy_document.id}: {e}"
            )

        return None

    @classmethod
    async def _analyze_document_content(
        cls,
        full_text: str,
        page_dict: Dict[int, str],
        user_question: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Use LLM to analyze document content for exclusions, inclusions, and appeal clauses.

        Args:
            full_text: Full extracted text from the document
            page_dict: Dict mapping page numbers to their text
            user_question: Optional specific question from the user

        Returns:
            Dict with analysis results
        """
        # Truncate text if needed
        text_for_analysis = full_text[:cls.MAX_CONTEXT_LENGTH]

        question_context = ""
        if user_question:
            question_context = f"""
The user has a specific question about this policy:
"{user_question}"

Please pay special attention to sections relevant to this question.
"""

        prompt = f"""Analyze this insurance policy document and extract the following information.
{question_context}
IMPORTANT: For each item you extract, include the exact page number where it appears.

Extract:
1. **EXCLUSIONS**: List all exclusions (things NOT covered). For each, provide:
   - The exclusion text (exact quote when possible)
   - The page number
   - A brief explanation of what it means

2. **INCLUSIONS/COVERAGE**: List what IS covered. For each, provide:
   - The coverage text (exact quote when possible)
   - The page number
   - A brief explanation

3. **APPEAL-RELEVANT CLAUSES**: Find clauses that would be useful for appealing a denial:
   - Appeal deadlines and procedures
   - Medical necessity definitions
   - Prior authorization requirements
   - Exceptions to exclusions
   - Grievance procedures
   For each, provide the exact quote and page number.

4. **QUOTABLE SECTIONS**: Extract 3-5 key quotes that could be cited in an appeal letter.
   Format as: "Quote text" (Page X)

5. **SUMMARY**: A brief (2-3 paragraph) plain-English summary of the key coverage and limitations.

Document text:
{text_for_analysis}

Respond in JSON format with the following structure:
{{
    "exclusions": [
        {{"text": "...", "page": 1, "explanation": "..."}}
    ],
    "inclusions": [
        {{"text": "...", "page": 1, "explanation": "..."}}
    ],
    "appeal_clauses": [
        {{"text": "...", "page": 1, "type": "appeal_deadline|medical_necessity|prior_auth|exception|grievance"}}
    ],
    "quotable_sections": [
        {{"quote": "...", "page": 1, "relevance": "..."}}
    ],
    "summary": "..."
}}
"""

        models = ml_router.internal_models_by_cost[:3]

        for model in models:
            try:
                result = await asyncio.wait_for(
                    model._infer_no_context(
                        system_prompts=[
                            "You are an expert at analyzing health insurance policy documents. "
                            "Your goal is to help users understand their coverage and find "
                            "information useful for appealing insurance denials. "
                            "Always include exact page references. Be thorough but concise."
                        ],
                        prompt=prompt,
                        temperature=0.2,
                    ),
                    timeout=60,
                )

                if result:
                    # Try to parse JSON from the response
                    parsed = cls._parse_analysis_response(result)
                    if parsed:
                        logger.debug(f"Successfully analyzed document with {model}")
                        return parsed

            except asyncio.TimeoutError:
                logger.warning(f"Timeout analyzing document with {model}")
            except Exception as e:
                logger.debug(f"Error analyzing document with {model}: {e}")

        return None

    @classmethod
    def _parse_analysis_response(cls, response: str) -> Optional[Dict[str, Any]]:
        """
        Parse the JSON response from the LLM.

        Args:
            response: Raw response string from LLM

        Returns:
            Parsed dict or None if parsing fails
        """
        try:
            # Try to find JSON in the response
            json_match = re.search(r"\{[\s\S]*\}", response)
            if json_match:
                parsed = json.loads(json_match.group(0))

                # Validate structure
                required_keys = ["exclusions", "inclusions", "appeal_clauses", "summary"]
                if all(key in parsed for key in required_keys):
                    return parsed

        except json.JSONDecodeError as e:
            logger.debug(f"JSON parsing error: {e}")
        except Exception as e:
            logger.debug(f"Error parsing analysis response: {e}")

        return None

    @classmethod
    def format_analysis_for_chat(
        cls, analysis: PolicyDocumentAnalysis, include_disclaimer: bool = True
    ) -> str:
        """
        Format the analysis results for display in chat.

        Args:
            analysis: The PolicyDocumentAnalysis instance
            include_disclaimer: Whether to include the legal disclaimer

        Returns:
            Formatted markdown string
        """
        output_parts = []

        # Summary first
        if analysis.summary:
            output_parts.append("## Summary\n")
            output_parts.append(analysis.summary)
            output_parts.append("\n")

        # Key exclusions
        if analysis.exclusions:
            output_parts.append("\n## Key Exclusions (What's NOT Covered)\n")
            for i, exc in enumerate(analysis.exclusions[:5], 1):
                if isinstance(exc, dict):
                    page = exc.get("page", "?")
                    text = exc.get("text", exc.get("explanation", ""))
                    explanation = exc.get("explanation", "")
                    output_parts.append(f"{i}. **Page {page}**: {text}")
                    if explanation and explanation != text:
                        output_parts.append(f"   - *{explanation}*")
                    output_parts.append("\n")

        # Key inclusions
        if analysis.inclusions:
            output_parts.append("\n## Key Coverage (What IS Covered)\n")
            for i, inc in enumerate(analysis.inclusions[:5], 1):
                if isinstance(inc, dict):
                    page = inc.get("page", "?")
                    text = inc.get("text", inc.get("explanation", ""))
                    output_parts.append(f"{i}. **Page {page}**: {text}\n")

        # Appeal-relevant clauses
        if analysis.appeal_clauses:
            output_parts.append("\n## Useful for Appeals\n")
            for i, clause in enumerate(analysis.appeal_clauses[:5], 1):
                if isinstance(clause, dict):
                    page = clause.get("page", "?")
                    text = clause.get("text", "")
                    clause_type = clause.get("type", "general")
                    output_parts.append(f"{i}. **{clause_type.replace('_', ' ').title()}** (Page {page}): \"{text}\"\n")

        # Quotable sections
        if analysis.quotable_sections:
            output_parts.append("\n## Quotable Sections for Your Appeal\n")
            output_parts.append("You can use these exact quotes in your appeal letter:\n\n")
            for i, quote in enumerate(analysis.quotable_sections[:5], 1):
                if isinstance(quote, dict):
                    quote_text = quote.get("quote", "")
                    page = quote.get("page", "?")
                    relevance = quote.get("relevance", "")
                    output_parts.append(f"> \"{quote_text}\" *(Page {page})*\n")
                    if relevance:
                        output_parts.append(f"   - {relevance}\n")
                    output_parts.append("\n")

        # Add disclaimer
        if include_disclaimer:
            output_parts.append("\n---\n")
            output_parts.append(POLICY_ANALYSIS_DISCLAIMER)

        return "".join(output_parts)

    @classmethod
    async def get_or_create_analysis(
        cls,
        policy_document: PolicyDocument,
        user_question: Optional[str] = None,
    ) -> Optional[PolicyDocumentAnalysis]:
        """
        Get existing analysis or create a new one.

        Args:
            policy_document: The PolicyDocument to analyze
            user_question: Optional specific question

        Returns:
            PolicyDocumentAnalysis instance
        """
        # Check for existing analysis
        existing = await PolicyDocumentAnalysis.objects.filter(
            policy_document=policy_document
        ).afirst()

        if existing:
            logger.debug(f"Found existing analysis for document {policy_document.id}")
            return existing

        # Create new analysis
        return await cls.analyze_policy_document(policy_document, user_question)

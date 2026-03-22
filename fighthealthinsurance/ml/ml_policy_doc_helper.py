"""
Helper for analyzing policy documents to extract relevant coverage information.

Uses internal ML models to:
1. Extract text from policy documents (Summary of Benefits, Medical Policy PDFs/DOCX)
2. Identify exclusions and inclusions
3. Find appeal-relevant clauses with page references
4. Generate quotable summaries for use in appeals
"""

import asyncio
import io
import json
from typing import Optional, Dict, Any
from loguru import logger

import pymupdf

from django_encrypted_filefield.crypt import Cryptographer

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
    def _read_and_decrypt_file(cls, file_field: Any) -> Optional[bytes]:
        """
        Read and decrypt bytes from an EncryptedFileField.

        Returns:
            Decrypted bytes if successful, None on failure.
        """
        try:
            with file_field.open() as f:
                encrypted_bytes: bytes = f.read()
                if not encrypted_bytes:
                    return None
                try:
                    return Cryptographer.decrypted(encrypted_bytes)
                except Exception:
                    # Decryption failed — file may have been stored unencrypted
                    logger.debug("Decryption failed, returning raw bytes as fallback")
                    return encrypted_bytes
        except Exception as e:
            logger.warning(f"Error reading encrypted file: {e}")
            return None

    @classmethod
    def _extract_text_from_pdf_bytes(cls, data: bytes) -> tuple[str, Dict[int, str]]:
        """
        Extract text from PDF bytes with page number tracking.

        Returns:
            Tuple of (full_text, page_dict) where page_dict maps page numbers to text
        """
        full_text = ""
        page_dict: Dict[int, str] = {}

        try:
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                for page_num, page in enumerate(doc, start=1):
                    page_text = page.get_text()
                    if page_text.strip():
                        page_dict[page_num] = page_text
                        full_text += f"\n\n[Page {page_num}]\n{page_text}"
        except Exception as e:
            logger.warning(f"Error extracting text from PDF bytes: {e}")

        return full_text, page_dict

    @classmethod
    def _extract_text_from_docx_bytes(cls, data: bytes) -> tuple[str, Dict[int, str]]:
        """
        Extract text from DOCX bytes.

        Returns:
            Tuple of (full_text, page_dict). DOCX does not have reliable page
            numbers, so page_dict uses paragraph-group indices as pseudo-pages.
        """
        full_text = ""
        page_dict: Dict[int, str] = {}

        try:
            import docx

            doc = docx.Document(io.BytesIO(data))
            # Group paragraphs into pseudo-pages (~3000 chars each)
            current_page = 1
            current_page_text = ""
            for para in doc.paragraphs:
                text = para.text.strip()
                if not text:
                    continue
                current_page_text += text + "\n"
                if len(current_page_text) > 3000:
                    page_dict[current_page] = current_page_text
                    full_text += f"\n\n[Section {current_page}]\n{current_page_text}"
                    current_page += 1
                    current_page_text = ""
            # Add remaining text
            if current_page_text.strip():
                page_dict[current_page] = current_page_text
                full_text += f"\n\n[Section {current_page}]\n{current_page_text}"
        except Exception as e:
            logger.warning(f"Error extracting text from DOCX bytes: {e}")

        return full_text, page_dict

    @classmethod
    def _extract_text_from_plaintext_bytes(
        cls, data: bytes
    ) -> tuple[str, Dict[int, str]]:
        """
        Extract text from plain text file bytes.

        Returns:
            Tuple of (full_text, page_dict) with a single pseudo-page.
        """
        full_text = ""
        page_dict: Dict[int, str] = {}

        try:
            content = data.decode("utf-8", errors="replace")
            if content.strip():
                page_dict[1] = content
                full_text = f"\n\n[Section 1]\n{content}"
        except Exception as e:
            logger.warning(f"Error reading plaintext bytes: {e}")

        return full_text, page_dict

    @classmethod
    def extract_text_from_bytes(
        cls, data: bytes, filename: str
    ) -> tuple[str, Dict[int, str]]:
        """
        Extract text from document bytes, dispatching by filename extension.

        Returns:
            Tuple of (full_text, page_dict)
        """
        lower_name = filename.lower()
        if lower_name.endswith(".pdf"):
            return cls._extract_text_from_pdf_bytes(data)
        elif lower_name.endswith(".docx"):
            return cls._extract_text_from_docx_bytes(data)
        elif lower_name.endswith(".txt"):
            return cls._extract_text_from_plaintext_bytes(data)
        elif lower_name.endswith((".doc", ".rtf")):
            logger.warning(
                f"Unsupported legacy format for text extraction: {filename}. "
                "Only .pdf, .docx, and .txt files are supported."
            )
            return "", {}
        else:
            logger.warning(f"Unsupported file type for text extraction: {filename}")
            return "", {}

    @classmethod
    async def analyze_policy_document(
        cls,
        policy_document: PolicyDocument,
        user_question: Optional[str] = None,
    ) -> Optional[PolicyDocumentAnalysis]:
        """
        Main entry point: Analyze a policy document and store the results.
        """
        try:
            file_field = policy_document.document_enc
            if not file_field:
                logger.warning(
                    f"No document file for PolicyDocument {policy_document.id}"
                )
                return None

            async with asyncio.timeout(cls.TIMEOUT_SECONDS):
                # Step 1: Read and decrypt the file, then extract text
                decrypted_bytes = await asyncio.to_thread(
                    cls._read_and_decrypt_file, file_field
                )
                if not decrypted_bytes:
                    logger.warning(
                        f"Could not read/decrypt document for PolicyDocument {policy_document.id}"
                    )
                    return None

                full_text, _ = await asyncio.to_thread(
                    cls.extract_text_from_bytes,
                    decrypted_bytes,
                    policy_document.filename,
                )

                if not full_text or len(full_text.strip()) < 100:
                    logger.warning(
                        f"Insufficient text extracted from {policy_document.filename}"
                    )
                    return None

                # Step 2: Analyze for exclusions, inclusions, and appeal clauses
                analysis_results = await cls._analyze_document_content(
                    full_text, user_question
                )

                if not analysis_results:
                    logger.warning(
                        f"No analysis results for {policy_document.filename}"
                    )
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
            logger.warning(f"Timeout analyzing policy document {policy_document.id}")
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error analyzing policy document {policy_document.id}: {e}"
            )

        return None

    @classmethod
    async def _analyze_document_content(
        cls,
        full_text: str,
        user_question: Optional[str] = None,
        remaining_timeout: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Use LLM to analyze document content for exclusions, inclusions, and appeal clauses.
        """
        text_for_analysis = full_text[: cls.MAX_CONTEXT_LENGTH]

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
        num_models = len(models)

        # Compute per-model timeout from remaining outer budget
        if remaining_timeout and remaining_timeout > 0:
            per_model_timeout = remaining_timeout / max(num_models, 1)
        else:
            per_model_timeout = cls.TIMEOUT_SECONDS / max(num_models, 1)

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
                    timeout=per_model_timeout,
                )

                if result:
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
        Uses json.JSONDecoder.raw_decode to avoid greedy regex issues.
        """
        try:
            # First, try parsing the entire response as JSON
            parsed: Dict[str, Any] = json.loads(response)
            if cls._validate_analysis_structure(parsed):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

        # Fall back to finding the first valid JSON object
        try:
            start = response.index("{")
            decoder = json.JSONDecoder()
            parsed, _ = decoder.raw_decode(response, start)
            if isinstance(parsed, dict) and cls._validate_analysis_structure(parsed):
                return parsed
        except (ValueError, json.JSONDecodeError) as e:
            logger.debug(f"JSON parsing error: {e}")

        return None

    @classmethod
    def _validate_analysis_structure(cls, parsed: Dict[str, Any]) -> bool:
        """Validate that parsed JSON has the expected top-level structure."""
        required_keys = ["exclusions", "inclusions", "appeal_clauses", "summary"]
        return all(key in parsed for key in required_keys)

    @classmethod
    def format_analysis_for_chat(
        cls, analysis: PolicyDocumentAnalysis, include_disclaimer: bool = True
    ) -> str:
        """Format the analysis results for display in chat."""
        output_parts: list[str] = []

        if analysis.summary:
            output_parts.append("## Summary\n")
            output_parts.append(analysis.summary)
            output_parts.append("\n")

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

        if analysis.inclusions:
            output_parts.append("\n## Key Coverage (What IS Covered)\n")
            for i, inc in enumerate(analysis.inclusions[:5], 1):
                if isinstance(inc, dict):
                    page = inc.get("page", "?")
                    text = inc.get("text", inc.get("explanation", ""))
                    output_parts.append(f"{i}. **Page {page}**: {text}\n")

        if analysis.appeal_clauses:
            output_parts.append("\n## Useful for Appeals\n")
            for i, clause in enumerate(analysis.appeal_clauses[:5], 1):
                if isinstance(clause, dict):
                    page = clause.get("page", "?")
                    text = clause.get("text", "")
                    clause_type = clause.get("type", "general")
                    output_parts.append(
                        f'{i}. **{clause_type.replace("_", " ").title()}** (Page {page}): "{text}"\n'
                    )

        if analysis.quotable_sections:
            output_parts.append("\n## Quotable Sections for Your Appeal\n")
            output_parts.append(
                "You can use these exact quotes in your appeal letter:\n\n"
            )
            for quote in analysis.quotable_sections[:5]:
                if isinstance(quote, dict):
                    quote_text = quote.get("quote", "")
                    page = quote.get("page", "?")
                    relevance = quote.get("relevance", "")
                    output_parts.append(f'> "{quote_text}" *(Page {page})*\n')
                    if relevance:
                        output_parts.append(f"   - {relevance}\n")
                    output_parts.append("\n")

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
        Includes user_question in cache lookup so different questions get fresh analysis.
        When user_question is None, prefer a generic (no-question) analysis.
        """
        lookup: Dict[str, Any] = {"policy_document": policy_document}
        if user_question:
            lookup["user_question"] = user_question
        else:
            lookup["user_question__isnull"] = True

        existing = await PolicyDocumentAnalysis.objects.filter(**lookup).afirst()

        # Fallback: if no null-question analysis exists, try empty string
        if not existing and not user_question:
            existing = await PolicyDocumentAnalysis.objects.filter(
                policy_document=policy_document, user_question=""
            ).afirst()

        if existing:
            logger.debug(f"Found existing analysis for document {policy_document.id}")
            return existing

        return await cls.analyze_policy_document(policy_document, user_question)

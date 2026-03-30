"""
Helper for analyzing policy documents to extract relevant coverage information.

Uses internal ML models to:
1. Extract text from policy documents (Summary of Benefits, Medical Policy PDFs/DOCX)
2. Run targeted regex search to find pages likely to contain key clauses
3. Split documents into chunks and analyze them in parallel (targeted + scattershot)
4. Synthesize chunk summaries into a comprehensive analysis with page references
5. Generate quotable summaries for use in appeals
"""

import asyncio
import io
import json
import re
from typing import Optional, Dict, Any, Callable, Awaitable, Set

from loguru import logger

import pymupdf

from django.db import IntegrityError
from django_encrypted_filefield.crypt import Cryptographer

from fighthealthinsurance.models import PolicyDocument, PolicyDocumentAnalysis
from fighthealthinsurance.ml.ml_router import ml_router

# Disclaimer text to include in all outputs
POLICY_ANALYSIS_DISCLAIMER = (
    "**Important Disclaimer:** This analysis is provided for informational purposes "
    "only and is not legal or professional advice. Insurance policies are complex "
    "documents, and this AI analysis may not capture all nuances. **We strongly "
    "encourage you to verify these findings against your actual policy document "
    "using the page references provided.** For definitive interpretations, contact "
    "your insurance company directly or consult with a qualified professional."
)

# Type alias for the progress callback
ProgressCallback = Callable[[int, int], Awaitable[None]]

# Regex patterns for finding insurance-relevant sections.
# Each tuple is (pattern, description) — description is used in logging.
_INSURANCE_SEARCH_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"exclusion", re.IGNORECASE), "exclusions"),
    (re.compile(r"not\s+cover", re.IGNORECASE), "coverage exclusions"),
    (re.compile(r"limitation", re.IGNORECASE), "limitations"),
    (re.compile(r"appeal", re.IGNORECASE), "appeal rights"),
    (re.compile(r"grievance", re.IGNORECASE), "grievance procedures"),
    (re.compile(r"medical\s+necessity", re.IGNORECASE), "medical necessity"),
    (re.compile(r"prior\s+auth", re.IGNORECASE), "prior authorization"),
    (re.compile(r"pre-?authorization", re.IGNORECASE), "preauthorization"),
    (re.compile(r"out.of.pocket", re.IGNORECASE), "out-of-pocket costs"),
    (re.compile(r"deductible", re.IGNORECASE), "deductibles"),
    (re.compile(r"co-?pay", re.IGNORECASE), "copayments"),
    (re.compile(r"coinsurance", re.IGNORECASE), "coinsurance"),
    (re.compile(r"formulary", re.IGNORECASE), "drug formulary"),
    (re.compile(r"external\s+review", re.IGNORECASE), "external review"),
    (re.compile(r"independent\s+review", re.IGNORECASE), "independent review"),
    (re.compile(r"experimental", re.IGNORECASE), "experimental treatment"),
    (re.compile(r"investigational", re.IGNORECASE), "investigational treatment"),
    (re.compile(r"benefit\s+period", re.IGNORECASE), "benefit period"),
    (re.compile(r"waiting\s+period", re.IGNORECASE), "waiting period"),
    (re.compile(r"pre-?existing", re.IGNORECASE), "pre-existing conditions"),
    (re.compile(r"emergency", re.IGNORECASE), "emergency services"),
    (re.compile(r"urgent\s+care", re.IGNORECASE), "urgent care"),
    (re.compile(r"claim\s+(?:filing|submission)", re.IGNORECASE), "claim filing"),
    (re.compile(r"time\s*limit", re.IGNORECASE), "time limits"),
    (re.compile(r"denied|denial", re.IGNORECASE), "denial language"),
]

# Common stopwords to exclude when extracting keywords from user questions.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "are",
        "from",
        "have",
        "has",
        "was",
        "were",
        "been",
        "will",
        "would",
        "could",
        "should",
        "can",
        "may",
        "does",
        "what",
        "how",
        "why",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "not",
        "but",
        "about",
        "into",
        "your",
        "you",
        "our",
        "their",
        "its",
        "his",
        "her",
        "any",
        "all",
        "also",
        "other",
        "than",
        "then",
        "some",
        "more",
        "most",
        "such",
        "only",
        "each",
        "very",
        "just",
        "there",
        "here",
        "insurance",
        "policy",
        "plan",
        "cover",
        "covered",
        "coverage",
        "health",
        "medical",
    }
)


class MLPolicyDocHelper:
    """Helper class for ML-powered policy document analysis."""

    # Characters per chunk for parallel analysis
    CHUNK_SIZE = 4000
    # Maximum parallel chunk analysis tasks
    MAX_PARALLEL_CHUNKS = 50
    # Maximum time for the entire analysis pipeline
    TIMEOUT_SECONDS = 180
    # Timeout for each individual chunk analysis
    CHUNK_TIMEOUT_SECONDS = 60
    # Timeout for the final synthesis call
    SYNTHESIS_TIMEOUT_SECONDS = 90

    @classmethod
    async def _infer_with_fallback(
        cls,
        system_prompts: list[str],
        prompt: str,
        temperature: float,
        timeout: float,
        model_count: int = 3,
        label: str = "",
    ) -> Optional[str]:
        """
        Try inference across multiple models with timeout, returning the first
        successful result or None if all models fail.
        """
        models = ml_router.internal_models_by_cost[:model_count]
        for model in models:
            try:
                result = await asyncio.wait_for(
                    model._infer_no_context(
                        system_prompts=system_prompts,
                        prompt=prompt,
                        temperature=temperature,
                    ),
                    timeout=timeout,
                )
                if result:
                    return str(result)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout on {label} with {model}")
            except Exception as e:
                logger.debug(f"Error on {label} with {model}: {e}")
        return None

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
                    return bytes(Cryptographer.decrypted(encrypted_bytes))
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
        else:
            logger.warning(f"Unsupported file type for text extraction: {filename}")
            return "", {}

    @classmethod
    def _build_chunks(cls, page_dict: Dict[int, str]) -> list[Dict[str, Any]]:
        """
        Build analysis chunks from extracted page_dict.

        Groups pages/sections into chunks of approximately CHUNK_SIZE characters.
        Each chunk records which pages it spans.
        """
        chunks: list[Dict[str, Any]] = []
        current_text = ""
        current_pages: list[int] = []

        for page_num in sorted(page_dict.keys()):
            page_text = page_dict[page_num]
            # If adding this page would exceed chunk size, flush current chunk
            if current_text and len(current_text) + len(page_text) > cls.CHUNK_SIZE:
                chunks.append({"text": current_text, "pages": list(current_pages)})
                current_text = ""
                current_pages = []
            current_text += page_text + "\n"
            current_pages.append(page_num)

        # Flush remaining
        if current_text.strip():
            chunks.append({"text": current_text, "pages": list(current_pages)})

        return chunks

    @classmethod
    def _regex_search_pages(
        cls,
        page_dict: Dict[int, str],
        user_question: Optional[str] = None,
    ) -> Dict[int, Set[str]]:
        """
        Scan every page with insurance-relevant regex patterns.

        Returns a dict mapping page_number -> set of matched pattern descriptions.
        Pages with more matches are higher priority for targeted analysis.
        """
        page_hits: Dict[int, Set[str]] = {}

        # Build extra patterns from user question keywords (3+ char words)
        extra_patterns: list[tuple[re.Pattern[str], str]] = []
        if user_question:
            words = set(
                w
                for w in re.findall(r"[a-zA-Z]{3,}", user_question)
                if w.lower() not in _STOPWORDS
            )
            for word in words:
                try:
                    pat = re.compile(re.escape(word), re.IGNORECASE)
                    extra_patterns.append((pat, f"user query: {word}"))
                except re.error:
                    pass

        all_patterns = _INSURANCE_SEARCH_PATTERNS + extra_patterns

        for page_num, page_text in page_dict.items():
            for pattern, desc in all_patterns:
                if pattern.search(page_text):
                    if page_num not in page_hits:
                        page_hits[page_num] = set()
                    page_hits[page_num].add(desc)

        return page_hits

    @classmethod
    def _build_targeted_chunks(
        cls,
        page_dict: Dict[int, str],
        page_hits: Dict[int, Set[str]],
        exclude_pages: Set[int],
    ) -> list[Dict[str, Any]]:
        """
        Build chunks from regex-hit pages, excluding any in *exclude_pages*.
        Each targeted chunk is tagged with what patterns matched.
        """
        targeted_pages = {
            p for p in page_hits if p not in exclude_pages and p in page_dict
        }
        if not targeted_pages:
            return []

        # Reuse _build_chunks on the filtered page subset
        filtered_page_dict = {p: page_dict[p] for p in targeted_pages}
        chunks = cls._build_chunks(filtered_page_dict)

        # Enrich each chunk with the search hits from its pages
        for chunk in chunks:
            hits: Set[str] = set()
            for page_num in chunk["pages"]:
                hits.update(page_hits.get(page_num, set()))
            chunk["search_hits"] = list(hits)

        return chunks

    @classmethod
    async def analyze_policy_document(
        cls,
        policy_document: PolicyDocument,
        user_question: Optional[str] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Optional[PolicyDocumentAnalysis]:
        """
        Main entry point: Analyze a policy document using two parallel strategies.

        Phase 1 (parallel):
          a) Targeted search — regex patterns find pages with insurance keywords,
             those pages get sent to analysis agents with extra context about what matched.
          b) Scattershot — the full document is chunked and all chunks are analyzed
             for relevance.

        Phase 2: All relevant summaries from both strategies are deduplicated and
        synthesized into a single structured analysis with page references.
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

                full_text, page_dict = await asyncio.to_thread(
                    cls.extract_text_from_bytes,
                    decrypted_bytes,
                    policy_document.filename,
                )

                if not full_text or len(full_text.strip()) < 100:
                    logger.warning(
                        f"Insufficient text extracted from {policy_document.filename}"
                    )
                    return None

                # Step 2: Run regex search, then build targeted chunks first
                page_hits = cls._regex_search_pages(page_dict, user_question)

                # Build targeted chunks from regex-hit pages (these get priority)
                targeted_chunks = cls._build_targeted_chunks(
                    page_dict, page_hits, set()
                )

                # Compute pages already covered by targeted chunks
                targeted_pages: Set[int] = set()
                for chunk in targeted_chunks:
                    targeted_pages.update(chunk["pages"])

                # Build scattershot chunks only from remaining pages
                remaining_page_dict = {
                    p: page_dict[p] for p in page_dict if p not in targeted_pages
                }
                scattershot_chunks = cls._build_chunks(remaining_page_dict)

                all_chunks = targeted_chunks + scattershot_chunks
                if not all_chunks:
                    logger.warning(f"No chunks built from {policy_document.filename}")
                    return None

                logger.info(
                    f"Analyzing {policy_document.filename}: "
                    f"{len(scattershot_chunks)} scattershot chunks + "
                    f"{len(targeted_chunks)} targeted chunks from "
                    f"{len(page_hits)} regex-hit pages"
                )

                # Step 3: Analyze all chunks in parallel
                plan_category = getattr(policy_document, "plan_category", "unknown")
                chunk_summaries = await cls._analyze_chunks_parallel(
                    all_chunks,
                    user_question=user_question,
                    plan_category=plan_category,
                    progress_callback=progress_callback,
                )

                if not chunk_summaries:
                    logger.warning(
                        f"No relevant chunks found in {policy_document.filename}"
                    )
                    return None

                logger.info(
                    f"Got {len(chunk_summaries)} relevant summaries from "
                    f"{len(all_chunks)} total chunks"
                )

                # Step 4: Synthesize chunk summaries into final analysis
                if progress_callback:
                    await progress_callback(0, 1)

                analysis_results = await cls._synthesize_chunk_summaries(
                    chunk_summaries,
                    user_question=user_question,
                    plan_category=plan_category,
                )

                if progress_callback:
                    await progress_callback(0, 0)

                if not analysis_results:
                    logger.warning(f"Synthesis failed for {policy_document.filename}")
                    return None

                # Step 5: Create and save the analysis record
                normalized_question = user_question or ""
                try:
                    analysis = await PolicyDocumentAnalysis.objects.acreate(
                        policy_document=policy_document,
                        user_question=normalized_question,
                        exclusions=analysis_results.get("exclusions", []),
                        inclusions=analysis_results.get("inclusions", []),
                        appeal_clauses=analysis_results.get("appeal_clauses", []),
                        summary=analysis_results.get("summary", ""),
                        quotable_sections=analysis_results.get("quotable_sections", []),
                    )
                except IntegrityError:
                    # Race condition: another request created the analysis first
                    logger.debug(
                        f"Duplicate analysis for {policy_document.id}, fetching existing"
                    )
                    analysis = await PolicyDocumentAnalysis.objects.filter(
                        policy_document=policy_document,
                        user_question=normalized_question,
                    ).afirst()
                    if analysis:
                        return analysis
                    return None

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

    # Map plan categories to their regulatory context for the LLM prompt
    PLAN_REGULATOR_CONTEXT: Dict[str, str] = {
        "employer_erisa": "This is an ERISA employer-sponsored plan regulated by the U.S. Department of Labor. Appeals go through the DOL/EBSA.",
        "employer_non_erisa": "This is a non-ERISA employer plan (government or church employer). Appeals are typically handled by the state insurance department.",
        "aca_marketplace": "This is an ACA Marketplace plan. Appeals may go through HHS or the state insurance department/exchange.",
        "medicare_traditional": "This is a Traditional Medicare plan regulated by CMS. Appeals follow the Medicare appeals process (redetermination, reconsideration, ALJ, etc.).",
        "medicare_advantage": "This is a Medicare Advantage (Part C) plan regulated by CMS. Appeals go through the plan first, then to an Independent Review Entity (IRE).",
        "medicaid_chip": "This is a Medicaid/CHIP plan regulated by the state Medicaid agency and CMS. Appeals go through the state fair hearing process.",
        "tricare": "This is a TRICARE military health plan. Appeals go through the TRICARE appeals process.",
        "va": "This is VA Health Care. Appeals go through the VA's Clinical Appeals process.",
        "individual_off_exchange": "This is an individual off-exchange plan. Appeals are typically handled by the state insurance department.",
        "short_term": "This is a short-term health plan. These may have limited appeal rights; check state regulations.",
    }

    @classmethod
    async def _analyze_single_chunk(
        cls,
        chunk: Dict[str, Any],
        chunk_index: int,
        user_question: Optional[str],
        plan_category: str,
        remaining_counter: asyncio.Lock,
        remaining_count: list[int],
        total_chunks: int,
        progress_callback: Optional[ProgressCallback],
    ) -> Optional[str]:
        """
        Analyze a single chunk for relevance. Returns a summary string if relevant, None otherwise.
        """
        pages_str = ", ".join(str(p) for p in chunk["pages"])
        chunk_text = chunk["text"]

        question_context = ""
        if user_question:
            question_context = f'The user is asking: "{user_question}"\n\n'

        plan_context = ""
        if plan_category and plan_category != "unknown":
            regulator_info = cls.PLAN_REGULATOR_CONTEXT.get(plan_category, "")
            if regulator_info:
                plan_context = f"Plan context: {regulator_info}\n\n"

        search_hint = ""
        search_hits = chunk.get("search_hits")
        if search_hits:
            hits_str = ", ".join(search_hits[:10])
            search_hint = (
                f"This section was flagged by keyword search for: {hits_str}. "
                "Pay special attention to these topics.\n\n"
            )

        prompt = f"""You are analyzing a chunk of a health insurance policy document (pages/sections: {pages_str}).

{question_context}{plan_context}{search_hint}Determine if this chunk contains ANY of the following:
- Coverage details (what IS covered, benefits)
- Exclusions (what is NOT covered)
- Appeal rights, deadlines, or procedures
- Medical necessity definitions
- Prior authorization requirements
- Grievance procedures
- Cost-sharing details (copays, deductibles, out-of-pocket maximums)
- Any information relevant to the user's question (if provided)

If this chunk contains relevant information, provide a concise summary (200-400 words) of what you found. Include exact quotes with page references where possible. Format as:

RELEVANT: YES
PAGES: {pages_str}
SUMMARY:
[Your summary with quotes and page references]

If this chunk contains NO relevant information (e.g., it's a table of contents, provider directory, definitions of common terms, or boilerplate), respond with just:

RELEVANT: NO

Document chunk:
{chunk_text}"""

        result = await cls._infer_with_fallback(
            system_prompts=[
                "You are an expert at analyzing health insurance policy documents. "
                "Be concise. Include exact page references for all findings."
            ],
            prompt=prompt,
            temperature=0.1,
            timeout=cls.CHUNK_TIMEOUT_SECONDS,
            model_count=2,
            label=f"chunk {chunk_index}",
        )

        async with remaining_counter:
            remaining_count[0] -= 1
            current_remaining = remaining_count[0]
        if progress_callback:
            await progress_callback(current_remaining, total_chunks)

        if not result:
            return None

        if "RELEVANT: NO" in result.upper():
            logger.debug(f"Chunk {chunk_index} (pages {pages_str}) not relevant")
            return None

        summary_start = result.find("SUMMARY:")
        if summary_start >= 0:
            return f"[Pages {pages_str}]\n{result[summary_start + 8:].strip()}"
        return f"[Pages {pages_str}]\n{result.strip()}"

    @classmethod
    async def _analyze_chunks_parallel(
        cls,
        chunks: list[Dict[str, Any]],
        user_question: Optional[str] = None,
        plan_category: str = "unknown",
        progress_callback: Optional[ProgressCallback] = None,
    ) -> list[str]:
        """
        Analyze all chunks in parallel (up to MAX_PARALLEL_CHUNKS at a time).
        Returns list of relevant chunk summaries.
        """
        total_chunks = len(chunks)
        # Shared mutable counter for progress tracking
        remaining_count = [total_chunks]
        remaining_counter = asyncio.Lock()

        if progress_callback:
            await progress_callback(total_chunks, total_chunks)

        summaries: list[str] = []

        # Use a bounded worker pool so we only have O(MAX_PARALLEL_CHUNKS) tasks
        # alive at any given time, even for very large documents.
        work_queue: asyncio.Queue[tuple[int, Dict[str, Any]]] = asyncio.Queue()
        for idx, chunk in enumerate(chunks):
            work_queue.put_nowait((idx, chunk))

        async def worker() -> None:
            while True:
                try:
                    idx, chunk = work_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                try:
                    result = await cls._analyze_single_chunk(
                        chunk=chunk,
                        chunk_index=idx,
                        user_question=user_question,
                        plan_category=plan_category,
                        remaining_counter=remaining_counter,
                        remaining_count=remaining_count,
                        total_chunks=total_chunks,
                        progress_callback=progress_callback,
                    )
                except Exception as exc:
                    logger.debug(f"Chunk {idx} raised exception: {exc}")
                else:
                    if result is not None:
                        summaries.append(result)

        # Start at most MAX_PARALLEL_CHUNKS workers (or fewer if there are
        # not enough chunks), and wait for all work to complete.
        num_workers = min(cls.MAX_PARALLEL_CHUNKS, total_chunks) or 1
        worker_tasks = [asyncio.create_task(worker()) for _ in range(num_workers)]
        await asyncio.gather(*worker_tasks)
        return summaries

    @classmethod
    async def _synthesize_chunk_summaries(
        cls,
        chunk_summaries: list[str],
        user_question: Optional[str] = None,
        plan_category: str = "unknown",
    ) -> Optional[Dict[str, Any]]:
        """
        Combine chunk summaries into a final structured analysis.
        """
        separator = "\n\n---\n\n"
        max_synthesis_chars = 30000
        combined_parts: list[str] = []
        total_chars = 0
        for summary in chunk_summaries:
            added_len = len(summary) + len(separator)
            if total_chars + added_len > max_synthesis_chars:
                logger.warning(
                    f"Truncating synthesis input at {len(combined_parts)}/{len(chunk_summaries)} summaries "
                    f"({total_chars} chars) to stay within {max_synthesis_chars} char limit"
                )
                break
            combined_parts.append(summary)
            total_chars += added_len
        combined_summaries = separator.join(combined_parts)

        question_context = ""
        if user_question:
            question_context = f"""
The user has a specific question about this policy:
"{user_question}"

Please pay special attention to sections relevant to this question.
"""

        plan_context = ""
        if plan_category and plan_category != "unknown":
            regulator_info = cls.PLAN_REGULATOR_CONTEXT.get(plan_category, "")
            if regulator_info:
                plan_context = f"""
Plan type context: {regulator_info}
Use this information to tailor your analysis of appeal rights and regulatory references.
"""

        prompt = f"""You are synthesizing an analysis of a health insurance policy document.
Multiple sections of the document have been analyzed independently. Below are the relevant findings from each section.
Your job is to combine these into a single, comprehensive, well-organized analysis.
{question_context}{plan_context}
CHUNK SUMMARIES:
{combined_summaries}

Synthesize all the above into a single structured analysis. Preserve exact page references from the chunk summaries.

Respond in JSON format with the following structure:
{{
    "exclusions": [
        {{"text": "exact quote from policy", "page": 1, "explanation": "plain English explanation"}}
    ],
    "inclusions": [
        {{"text": "exact quote from policy", "page": 1, "explanation": "plain English explanation"}}
    ],
    "appeal_clauses": [
        {{"text": "exact quote", "page": 1, "type": "appeal_deadline|medical_necessity|prior_auth|exception|grievance"}}
    ],
    "quotable_sections": [
        {{"quote": "exact quote for appeal letter", "page": 1, "relevance": "why this is useful"}}
    ],
    "summary": "2-3 paragraph plain-English summary of coverage, limitations, appeal rights, and regulatory oversight"
}}"""

        num_models = min(len(ml_router.internal_models_by_cost), 3)
        per_model_timeout = cls.SYNTHESIS_TIMEOUT_SECONDS / max(num_models, 1)

        try:
            result = await asyncio.wait_for(
                cls._infer_with_fallback(
                    system_prompts=[
                        "You are an expert at analyzing health insurance policy documents. "
                        "Synthesize findings into a clear, structured analysis. "
                        "Always preserve exact page references. Be thorough but concise."
                    ],
                    prompt=prompt,
                    temperature=0.2,
                    timeout=per_model_timeout,
                    model_count=3,
                    label="synthesis",
                ),
                timeout=cls.SYNTHESIS_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("Synthesis exceeded global timeout")
            result = None

        if result:
            parsed = cls._parse_analysis_response(result)
            if parsed:
                logger.debug("Successfully synthesized analysis")
                return parsed

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
        except (json.JSONDecodeError, ValueError) as e:
            logger.debug(f"Initial JSON parse failed, trying raw_decode fallback: {e}")

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
            output_parts.append(
                "**Please verify:** We've included page numbers so you can "
                "double-check each finding against your actual policy document. "
                "AI analysis can miss nuances — confirming key details yourself "
                "strengthens your position if you need to appeal.\n\n"
            )
            output_parts.append(POLICY_ANALYSIS_DISCLAIMER)

        return "".join(output_parts)

    @classmethod
    async def get_or_create_analysis(
        cls,
        policy_document: PolicyDocument,
        user_question: Optional[str] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Optional[PolicyDocumentAnalysis]:
        """
        Get existing analysis or create a new one.
        Includes user_question in cache lookup so different questions get fresh analysis.
        When user_question is None, prefer a generic (no-question) analysis.
        """
        # Normalize None (or other falsy values) to the empty string to match storage.
        normalized_user_question = user_question or ""

        lookup: Dict[str, Any] = {
            "policy_document": policy_document,
            "user_question": normalized_user_question,
        }

        existing = await PolicyDocumentAnalysis.objects.filter(**lookup).afirst()

        if existing:
            logger.debug(f"Found existing analysis for document {policy_document.id}")
            return existing

        return await cls.analyze_policy_document(
            policy_document,
            normalized_user_question,
            progress_callback=progress_callback,
        )

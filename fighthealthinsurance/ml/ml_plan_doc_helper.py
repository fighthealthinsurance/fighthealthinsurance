"""
Helper for extracting and summarizing relevant sections from plan documents.

The plan documents a person uploads during the appeal flow are read
(decrypted and parsed page by page) and summarized for the appeal by an
internal model, whose window holds MAX_CONTEXT_LENGTH characters of them:

1. Documents that fit go in whole.
2. Documents that don't -- an Evidence of Coverage or a clinical policy runs
   to tens of pages -- are searched instead. The best local model is told
   they are too long, shown how they are organized (their headings), and
   asked what to search them for; document_grep then fills the window with
   the passages that best cover those search strings, each labelled with
   its document and page, taking them round-robin so one string matching
   every page cannot crowd out the rest.
3. What was selected is summarized for use in appeal generation.
"""

import asyncio
import bisect
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set

from loguru import logger

from fighthealthinsurance.context_utils import truncate_at_boundary
from fighthealthinsurance.document_grep import (
    merge_adjacent,
    passage_text,
    select_passages,
)
from fighthealthinsurance.ml.ml_document_extraction import (
    PARSED_EXTENSIONS,
    aextract_text_from_bytes,
    read_and_decrypt_file,
)
from fighthealthinsurance.ml.ml_inference import infer_with_fallback
from fighthealthinsurance.ml.ml_models import RemoteModelLike
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import Denial, PlanDocuments

# Opens each page of a plan document's text, so a passage can name its page.
_PAGE_MARKER_RE = re.compile(r"^\[Page (\d+)\]$", re.MULTILINE)

# Lines that look like headings in extracted plan-document text: all
# capitals, or short and opened by a section number ("4.2 Exclusions",
# "B. Appeals", "Section 3"). A numbered line that runs on, or ends the way
# a list item or sentence does, is content.
_CAPS_HEADING_RE = re.compile(r"^[^a-z]*[A-Z]{3}[^a-z]*$")
_NUMBERED_HEADING_RE = re.compile(
    r"^(?:(?:section|article|part|chapter)\s+\S+|\d+(?:\.\d+)*\.?|[A-Z]\.)\s+\S",
    re.IGNORECASE,
)

# A list marker a model may put before a search string despite being asked
# not to: "-", "*", "1.", "2)", "(a)".
_LIST_MARKER_RE = re.compile(r"^\s*(?:[-*\u2022\u00b7]+|\(?\d+[.)]|\([a-z]\))\s*")


@dataclass(frozen=True)
class PlanDocumentText:
    """One plan document's text: its pages, each opened by "[Page N]"."""

    name: str
    text: str


class MLPlanDocHelper:
    """Helper class for ML-powered plan document analysis."""

    # Maximum characters to send to the model for summarization
    MAX_CONTEXT_LENGTH = 8000
    #: The largest stored document this will open. Checked before the read,
    #: since reading is where the memory goes.
    #:
    #: This bounds the INPUT, not what a parser makes of it. A compressed
    #: format expands far beyond its stored size, which is a pre-existing
    #: exposure for the Word documents this has always accepted and not
    #: something this number fixes. Bounding decompressed members is its own
    #: change.
    MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
    # Maximum time to spend on plan document summarization
    TIMEOUT_SECONDS = 60
    #: Search strings asked for (and kept) when documents are too long.
    MIN_SEARCH_TERMS = 6
    MAX_SEARCH_TERMS = 10
    #: The most of the documents' headings shown to the model choosing them.
    MAX_OUTLINE_CHARS = 2500
    #: Fallback models tried, in cost order, after the best local one.
    SEARCH_TERM_FALLBACK_MODELS = 2
    #: A passage's "[Plan document N, page P]" label and separator.
    PASSAGE_LABEL_CHARS = 60
    MAX_PASSAGES = 24

    @classmethod
    async def generate_search_terms(
        cls,
        denial_text: str,
        procedure: Optional[str] = None,
        diagnosis: Optional[str] = None,
        documents: Sequence[PlanDocumentText] = (),
    ) -> List[str]:
        """
        Ask the best local model what to search the plan documents for.

        Called when the documents are too long for the summary's window. The
        model is told so, given the denial (letter excerpt, procedure,
        diagnosis) and the documents' headings, and asked for short search
        strings in the documents' own words, since search is literal: a
        policy that says "step therapy" is never found by "fail first".

        The best local model (ml_router.best_internal_model) goes first,
        with the cheapest general-purpose ones behind it in case it fails.
        Should all fail, terms are built from the denial without a model.

        Args:
            denial_text: The denial letter text
            procedure: The denied procedure (if known)
            diagnosis: The diagnosis (if known)
            documents: The plan documents being searched

        Returns:
            Search strings, at most MAX_SEARCH_TERMS of them
        """
        context_parts = []
        if denial_text:
            # Truncate denial text if too long, preferring a sentence
            # boundary so the model isn't fed a half-finished sentence.
            context_parts.append(
                f"Denial letter excerpt: {truncate_at_boundary(denial_text, 2000)}"
            )
        if procedure:
            context_parts.append(f"Denied procedure: {procedure}")
        if diagnosis:
            context_parts.append(f"Diagnosis: {diagnosis}")

        if not context_parts:
            # Nothing to ask about, but documents too long to send whole
            # still need something to be searched for.
            return cls._extract_fallback_terms(denial_text, procedure, diagnosis)

        context = "\n".join(context_parts)
        documents_part = ""
        if documents:
            total = sum(len(doc.text) for doc in documents)
            documents_part = (
                f"The insurance plan documents submitted with this appeal are "
                f"too long to read whole: {total:,} characters, of which about "
                f"{cls.MAX_CONTEXT_LENGTH:,} fit. You are choosing what to "
                f"search them for, so the parts that matter to this appeal are "
                f"the ones that get read.\n\n"
            )
            context += (
                "\n\nHow the documents are organized (their headings, or their "
                f"opening where they have none):\n{cls.document_outline(documents)}"
            )

        prompt = f"""{documents_part}{context}

Write {cls.MIN_SEARCH_TERMS} to {cls.MAX_SEARCH_TERMS} search strings, one per line. Each is a word or a short phrase (one to four words) that appears word for word in documents like these -- use the documents' own terms where the headings show them, not a paraphrase. Cover:
- the denied treatment or drug (brand and generic names, and billing codes if known)
- the coverage or medical necessity criteria for it
- exclusions and limitations that might apply
- prior authorization or step therapy requirements
- appeal rights and deadlines

Write only the search strings: no numbering, bullets, quotes or explanations."""

        result = await infer_with_fallback(
            system_prompts=[
                "You choose search strings for finding the passages that "
                "matter in long health insurance plan documents. Answer with "
                "search strings only, one per line."
            ],
            prompt=prompt,
            temperature=0.3,
            timeout=30,
            label="search terms",
            models=cls._search_term_models(),
            task="doc_summary",
        )
        terms = cls.parse_search_terms(result) if result else []
        if terms:
            # The denial's own procedure and diagnosis are searched for even
            # when the model left them out.
            known = {term.lower() for term in terms}
            for extra in (procedure, diagnosis):
                if extra and extra.lower() not in known:
                    terms.append(extra)
            logger.debug(f"Generated {len(terms)} plan document search terms")
            return terms

        # Fallback: extract key terms from denial text and procedure
        fallback_terms = cls._extract_fallback_terms(denial_text, procedure, diagnosis)
        return fallback_terms

    @classmethod
    def _search_term_models(cls) -> List[RemoteModelLike]:
        """The best local model, then the cheapest general-purpose ones."""
        best: Optional[RemoteModelLike] = ml_router.best_internal_model()
        pool: List[RemoteModelLike] = ml_router.general_purpose_internal_models()
        if best is None:
            return pool[: 1 + cls.SEARCH_TERM_FALLBACK_MODELS]
        rest = [model for model in pool if model is not best]
        return [best] + rest[: cls.SEARCH_TERM_FALLBACK_MODELS]

    @classmethod
    def parse_search_terms(cls, text: str) -> List[str]:
        """Search strings out of a model's answer.

        One per line is what was asked for; a comma- or semicolon-separated
        list is split too. List markers, quotes and trailing punctuation
        are dropped, as are a heading line ("Search strings:") and anything
        too long to be a search string, and repeats.
        """
        terms: List[str] = []
        seen: Set[str] = set()
        for line in text.splitlines():
            for piece in re.split(r"[;,]", line):
                term = _LIST_MARKER_RE.sub("", piece).strip()
                term = term.strip("\"'`\u201c\u201d\u2018\u2019*").strip()
                term = term.rstrip(".")
                if (
                    not term
                    or term.endswith(":")
                    or len(term) > 60
                    or len(term.split()) > 6
                    or term.lower() in seen
                ):
                    continue
                seen.add(term.lower())
                terms.append(term)
        return terms[: cls.MAX_SEARCH_TERMS]

    @classmethod
    def document_outline(cls, documents: Sequence[PlanDocumentText]) -> str:
        """The documents' headings, for the model choosing search strings.

        A document with fewer than three heading-like lines is shown by its
        opening instead. Bounded by MAX_OUTLINE_CHARS overall.
        """
        parts: List[str] = []
        budget = cls.MAX_OUTLINE_CHARS
        for doc in documents:
            headings: List[str] = []
            for raw in doc.text.splitlines():
                line = raw.strip()
                if not (3 <= len(line) <= 80) or _PAGE_MARKER_RE.match(line):
                    continue
                numbered = (
                    len(line) <= 60
                    and _NUMBERED_HEADING_RE.match(line)
                    and line[-1] not in ",;."
                )
                if (_CAPS_HEADING_RE.match(line) or numbered) and line not in headings:
                    headings.append(line)
            if len(headings) >= 3:
                body = "\n".join(headings)
            else:
                body = truncate_at_boundary(
                    _PAGE_MARKER_RE.sub("", doc.text).strip(), 800
                )
            part = f"{doc.name}:\n{body}"
            if len(part) > budget:
                part = truncate_at_boundary(part, budget)
            if not part:
                break
            parts.append(part)
            budget -= len(part)
            if budget <= 0:
                break
        return "\n\n".join(parts)

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
    async def load_plan_documents(cls, denial_id: int) -> List[PlanDocumentText]:
        """Every readable plan document uploaded with a denial, as text.

        Decrypted and parsed page by page; each page is opened by a
        "[Page N]" line so passages and summaries can cite it. Documents
        that can't be read (too large, damaged, scanned) are left out.
        """
        documents: List[PlanDocumentText] = []
        try:
            plan_docs = PlanDocuments.objects.filter(denial_id=denial_id).order_by(
                "plan_document_id"
            )
            async for doc in plan_docs.aiterator():
                try:
                    # Try encrypted field first, fall back to unencrypted
                    file_field = doc.plan_document_enc or doc.plan_document
                    if not file_field:
                        continue

                    # Read and decrypt the file off the event loop. We must
                    # decrypt the bytes rather than read file_field.path:
                    # plan_document_enc is an EncryptedFileField whose on-disk
                    # bytes are ciphertext, and .path also raises
                    # NotImplementedError on remote/S3 storage backends.
                    # The ceiling goes in, so an oversized document is never
                    # read rather than read and then rejected: reading is
                    # where the memory goes.
                    decrypted_bytes = await asyncio.to_thread(
                        read_and_decrypt_file, file_field, cls.MAX_DOCUMENT_BYTES
                    )
                    if not decrypted_bytes:
                        continue
                    pages = await cls._read_pages(
                        decrypted_bytes, file_field.name or ""
                    )
                    text = "\n\n".join(
                        f"[Page {num}]\n{page.strip()}"
                        for num, page in sorted(pages.items())
                        if page.strip()
                    )
                    if text:
                        documents.append(
                            PlanDocumentText(
                                name=f"Plan document {len(documents) + 1}",
                                text=text,
                            )
                        )
                except Exception as e:
                    logger.debug(f"Error processing plan document: {type(e).__name__}")

        except Exception as e:
            logger.opt(exception=True).debug(
                f"Error getting plan documents for denial {denial_id}: "
                f"{type(e).__name__}"
            )
        return documents

    @classmethod
    async def extract_relevant_text(
        cls,
        denial_id: int,
        search_terms: Optional[List[str]] = None,
        denial: Optional[Denial] = None,
    ) -> str:
        """
        The plan documents' text that goes in the summary's window.

        With ``search_terms``, the passages that best cover them. Without,
        all of it when it fits MAX_CONTEXT_LENGTH; otherwise the best local
        model chooses the search strings (generate_search_terms) and the
        passages that best cover those go in.

        Args:
            denial_id: The denial ID to get plan documents for
            search_terms: Terms to search for, or None to decide as above
            denial: The denial, when the caller already has it

        Returns:
            Labelled text from the plan documents, or "" when there is none
        """
        documents = await cls.load_plan_documents(denial_id)
        if not documents:
            return ""
        if search_terms is None:
            whole = cls.whole_text(documents)
            if len(whole) <= cls.MAX_CONTEXT_LENGTH:
                return whole
            if denial is None:
                denial = await Denial.objects.filter(denial_id=denial_id).aget()
            logger.info(
                f"Plan documents for denial {denial_id} are too long to send "
                f"whole ({len(whole):,} chars); asking for search strings"
            )
            search_terms = await cls.generate_search_terms(
                denial_text=denial.denial_text or "",
                procedure=denial.value_or_candidate("procedure"),
                diagnosis=denial.value_or_candidate("diagnosis"),
                documents=documents,
            )
        return cls.relevant_passages(documents, search_terms)

    @staticmethod
    def whole_text(documents: Sequence[PlanDocumentText]) -> str:
        return "\n\n---\n\n".join(f"[{doc.name}]\n{doc.text}" for doc in documents)

    @classmethod
    def relevant_passages(
        cls, documents: Sequence[PlanDocumentText], search_terms: Sequence[str]
    ) -> str:
        """The passages that best cover ``search_terms``, within the window,
        in reading order, each labelled with its document and page."""
        terms = [term for term in search_terms if term and term.strip()]
        if not terms:
            return ""
        passages = select_passages(
            [doc.text for doc in documents],
            terms,
            budget_chars=cls.MAX_CONTEXT_LENGTH,
            max_passages=cls.MAX_PASSAGES,
            per_passage_overhead=cls.PASSAGE_LABEL_CHARS,
        )
        page_markers = [
            [(m.start(), m.group(1)) for m in _PAGE_MARKER_RE.finditer(doc.text)]
            for doc in documents
        ]
        sections: List[str] = []
        # Reading order. Passages that meet are joined: a gap no longer than
        # the label it saves costs nothing against the budget.
        for passage in merge_adjacent(passages, max_gap=cls.PASSAGE_LABEL_CHARS):
            doc = documents[passage.source]
            markers = page_markers[passage.source]
            at = bisect.bisect_right([pos for pos, _ in markers], passage.hit) - 1
            label = (
                f"[{doc.name}, page {markers[at][1]}]" if at >= 0 else f"[{doc.name}]"
            )
            sections.append(f"{label}\n{passage_text(doc.text, passage)}")
        return "\n\n---\n\n".join(sections)

    @classmethod
    async def _read_pages(cls, data: bytes, filename: str) -> Dict[int, str]:
        """A decrypted document's text, by page number.

        Text extraction is dispatched by *filename* extension (PDF, DOCX,
        plaintext); a format with no parser is read as text, as one page.
        """
        # The parse runs on the parser's own thread.
        _full_text, page_dict = await aextract_text_from_bytes(data, filename)
        parser_knows_this = filename.lower().endswith(PARSED_EXTENSIONS)
        if not page_dict and not parser_knows_this:
            # What this path did before it decrypted anything: read whatever
            # it does not recognise as text. A legacy upload named .text or
            # .rtf contributes what it says rather than nothing, silently.
            # Reading a patient's file as text costs its own size; building a
            # document tree from it does not, which is why a real parser for
            # those formats is a separate question.
            #
            # Only for a format with no parser. A PDF that came back empty is
            # damaged or scanned, and decoding a PDF as text yields its
            # syntax and metadata, which is the bug this branch exists to
            # fix, arriving by a different door.
            guessed = data.decode("utf-8", errors="ignore")
            if not guessed.strip():
                return {}
            page_dict = {1: guessed}
        return dict(page_dict)

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

        procedure = denial.value_or_candidate("procedure") or "the denied treatment"
        diagnosis = denial.value_or_candidate("diagnosis") or ""

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
{truncate_at_boundary(relevant_text, cls.MAX_CONTEXT_LENGTH)}

Provide a concise summary (max 500 words) that would help craft an effective appeal.
Include specific page references where helpful. A long document is summarized
from passages selected by searching it, so when a point above isn't covered,
leave it out rather than saying the plan doesn't address it."""

        result = await infer_with_fallback(
            system_prompts=[
                "You are an expert at analyzing health insurance plan documents "
                "to help patients and providers craft effective appeals. "
                "Provide clear, actionable summaries focused on what supports the appeal."
            ],
            prompt=prompt,
            temperature=0.3,
            timeout=45,
            min_length=50,
            label="plan doc summary",
            task="doc_summary",
        )
        if result:
            logger.debug(f"Generated plan document summary ({len(result)} chars)")
        return result

    @classmethod
    async def generate_plan_documents_summary(cls, denial_id: int) -> Optional[str]:
        """
        Main entry point: Generate a summary of relevant plan document sections.

        This method:
        1. Reads the plan documents, whole when they fit the window
        2. Otherwise asks the best local model what to search them for, and
           selects the passages that best cover those search strings
        3. Summarizes what was selected for use in appeal generation

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
                # Steps 1 and 2: the documents whole when they fit the
                # window, else the passages for the search strings the best
                # local model chose.
                relevant_text = await cls.extract_relevant_text(
                    denial_id, denial=denial
                )

                if not relevant_text:
                    logger.debug(
                        f"No relevant text found in plan docs for denial {denial_id}"
                    )
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

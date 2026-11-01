"""
Tool calls for the Appeal Arena's synthetic chat drafts.

The chat assistant answers some questions by looking something up first: a
``**pubmed_query: ...**`` for the research on a treatment, a
``**uspstf_lookup {...}**`` for a preventive-care rule, a
``**medicaid_info {...}**`` for a state's Medicaid office, and so on. The
arena asks each model for its reply through the same ``generate_chat_response``
the chat uses, so a model that would look something up asks to here too; until
now nothing ran the lookup, and the voter was shown the raw tool syntax (or the
reply was too short to keep, when the call was all it said).

``answer_with_tools`` runs those calls the way ChatInterface does, through the
chat's own tool handlers, and hands the result back to the model for the reply
it would have given a person. It also keeps a record of every call, what it
was asked and what it found, so the voter can see the lookup next to the reply
and judge whether the model used it well (or needed it at all). Whether to
look anything up stays the model's call: nothing here asks for a tool.

Only the read-only lookups run, against public sources: PubMed, RxNorm,
ClinicalTrials.gov, USPSTF, the Medicaid directory, eligibility rules and
Medicaid.gov pages, payers' PA lists and the financial-assistance directory.
The tools that write (appeal and prior-auth records, letters), fetch a URL the
model names, or search a person's documents have nothing to act on in a
synthetic case and are never run; their syntax is stripped from the reply.
The case is synthetic, so a query carries nothing about a person.
"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Type, cast

from django.conf import settings

from loguru import logger

from fighthealthinsurance.chat.tools import (
    AppealTool,
    ClinicalTrialsTool,
    DocFetcherTool,
    DocumentSearchTool,
    FinancialAssistanceTool,
    GenerateAppealLetterTool,
    MedicaidEligibilityTool,
    MedicaidGovLookupTool,
    MedicaidInfoTool,
    PaRequirementLookupTool,
    PriorAuthTool,
    PubMedTool,
    RxNormLookupTool,
    USPSTFLookupTool,
)
from fighthealthinsurance.chat.tools.base_tool import BaseTool

# How many rounds of lookups one draft may make: a model may look something
# up, read it and look up one more thing. Past that the call is stripped.
MAX_TOOL_DEPTH = 2
# The most lookups one draft may run, all rounds together.
MAX_TOOL_CALLS = 4
# Seconds one lookup (the search and the model's reply to what it found) may
# take before the draft goes on without it.
CHOOSER_CHAT_TOOL_TIMEOUT = getattr(settings, "CHOOSER_CHAT_TOOL_TIMEOUT", 120.0)
# What the voter is shown of one lookup: the call as the model wrote it, and
# what came back, cut to these lengths.
_MAX_CALL_CHARS = 300
_MAX_FOUND_CHARS = 4000
_MAX_NOTES = 12
_MAX_NOTE_CHARS = 300
# What the model is told when its lookups left it with nothing to say.
_NOTHING_FOUND_MESSAGE = (
    "(The lookup you asked for found nothing useful. Answer the person's "
    "last question as best you can without it, and do not ask for another "
    "lookup.)"
)


def tools_allowed() -> bool:
    """Whether the arena runs the chat drafts' lookups. Read per call, like
    the arena's live PubMed switch: off in tests, which would otherwise
    reach PubMed and the other public services; an operator short on NCBI
    quota can turn it off too. Off, a draft is kept as the model wrote it."""
    return bool(getattr(settings, "CHOOSER_CHAT_TOOLS", True))


@dataclass
class ToolCallRecord:
    """One lookup a draft made, as the voter is shown it."""

    tool: str
    call: str
    found: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    timed_out: bool = False

    def as_dict(self) -> Dict[str, Any]:
        found = "\n\n".join(self.found).strip()
        if len(found) > _MAX_FOUND_CHARS:
            found = found[:_MAX_FOUND_CHARS].rstrip() + "…"
        record: Dict[str, Any] = {
            "tool": self.tool,
            "call": self.call[:_MAX_CALL_CHARS],
            "found": found,
            "notes": [n[:_MAX_NOTE_CHARS] for n in self.notes[:_MAX_NOTES]],
        }
        if self.timed_out:
            record["timed_out"] = True
        return record


@dataclass
class ToolAnswer:
    """A draft and the lookups it made on the way (empty when none)."""

    text: Optional[str]
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)


def _call_text(match: re.Match[str], text: str) -> str:
    """The call as the voter should read it: the query or JSON payload when
    the pattern captures one, else the whole call. The financial-assistance
    pattern matches only the name before its JSON (see
    FINANCIAL_ASSISTANCE_REGEX), so its payload is read up to the call's
    real end, or every such call would read the same and merge as one."""
    if match.re.pattern == FinancialAssistanceTool.pattern:
        end = FinancialAssistanceTool._find_call_end(text, match)
        return text[match.end() : end].strip().rstrip("*").strip()
    try:
        captured = match.group(1)
    except IndexError:
        captured = None
    return (captured or match.group(0)).strip()


Note = Callable[[str], Awaitable[None]]
FollowUp = Callable[..., Awaitable[Tuple[Optional[str], Optional[str]]]]

# The read-only tools, in ChatInterface's order (RxNorm before PubMed, so a
# normalized drug name can feed a later search), each with how to build it
# around a status-line sink and a follow-up callback.
_TOOLS: List[Tuple[Type[BaseTool], Callable[[Note, FollowUp], BaseTool]]] = [
    (RxNormLookupTool, lambda n, f: RxNormLookupTool(n, call_llm_callback=f)),
    (PubMedTool, lambda n, f: PubMedTool(n, call_llm_callback=f)),
    (ClinicalTrialsTool, lambda n, f: ClinicalTrialsTool(n, call_llm_callback=f)),
    (
        MedicaidEligibilityTool,
        lambda n, f: MedicaidEligibilityTool(n, f, eligibility_computed=[False]),
    ),
    (MedicaidInfoTool, lambda n, f: MedicaidInfoTool(n, f)),
    (
        MedicaidGovLookupTool,
        lambda n, f: MedicaidGovLookupTool(n, call_llm_callback=f, lookup_count=[0]),
    ),
    (USPSTFLookupTool, lambda n, f: USPSTFLookupTool(n, f)),
    (
        PaRequirementLookupTool,
        lambda n, f: PaRequirementLookupTool(n, call_llm_callback=f),
    ),
    (
        FinancialAssistanceTool,
        lambda n, f: FinancialAssistanceTool(n, call_llm_callback=f),
    ),
]


def _detect(kind: Type[BaseTool], text: str) -> Optional[re.Match[str]]:
    """BaseTool.detect without building the tool."""
    return re.search(kind.pattern, text, flags=kind.detect_flags)


async def _quiet(_message: str) -> None:
    return None


def strip_tool_syntax(text: str) -> str:
    """``text`` without any tool call left in it: one this module will not
    run, or one past the depth or call limit. Each tool strips its own
    calls (BaseTool.strip_calls_on_error), so the anchored JSON calls are
    removed span by span rather than by a pattern that could run on into
    the prose after them, and the financial-assistance call with its JSON
    (its pattern stops at the brace). A reply that was nothing but calls
    comes back empty, so the arena's short-reply fallback can ask for a
    real answer rather than keep the raw call as one."""
    stripped = text
    for tool in [build(_quiet, _no_follow_up) for _, build in _TOOLS] + [
        AppealTool(_quiet),
        PriorAuthTool(_quiet),
        GenerateAppealLetterTool(_quiet),
        DocFetcherTool(_quiet),
        DocumentSearchTool(_quiet),
    ]:
        if not tool.detect(stripped):
            continue
        if isinstance(tool, FinancialAssistanceTool):
            stripped = tool.clean_all_matches(stripped, tool.detect_all(stripped))
        else:
            stripped = tool.strip_calls_on_error(stripped)
        if tool.detect(stripped):
            # strip_calls_on_error hands back the text unchanged when the
            # call was all of it.
            return ""
    return stripped.strip()


async def _no_follow_up(*_args: Any, **_kwargs: Any) -> Tuple[None, None]:
    return None, None


class _ToolRun:
    """One draft's model, the conversation it answers, and the record of
    the lookups it makes."""

    def __init__(self, model: Any, history: List[Dict[str, str]]) -> None:
        self.model = model
        self.history = list(history)
        self.records: List[ToolCallRecord] = []

    async def ask(
        self, message: str, summary: Optional[str], history: List[Dict[str, str]]
    ) -> Optional[str]:
        response, _ = await self.model.generate_chat_response(
            current_message_for_llm=message,
            previous_context_summary=summary,
            history=history,
            is_professional=True,
            is_logged_in=True,
        )
        return cast(Optional[str], response)

    def _follow_up(
        self, record: ToolCallRecord, exchange: List[Dict[str, str]]
    ) -> FollowUp:
        """The callback a tool hands what it found to: noted on ``record``,
        then read back to the model (whose reply may look up more)."""

        async def follow_up(
            _backends: Any,
            found: str,
            summary: Optional[str],
            history_for_llm: Any,
            depth: int = 1,
            **_kwargs: Any,
        ) -> Tuple[Optional[str], Optional[str]]:
            record.found.append(str(found))
            # Most tools hand on the history as it stood before the
            # question; the JSON tools add the exchange themselves.
            seen = list(history_for_llm or [])
            if len(seen) < len(exchange):
                seen = list(exchange)
            answer = await self.ask(found, summary or None, seen)
            if not answer:
                return answer, None
            return await self.run(answer, found, seen, depth), None

        return follow_up

    async def run(
        self,
        reply: str,
        asked: str,
        history: List[Dict[str, str]],
        depth: int,
    ) -> str:
        """Run every lookup in ``reply`` (the model's answer to ``asked``,
        with ``history`` before it), and return the reply the model gave
        once it had read what they found."""
        if depth >= MAX_TOOL_DEPTH:
            return strip_tool_syntax(reply)
        for kind, build in _TOOLS:
            match = _detect(kind, reply)
            if match is None:
                continue
            if len(self.records) >= MAX_TOOL_CALLS:
                break
            record = ToolCallRecord(tool=kind.name, call=_call_text(match, reply))
            self.records.append(record)
            exchange = history + [
                {"role": "user", "content": asked},
                {"role": "assistant", "content": reply},
            ]

            async def note(message: str, record: ToolCallRecord = record) -> None:
                record.notes.append(str(message))

            tool = build(note, self._follow_up(record, exchange))
            try:
                reply, _, _ = await asyncio.wait_for(
                    tool.handle(
                        reply,
                        "",
                        model_backends=[self.model],
                        current_message_for_llm=asked,
                        previous_context_summary="",
                        history_for_llm=list(history),
                        depth=depth,
                        is_logged_in=True,
                        is_professional=True,
                    ),
                    timeout=CHOOSER_CHAT_TOOL_TIMEOUT,
                )
            except asyncio.TimeoutError:
                record.timed_out = True
                logger.info(f"Chooser chat draft: {kind.name} lookup timed out")
                reply = strip_tool_syntax(reply)
        return strip_tool_syntax(reply)


async def answer_with_tools(
    model: Any,
    question: str,
    history: List[Dict[str, str]],
    min_len: int,
) -> ToolAnswer:
    """``model``'s reply to ``question`` (after ``history``), with any
    lookup it asks for run and read back to it first; see the module
    docstring. ``min_len`` is the shortest reply the arena keeps: when the
    lookups leave the model with less, it is asked once more to answer
    without them."""
    run = _ToolRun(model, history)
    reply = await run.ask(question, None, run.history)
    if not reply or not tools_allowed():
        return ToolAnswer(reply)
    final = await run.run(reply, question, run.history, 0)
    found_nothing = not any(r.found for r in run.records)
    if run.records and found_nothing and len(final.strip()) <= min_len:
        # A search that found nothing leaves only what the model said
        # around its call, often nothing at all. (One that found something
        # was read back to the model already; telling it the search found
        # nothing would be false.)
        retry = await run.ask(
            _NOTHING_FOUND_MESSAGE,
            None,
            run.history
            + [
                {"role": "user", "content": question},
                {"role": "assistant", "content": reply},
            ],
        )
        if retry:
            final = strip_tool_syntax(retry)
    return ToolAnswer(final, [r.as_dict() for r in run.records])

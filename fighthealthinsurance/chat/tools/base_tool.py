"""
Base class for chat tool handlers.

Tool handlers process specific "tool calls" that the LLM includes in responses.
Each tool can detect its pattern in text, execute the tool action, and format results.
"""

import json
import re
from abc import ABC, abstractmethod
from typing import Awaitable, Callable, List, Optional, Set, Tuple

from loguru import logger

# Identity/audit fields that LLM-supplied tool payloads must never overwrite,
# even though they are concrete editable columns.
_TOOL_FIELD_DENYLIST = {
    "id",
    "pk",
    "uuid",
    "semi_sekret",
    "follow_up_semi_sekret",
    "session_key",
}


def settable_model_fields(model_cls) -> Set[str]:
    """Field names an LLM tool payload may set on ``model_cls``.

    Concrete, editable, non-primary-key, non-relation fields minus the
    denylist. Relations (and their ``*_id`` attribute forms) are excluded so
    a crafted payload can't re-point an appeal/prior auth at another user's
    denial or chat; identity fields are excluded so it can't overwrite
    lookup keys.
    """
    fields: Set[str] = set()
    for f in model_cls._meta.concrete_fields:
        if f.primary_key or f.is_relation or not getattr(f, "editable", True):
            continue
        if f.name in _TOOL_FIELD_DENYLIST:
            continue
        fields.add(f.name)
    return fields


def is_safe_tool_field(key: str, allowed: Set[str]) -> bool:
    """Whether an LLM payload key may be applied to a model instance."""
    if not key or key.startswith("_") or key.endswith("_id"):
        return False
    return key in allowed


def parse_anchored_json_payload(text: str, match: re.Match[str]) -> Tuple[dict, str]:
    """Parse the JSON payload of an anchored ``**tool**{...}`` call precisely.

    The anchored patterns (create_or_update_appeal / _prior_auth /
    generate_appeal_letter) capture ``(\\{.*\\})`` under DOTALL, so when two
    tool calls share a reply the greedy group runs from the first call's
    ``{`` through the LAST ``}`` in the text -- ``json.loads`` on the group
    then fails and BOTH calls get stripped. Instead, decode from the
    captured group's start with ``raw_decode``, which stops at the end of
    the first complete JSON value (same approach as FinancialAssistanceTool
    / the PA-requirement lookup). Returns ``(payload, call_span)`` where
    ``call_span`` is the exact ``**tool**{...}`` substring of ``text`` to
    replace -- handlers must replace it rather than ``match.group(0)``,
    whose over-capture would swallow the text between the calls.

    Raises ``json.JSONDecodeError`` for an undecodable or non-object
    payload. NOTE for callers: the payload can carry medical/claim details,
    so error paths must not log it or echo it back -- log sizes only.
    """
    start = match.start(1)
    payload, end = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(payload, dict):
        raise json.JSONDecodeError(
            "tool payload must be a JSON object", text[start : start + end], 0
        )
    return payload, text[match.start() : start + end]


def remove_anchored_call(
    text: str, match: re.Match[str], tool: Optional["BaseTool"] = None
) -> str:
    """Remove ONE anchored ``**tool**{...}`` call from ``text`` precisely.

    Uses the raw_decode span when the payload parses. When it does NOT
    parse, the removal runs to the last ``}`` of the broken body -- bounded
    by where the next call of ``tool`` starts, so it still can't swallow a
    later tool call the way a ``re.sub`` over the greedy DOTALL pattern
    would. Cutting at the first newline instead (as this used to) left the
    rest of a pretty-printed malformed payload behind, putting its
    contents -- possibly medical detail -- in the reply and in chat
    history. Without ``tool`` the bound is the end of the text.
    """
    start = match.start()
    try:
        _, span = parse_anchored_json_payload(text, match)
        return text[:start] + text[start + len(span) :]
    except json.JSONDecodeError:
        pass

    body_start = match.start(1)
    # Never reach past the next call of this tool: it gets its own removal.
    limit = len(text)
    if tool is not None:
        following = tool.detect(text[body_start + 1 :])
        if following:
            limit = body_start + 1 + following.start()
    close = text.rfind("}", body_start, limit)
    if close != -1:
        end = close + 1
    else:
        # No closing brace at all (a truncated payload): fall back to the
        # line bound, which at least takes the token and what follows it.
        newline = text.find("\n", body_start)
        end = min(newline if newline != -1 else limit, limit)
    return text[:start] + text[end:]


def strip_anchored_calls(
    tool: "BaseTool",
    response_text: str,
    notice: Optional[str] = None,
    empty_fallback: Optional[str] = None,
) -> str:
    """Span-bounded removal of EVERY remaining call of ``tool`` in the text.

    The anchored tools' error/straggler cleanup: a loop of
    ``remove_anchored_call`` so nothing between calls is lost. It runs until
    no call is left rather than for a fixed number of rounds -- a fixed cap
    returned the calls past it as raw syntax, payload included. Each removal
    strictly shortens the text, so the loop terminates; the progress check
    is defensive only.

    ``notice`` is appended ONCE when anything was actually stripped. Pass it
    whenever the dropped calls carried work that was never done: the
    stripped reply is what the user reads AND what is persisted to
    chat_history, so with no notice the model sees its call as accepted and
    never retries it. The error path passes None -- a status message has
    already gone out there.

    ``empty_fallback`` is returned when the reply was NOTHING but tool calls
    and stripping leaves it empty. Without one the original text is returned
    (the historical behavior), which in that case hands the user the raw
    call and its payload -- so the anchored tools pass a sentence instead.
    """
    text = response_text
    removed = 0
    while True:
        match = tool.detect(text)
        if not match:
            break
        shortened = remove_anchored_call(text, match, tool)
        if len(shortened) >= len(text):
            logger.warning(
                f"{tool.name}: tool-call removal made no progress; "
                f"stopping with {len(text)} chars left"
            )
            break
        text = shortened
        removed += 1
    text = text.strip()
    if removed and notice:
        text = f"{text}\n\n{notice}" if text else notice
    if text:
        return text
    return empty_fallback if empty_fallback is not None else response_text


class BaseTool(ABC):
    """
    Abstract base class for chat tool handlers.

    Subclasses implement specific tools like PubMed search, Medicaid lookup, etc.
    """

    # Regex pattern to detect this tool in LLM output
    pattern: str = ""

    # Regex flags used for detection (subclasses can override)
    detect_flags: int = re.IGNORECASE

    # Regex flags used by detect_all() — defaults match the JSON-payload tools
    # (medicaid, doc fetcher, USPSTF, PA requirement) which need DOTALL for
    # multi-line JSON.
    detect_all_flags: int = re.DOTALL | re.IGNORECASE

    # Human-readable name for status messages
    name: str = "Tool"

    # How many calls of THIS tool ``handle`` may execute in one reply. Most
    # tools keep 1 -- their handlers either process every match internally
    # via detect_all (medicaid, financial assistance) or run a recursive
    # LLM pass that supersedes the reply. The anchored JSON tools raise it:
    # since their execute() replaces only the exact call span, a second
    # call in the same reply would otherwise survive unexecuted and render
    # as raw tool syntax.
    max_calls_per_reply: int = 1

    def __init__(
        self,
        send_status_message: Callable[[str], Awaitable[None]],
    ):
        """
        Initialize the tool handler.

        Args:
            send_status_message: Async function to send status updates to the user
        """
        self.send_status_message = send_status_message

    def detect(self, text: str) -> Optional[re.Match[str]]:
        """
        Check if this tool's pattern is present in the text.

        Args:
            text: The LLM response text to check

        Returns:
            Match object if pattern found, None otherwise
        """
        if not self.pattern:
            return None
        return re.search(self.pattern, text, flags=self.detect_flags)

    def detect_all(self, text: str) -> List[re.Match[str]]:
        """Find every tool-call match in the text.

        Useful when an LLM emits multiple invocations of the same tool in a
        single response and the handler needs to enumerate them.
        """
        if not self.pattern:
            return []
        return list(re.finditer(self.pattern, text, flags=self.detect_all_flags))

    def clean_response(self, text: str, match: re.Match[str]) -> str:
        """
        Remove the tool call from the response text.

        Args:
            text: Original response text
            match: The regex match object for the tool call

        Returns:
            Response text with the tool call removed
        """
        return text.replace(match.group(0), "").strip()

    def clean_all_matches(self, text: str, matches: List[re.Match[str]]) -> str:
        """Strip every tool-call match from the response text."""
        cleaned = text
        for match in matches:
            cleaned = cleaned.replace(match.group(0), "")
        return cleaned.strip()

    @staticmethod
    def merge_strings(existing: Optional[str], addition: Optional[str]) -> str:
        """Concatenate two strings with a blank-line separator.

        Returns ``""`` if both are empty/None. Used to glue an LLM follow-up
        response onto the original response (or follow-up context onto the
        original context) so adjacent paragraphs don't run together.

        Only newline characters are stripped at the join — leading spaces /
        tabs are preserved so markdown indentation, nested-list alignment,
        and code-block leading whitespace aren't accidentally clobbered.
        """
        if not existing:
            return (addition or "").lstrip("\n")
        if not addition:
            return existing
        return existing.rstrip("\n") + "\n\n" + addition.lstrip("\n")

    @abstractmethod
    async def execute(
        self, match: re.Match[str], response_text: str, context: str, **kwargs
    ) -> Tuple[str, str]:
        """
        Execute the tool action.

        Args:
            match: The regex match object containing tool parameters
            response_text: The current LLM response text
            context: The current context string
            **kwargs: Additional arguments specific to the tool

        Returns:
            Tuple of (updated_response_text, updated_context)
        """
        pass

    def strip_calls_on_error(self, response_text: str) -> str:
        """Remove this tool's raw syntax after execute() failed.

        Default: regex-sub every match of the pattern. The anchored JSON
        tools override this with span-bounded removal (strip_anchored_calls)
        because a greedy DOTALL sub over their pattern would also delete
        the text BETWEEN two calls -- including a different pending tool
        call. Falls back to the original text when stripping leaves nothing.
        """
        stripped = re.sub(
            self.pattern, "", response_text, flags=self.detect_all_flags
        ).strip()
        return stripped or response_text

    def dropped_calls_notice(self) -> str:
        """Sentence appended when calls past ``max_calls_per_reply`` are
        dropped from a SUCCESSFUL reply.

        Those calls carried updates that were never applied, and the reply
        (minus them) is what both the user reads and the model sees in the
        history, so saying nothing would leave the user thinking the change
        landed and the model with no reason to retry.
        """
        return (
            f"(Note: I could only apply the first {self.max_calls_per_reply} "
            f"updates in one go, so anything after that hasn't been saved -- "
            f"tell me what else to change and I'll take care of it.)"
        )

    async def handle(
        self, response_text: str, context: str, **kwargs
    ) -> Tuple[str, str, bool]:
        """
        Detect and handle this tool if present in the response.

        Executes up to ``max_calls_per_reply`` calls of this tool (each pass
        re-detects against the updated text, so an execute() that replaces
        its call span lets the next call be found).

        Args:
            response_text: The LLM response text
            context: The current context string
            **kwargs: Additional arguments passed to execute()

        Returns:
            Tuple of (updated_response_text, updated_context, was_handled)
        """
        handled = False
        try:
            for _ in range(max(1, self.max_calls_per_reply)):
                match = self.detect(response_text)
                if not match:
                    break
                logger.debug(f"{self.name} tool detected in response")
                response_text, context = await self.execute(
                    match, response_text, context, **kwargs
                )
                handled = True
            if handled and self.max_calls_per_reply > 1 and self.detect(response_text):
                # Calls past the per-reply cap are stripped (with a notice
                # saying they were not applied) rather than left to render
                # as raw tool syntax with their JSON payloads. Gated on
                # max_calls_per_reply > 1, i.e. the anchored JSON tools:
                # span-bounded removal assumes their `**tool**{...}` shape,
                # and single-call tools keep their historical behavior.
                logger.info(
                    f"{self.name}: more than {self.max_calls_per_reply} calls "
                    f"in one reply; stripping the rest"
                )
                response_text = strip_anchored_calls(
                    self, response_text, notice=self.dropped_calls_notice()
                )
            return response_text, context, handled

        except Exception as e:
            logger.opt(exception=True).warning(f"Error executing {self.name} tool: {e}")
            try:
                await self.send_status_message(
                    f"Error processing {self.name} request. Continuing with original response."
                )
            except Exception:
                logger.debug(f"{self.name}: could not send tool-error status")
            # Best-effort strip of the raw tool syntax before handing the text
            # back: the user should never see `**create_or_update_appeal**
            # {...}` in the chat because a tool blew up mid-execution.
            cleaned_response = response_text
            try:
                cleaned_response = self.strip_calls_on_error(response_text)
            except Exception:
                logger.debug(f"{self.name}: could not strip tool syntax on error")
            return cleaned_response, context, True

"""
LLM client utilities for chat interface.

Extracts core LLM calling logic from ChatInterface for reusability and testing.
"""

import math
import re
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from loguru import logger

from fighthealthinsurance.chat.message_preprocessor import (
    MessageVariant,
    is_stored_message_marker,
)
from fighthealthinsurance.chat.safety_filters import (
    detect_eligibility_verdict,
    detect_false_promises,
)
from fighthealthinsurance.chat.tools.patterns import (
    contains_tool_call,
    count_tool_invocations,
)
from fighthealthinsurance.ml.ml_models import RemoteModelLike, repetition_penalty

# Shared similarity/exemption helpers (stdlib-only module, importable from
# both this layer and ml_models without a cycle). normalize_text,
# bag_of_words, is_canned_reply, user_requested_repeat and the exemption
# constants are re-exported from here for existing importers/tests.
from fighthealthinsurance.ml.response_similarity import (
    CANNED_REPLY_SIGNATURES,
    USER_REQUESTED_REPEAT_RE,
    bag_of_words,
    is_canned_reply,
    is_mostly_repeated,
    normalize_text,
    response_similarity,
    user_requested_repeat,
    user_requested_transformation,
)
from fighthealthinsurance.utils import ensure_message_alternation

# Response patterns that indicate a bad/leaked response
BAD_RESPONSE_PATTERNS = re.compile(
    r"(The user is a|The assistant is|is helping a patient with their|"
    r"I hope this message finds you well|"
    r"It is a conversation between a patient and an assistant|"
    r"Discussing how to appeal|Helping a patient appeal|the context is|"
    r"The patient was denied coverage for|"
    r"The patient is at risk of progression to type 2 diabetes mellitus|"
    r"You are Doughnut|Discussing an appeal for a|My system prompt is)",
    re.IGNORECASE,
)

# Context patterns that indicate a bad context
BAD_CONTEXT_PATTERNS = re.compile(
    r"(^Hi, |my name is doughnut|To help me understand, can you)",
    re.IGNORECASE,
)

# Minimum length for a valid LLM response or context
MIN_RESPONSE_LENGTH = 5

# Penalty applied ON TOP of forfeiting the model prior when a reply states a
# Medicaid/Medicare eligibility verdict the checker never produced. The
# forfeit is what makes the guard hold across backends; this margin keeps an
# invented verdict below a plain answer from the SAME call, whose content
# signals would otherwise be identical.
INVENTED_ELIGIBILITY_VERDICT_PENALTY = 2000

# Pattern to detect when the response asks for the patient's name
ASKS_FOR_PATIENT_NAME = re.compile(
    r"(what is (?:the |your )?patient'?s? name|"
    r"(?:could|can|would) you (?:please )?(?:provide|tell|give|share) (?:me )?(?:the |your )?patient'?s? name|"
    r"(?:what|who) is the (?:patient|name of the patient)|"
    r"I(?:'ll| will) need (?:the |your )?patient'?s? name)",
    re.IGNORECASE,
)


def estimate_history_tokens(history: List[Dict[str, str]]) -> int:
    """
    Estimate token count for message history.

    Uses rough approximation of ~4 characters per token.

    Args:
        history: List of message dicts with 'content' keys

    Returns:
        Estimated token count
    """
    return sum(len(msg.get("content", "")) for msg in history) // 4


# Common file extensions to detect document names in chat history
_FILE_EXT_PATTERN = re.compile(
    r"[\w\-. ]+\.(?:pdf|jpg|jpeg|png|gif|docx|doc|txt|csv|xlsx|xls|rtf|tiff?|bmp|webp)\b",
    re.IGNORECASE,
)


def _extract_document_names_from_history(
    chat_history: List[Dict[str, str]],
) -> List[str]:
    """Extract document filenames from recent chat history messages."""
    names: List[str] = []
    for msg in chat_history[-10:]:  # Only check recent messages
        content = msg.get("content", "")
        for match in _FILE_EXT_PATTERN.finditer(content):
            names.append(match.group(0).strip())
    return names


# --- Repetition penalty helpers ---

# Penalty for response that exactly matches (normalized) the last user/assistant message
EXACT_REPEAT_PENALTY = -500.0
# Penalty for response that is a near-verbatim (but not exact) rewording of a
# recent message. Between exact and bag-of-words in severity.
NEAR_REPEAT_PENALTY = -400.0
# Penalty for response with same bag-of-words as the last user/assistant message
BAG_OF_WORDS_REPEAT_PENALTY = -75.0
# Lighter penalties for matching older messages in the history
OLDER_ASSISTANT_REPEAT_PENALTY = -20.0
OLDER_USER_REPEAT_PENALTY = -10.0

# How many recent assistant replies a candidate is checked against for the
# HARD repeat rejection (covers both A-A loops and A-B-A alternation loops).
RECENT_ASSISTANT_REPLIES_TO_CHECK = 3


def find_repeated_reply(
    response_text: str,
    chat_history: Optional[List[Dict[str, str]]],
    current_message: Optional[str] = None,
    max_assistant_messages: int = RECENT_ASSISTANT_REPLIES_TO_CHECK,
) -> Optional[str]:
    """Detect a candidate reply that mostly repeats the recent conversation.

    Returns a short reason string ("echoes_user_message" /
    "repeats_recent_assistant_reply") when the response is (nearly) identical
    to the current user message or to one of the last few assistant replies,
    None otherwise. This is the trigger for HARD rejection: the observed chat
    loops were the model re-sending its previous reply verbatim after the
    user answered its question, and soft score penalties (-500) were dwarfed
    by per-model base scores (quality**2 scaling reaches ~8800), so the
    repeat still won the fan-out race.
    """
    if not response_text:
        return None
    if is_canned_reply(response_text):
        return None
    # A faithful transformation (proofread / fix the grammar / "now
    # reformat it into three paragraphs") is SUPPOSED to read nearly the
    # same as the text being transformed -- the user's pasted message OR
    # our own previous reply, and normalize_text collapses exactly the
    # differences a reformat introduces. So BOTH rungs stand down on
    # transform requests: with only the echoes-the-user rung exempted,
    # every faithful iterative edit of our last answer was still
    # hard-rejected and the anti-repeat retry steered models AWAY from
    # the requested output.
    if current_message and user_requested_transformation(current_message):
        return None
    # The stored-content markers ("You pasted a long message (~N chars)...",
    # "I've uploaded a document: ...") are system-generated stand-ins, not
    # user prose: a reply acknowledging the stored paste/document naturally
    # reuses their wording, so echoing them is not a loop. Without this
    # exemption a long-paste turn could have EVERY candidate hard-rejected,
    # failing the whole turn. A reply that IS the bare marker adds nothing,
    # though -- reject that one so it can't be delivered as the answer (when
    # every candidate is a bare echo, the stored-content acknowledgment
    # fallback takes over with something actually useful).
    if current_message and is_mostly_repeated(response_text, current_message):
        if not is_stored_message_marker(current_message):
            return "echoes_user_message"
        if normalize_text(response_text) == normalize_text(current_message):
            return "echoes_user_message"
    if not chat_history:
        return None
    checked = 0
    for msg in reversed(chat_history):
        if msg.get("role") != "assistant":
            continue
        if is_mostly_repeated(response_text, msg.get("content", "")):
            return "repeats_recent_assistant_reply"
        checked += 1
        if checked >= max_assistant_messages:
            break
    return None


def compute_repetition_penalty(
    response_text: str,
    chat_history: List[Dict[str, str]],
    current_message: Optional[str] = None,
) -> float:
    """
    Compute a penalty for responses that repeat previous messages.

    Greatly penalizes exact matches (ignoring case/whitespace) with the
    current user message or the last assistant message.  Mildly penalizes
    same bag-of-words in different order.  Applies lighter penalties for
    matching older messages.

    Args:
        response_text: The candidate response text to evaluate.
        chat_history: The chat history *before* the current turn.
        current_message: The user message that triggered this response.
            Because scoring runs before the message is appended to
            chat_history, this must be supplied separately.
    """
    if not response_text:
        return 0.0

    if not chat_history and not current_message:
        return 0.0

    # Transform requests expect output that reads like existing text (the
    # user's paste or our previous reply), so similarity is compliance, not
    # looping. Without this the soft -400/-500 penalties made the compliant
    # candidate lose to one that ignored the request even after the hard
    # rung (find_repeated_reply) stood down -- content-quality deltas
    # between candidates are only ~100 points.
    if current_message and user_requested_transformation(current_message):
        return 0.0

    penalty = 0.0
    normalized_response = normalize_text(response_text)
    response_bow = bag_of_words(response_text)

    # --- Check against the current user message (not yet in history) ---
    # Severity order: exact > near-verbatim > bag-of-words. The near-repeat
    # check runs BEFORE bag-of-words equality so a long reply sharing the
    # exact word set (which IS a near-verbatim reorder) gets the stronger
    # penalty; short reorders never trip the similarity path and still land
    # in the mild bag-of-words bucket. Stored-content markers are exempt for
    # the same reason as in find_repeated_reply: they are system-written, so
    # a reply that acknowledges them in similar words is not an echo.
    if current_message and not is_stored_message_marker(current_message):
        normalized_current = normalize_text(current_message)
        if normalized_response == normalized_current:
            penalty += EXACT_REPEAT_PENALTY
            logger.debug(
                "Response is exact repeat of current user message, applying heavy penalty"
            )
        elif is_mostly_repeated(response_text, current_message):
            penalty += NEAR_REPEAT_PENALTY
            logger.debug(
                "Response is a near-verbatim repeat of current user message, penalizing"
            )
        elif response_bow == bag_of_words(current_message):
            penalty += BAG_OF_WORDS_REPEAT_PENALTY
            logger.debug(
                "Response has same bag-of-words as current user message, applying mild penalty"
            )

    if not chat_history:
        return penalty

    # Find the last user and assistant messages from history
    last_user_msg: Optional[str] = None
    last_assistant_msg: Optional[str] = None
    for msg in reversed(chat_history):
        if msg.get("role") == "user" and last_user_msg is None:
            last_user_msg = msg.get("content", "")
        elif msg.get("role") == "assistant" and last_assistant_msg is None:
            last_assistant_msg = msg.get("content", "")
        if last_user_msg is not None and last_assistant_msg is not None:
            break

    # Heavy penalties for matching the immediate previous messages in
    # history. Same severity order as above: exact > near-verbatim >
    # bag-of-words.
    for prev_text in [last_user_msg, last_assistant_msg]:
        if not prev_text:
            continue
        normalized_prev = normalize_text(prev_text)
        if normalized_response == normalized_prev:
            penalty += EXACT_REPEAT_PENALTY
            logger.debug(
                "Response is exact repeat of previous message, applying heavy penalty"
            )
        elif is_mostly_repeated(response_text, prev_text):
            penalty += NEAR_REPEAT_PENALTY
            logger.debug(
                "Response is a near-verbatim repeat of previous message, penalizing"
            )
        elif response_bow == bag_of_words(prev_text):
            penalty += BAG_OF_WORDS_REPEAT_PENALTY
            logger.debug(
                "Response has same bag-of-words as previous message, applying mild penalty"
            )

    # Lighter penalties for matching older messages
    for msg in chat_history:
        content = msg.get("content", "")
        if not content:
            continue
        # Skip the immediate messages we already checked
        if content == last_user_msg or content == last_assistant_msg:
            continue
        normalized_prev = normalize_text(content)
        if normalized_response == normalized_prev:
            if msg.get("role") == "assistant":
                penalty += OLDER_ASSISTANT_REPEAT_PENALTY
                logger.debug(
                    "Response repeats an older assistant message, applying -20 penalty"
                )
            else:
                penalty += OLDER_USER_REPEAT_PENALTY
                logger.debug(
                    "Response repeats an older user message, applying -10 penalty"
                )

    return penalty


def score_llm_response(
    result: Optional[Tuple[Optional[str], Optional[str]]],
    call_score: int,
    is_primary_call: bool = True,
    chat_history: Optional[List[Dict[str, str]]] = None,
    current_message: Optional[str] = None,
    rejection_stats: Optional[Dict[str, int]] = None,
    eligibility_verified: bool = False,
) -> float:
    """
    Score an LLM response for quality and safety.

    Args:
        result: Tuple of (response_text, context_part) from LLM
        call_score: Base score from model quality
        is_primary_call: Whether this is a primary (not retry) call
        chat_history: Current chat history for context-aware scoring
        current_message: The user message that triggered this response.
            Passed separately because it is not yet in chat_history
            at scoring time.
        rejection_stats: Optional mutable dict; hard rejections increment
            its "repeated_rejected" counter so the caller can tell "no
            usable response" apart from "responses arrived but all repeated
            earlier replies" and retry with an anti-repeat instruction.
        eligibility_verified: True when the medicaid_eligibility checker has
            actually produced a determination for this chat (the tool's
            follow-up pass, or any later turn in the same session). While
            False, a candidate that flatly asserts an eligibility verdict is
            inventing it: it forfeits its model prior and takes
            INVENTED_ELIGIBILITY_VERDICT_PENALTY on top.

    Returns:
        Float score (higher is better, -inf for invalid responses).
        -inf is a HARD rejection: best_within_timelimit never returns a
        result scored -inf, so repeats can't be delivered from this pass.
    """
    if result is None:
        return float("-inf")

    response_text, context_part = result

    if not context_part and not response_text:
        return float("-inf")

    # HARD rejection of (near-)repeated replies. Soft penalties are not
    # enough here: per-model base scores reach ~8800 (quality**2 scaling),
    # so a -500 nudge still let a verbatim repeat from the favorite backend
    # win the fan-out race — which is exactly the observed chat loop.
    # Skipped when the user explicitly asked us to repeat ourselves.
    if (
        response_text
        and len(response_text) > MIN_RESPONSE_LENGTH
        and not user_requested_repeat(current_message)
    ):
        repeat_reason = find_repeated_reply(
            response_text, chat_history, current_message
        )
        if repeat_reason:
            if rejection_stats is not None:
                rejection_stats["repeated_rejected"] = (
                    rejection_stats.get("repeated_rejected", 0) + 1
                )
            logger.info(f"Hard-rejecting candidate chat reply: {repeat_reason}")
            return float("-inf")

    score = 0.0
    invented_verdict = False

    # A tool call is a legitimate reply shape even though it carries no
    # user-facing prose or panda context summary -- the tool's follow-up pass
    # writes those. Counted with the same patterns the tool bonus uses (NOT
    # contains_tool_call, which also fires on a bare mention of a token) so
    # only a real call earns the full model prior below.
    tool_call_matches = count_tool_invocations(response_text)
    has_tool_call = tool_call_matches > 0

    # Bonus for being a primary call
    if is_primary_call:
        score += 100

    # Score context quality
    if context_part and len(context_part) > MIN_RESPONSE_LENGTH:
        score += 10
        if BAD_CONTEXT_PATTERNS.search(context_part):
            score -= 5

    # Score response quality
    if response_text and len(response_text) > MIN_RESPONSE_LENGTH:
        score += 100

        # Penalize responses that leak system prompts
        if BAD_RESPONSE_PATTERNS.search(response_text):
            score -= 75

        # Bonus for tool usage (search anywhere in response).
        # count_tool_invocations applies TOOL_DETECT_FLAGS, which the
        # ^...$-anchored patterns need -- a flag-less search only ever
        # matched a tool call at the very start of the reply.
        score += 100 * tool_call_matches

        # Safety: Penalize false promises
        if detect_false_promises(response_text):
            score -= 200
            logger.warning("Detected false promise in response, penalizing score")

        # Safety: an eligibility verdict the checker never computed is a
        # guess about someone's health coverage dressed up as a
        # determination. The prior is FORFEITED below rather than nudged --
        # see the invented_verdict handling after the base score is added.
        if not eligibility_verified and detect_eligibility_verdict(response_text):
            invented_verdict = True
            logger.warning(
                "Response asserts a Medicaid/Medicare eligibility verdict the "
                "checker never computed, forfeiting its model prior"
            )

        # Bonus for referencing an uploaded document by name (derived from history)
        if chat_history:
            response_lower = response_text.lower()
            for doc_name in _extract_document_names_from_history(chat_history):
                if doc_name.lower() in response_lower:
                    score += 150
                    logger.debug(
                        f"Response references uploaded document '{doc_name}', boosting score"
                    )
                    break

        # Penalize repetitive responses so cleaner alternatives are preferred
        rep_penalty = repetition_penalty(response_text) * 200
        if rep_penalty > 0:
            score -= rep_penalty
            logger.debug(f"Repetition penalty: -{rep_penalty:.0f}")

        # Always penalize asking for patient name — it's known client-side
        if ASKS_FOR_PATIENT_NAME.search(response_text):
            score -= 200
            logger.debug(
                "Response asks for patient name, penalizing (known client-side)"
            )

        # Penalize responses that repeat previous messages -- unless the
        # user explicitly asked for a repeat: every other rung of the
        # ladder (hard rejection, self-heal, retry last-resort penalty) is
        # exempted on those turns, and leaving this -400/-500 nudge in
        # place made the compliant repeat lose to a candidate that ignored
        # the user's request.
        if (chat_history or current_message) and not user_requested_repeat(
            current_message
        ):
            score += compute_repetition_penalty(
                response_text, chat_history or [], current_message
            )

    # Add base quality score from model. A reply with no context summary is
    # normally half-finished, so it only gets a hundredth of the model
    # prior -- but a TOOL CALL has no summary by design, and dividing its
    # prior sank real tool calls (score ~315) under chatty candidates that
    # skipped the tool (~5810). That is how an eligibility verdict invented by
    # a model instead of computed by the checker won a fan-out.
    prior_applied = 0.0
    if response_text and (context_part or has_tool_call):
        prior_applied = call_score
    else:
        prior_applied = call_score / 100
    score += prior_applied

    # An invented eligibility verdict forfeits the model prior outright, then
    # takes a fixed penalty on top.
    #
    # A fixed nudge alone could not work: the prior is the dominant term and
    # the gaps between priors are bigger than any constant worth using. One
    # quality-210 backend contributes BOTH a truncated-history call
    # (210**2//5 = 8820) and a full-history one (210**2//4 = 11025), so a
    # 2000-point penalty still left the full-history call's invented verdict
    # (~9235) beating the truncated call's real checker invocation (~9120) --
    # the exact swap this guard exists to prevent, from a single backend.
    # Cross-backend gaps are larger still.
    #
    # Forfeiting the prior leaves only the content signals, so an invented
    # verdict loses to any candidate that either called the checker or
    # answered without a verdict, whatever backend it came from. It is
    # deliberately NOT a hard -inf rejection like a repeated reply: the
    # detector is a regex over free text, and one false positive must not be
    # able to empty a fan-out and break the turn. When every candidate
    # invents a verdict they are all levelled equally and the best of them
    # still gets delivered.
    # Subtract exactly what was added: a candidate with no context summary and
    # no tool call only received call_score/100, so taking back the full
    # call_score drove it thousands of points below its peers and inverted the
    # ranking among invented verdicts on model quality -- the opposite of the
    # "levelled equally" property this is supposed to have.
    if invented_verdict:
        score -= prior_applied + INVENTED_ELIGIBILITY_VERDICT_PENALTY

    logger.debug(f"Scored response as {score}")
    return score


# Per-session demotion for backends whose candidates were hard-rejected as
# repeats on earlier TURNS of the same chat session: each strike multiplies
# the backend's base score by the decay (capped), so a persistent looper
# decays from internal-tier preference (~8000 base) toward external-tier
# (~1900) after ~4 looping turns instead of winning every race on raw quality
# forever. Content-quality signals are unaffected -- only the model-preference
# prior decays. Strikes are in-memory per ChatInterface (one WebSocket
# session) and never persisted.
#
# At most ONE strike per backend per scorer (i.e. per turn): the number of
# candidates a backend contributes to a fan-out is a property of the fan-out
# shape -- the primary fhi backend is listed twice, each backend may get a
# truncated- and a full-history call, and each message variant multiplies
# again -- so counting per candidate ranked backends by how many slots they
# occupy rather than how often they loop, and saturated the cap inside a
# single turn.
REPEAT_OFFENDER_DECAY = 0.7
REPEAT_OFFENDER_CAP = 4


def create_response_scorer(
    call_scores: Dict[Awaitable, int],
    primary_calls: Optional[List[Awaitable]] = None,
    chat_history: Optional[List[Dict[str, str]]] = None,
    current_message: Optional[str] = None,
    rejection_stats: Optional[Dict[str, int]] = None,
    call_labels: Optional[Dict[Awaitable, str]] = None,
    repeat_offenders: Optional[Dict[str, int]] = None,
    score_log: Optional[Dict[Awaitable, float]] = None,
    eligibility_verified: bool = False,
) -> Callable[[Optional[Tuple[Optional[str], Optional[str]]], Awaitable], float]:
    """
    Create a scoring function for use with best_within_timelimit.

    Args:
        call_scores: Dict mapping call awaitables to their base scores
        primary_calls: List of primary (non-retry) calls for bonus scoring
        chat_history: Current chat history for context-aware scoring
        current_message: The user message that triggered this response.
            Passed separately because it is not yet in chat_history
            at scoring time.
        rejection_stats: Optional mutable dict shared with the caller; see
            score_llm_response.
        call_labels: Optional dict mapping call awaitables to backend labels
            (filled by the build_* helpers). Enables the repeat-offender
            demotion and the per-candidate score log.
        repeat_offenders: Optional mutable dict (label -> strike count)
            owned by the caller and shared across turns of one chat
            session. Strikes demote the backend's base score (see
            REPEAT_OFFENDER_DECAY) and each hard-rejected repeat from a
            labeled call adds a strike.
        score_log: Optional mutable dict; every scored call's final score is
            recorded here (keyed by the original awaitable) so the caller
            can report per-candidate scores in debug output.
        eligibility_verified: Whether the medicaid_eligibility checker has
            produced a determination for this chat; see score_llm_response.

    Returns:
        Scoring function compatible with best_within_timelimit
    """
    primary_set = set(primary_calls) if primary_calls else set()
    # Labels already struck by THIS scorer, so one looping turn costs a
    # backend one strike no matter how many candidates it contributed.
    struck_this_turn: set = set()

    def score_fn(
        result: Optional[Tuple[Optional[str], Optional[str]]],
        original_task: Awaitable,
    ) -> float:
        call_score = call_scores.get(original_task, 0)
        label = call_labels.get(original_task) if call_labels else None
        if label and repeat_offenders:
            # Strikes earned on EARLIER turns only: struck_this_turn keeps the
            # current turn's strike from also demoting the candidates being
            # scored alongside it.
            earlier_strikes = repeat_offenders.get(label, 0) - (
                1 if label in struck_this_turn else 0
            )
            strikes = min(max(earlier_strikes, 0), REPEAT_OFFENDER_CAP)
            if strikes:
                call_score = int(call_score * (REPEAT_OFFENDER_DECAY**strikes))
        is_primary = original_task in primary_set
        rejected_before = (
            rejection_stats.get("repeated_rejected", 0)
            if rejection_stats is not None
            else 0
        )
        score = score_llm_response(
            result,
            call_score,
            is_primary,
            chat_history=chat_history,
            current_message=current_message,
            rejection_stats=rejection_stats,
            eligibility_verified=eligibility_verified,
        )
        if (
            label
            and label not in struck_this_turn
            and repeat_offenders is not None
            and rejection_stats is not None
            and rejection_stats.get("repeated_rejected", 0) > rejected_before
        ):
            struck_this_turn.add(label)
            repeat_offenders[label] = repeat_offenders.get(label, 0) + 1
        if score_log is not None:
            score_log[original_task] = score
        return score

    return score_fn


def build_llm_calls(
    model_backends: List[RemoteModelLike],
    current_message: str,
    previous_context_summary: Optional[str],
    history: List[Dict[str, str]],
    is_professional: bool,
    is_logged_in: bool,
    full_history: Optional[List[Dict[str, str]]] = None,
    allow_repeated_reply: bool = False,
    call_labels: Optional[Dict[Awaitable, str]] = None,
) -> Tuple[List[Awaitable[Tuple[Optional[str], Optional[str]]]], Dict[Awaitable, int]]:
    """
    Build parallel LLM calls for multiple model backends.

    Args:
        model_backends: List of model backends to call
        current_message: Current message to send
        previous_context_summary: Summary of previous context
        history: Truncated message history
        is_professional: Whether user is a professional
        is_logged_in: Whether user is logged in
        full_history: Full untruncated history (optional)
        call_labels: Optional mutable dict filled with call -> backend label
            (str(backend)); lets the scorer attribute candidates to models
            for repeat-offender demotion and debug reporting.

    Returns:
        Tuple of (list of call awaitables, dict mapping calls to quality scores)
    """
    calls: List[Awaitable[Tuple[Optional[str], Optional[str]]]] = []
    call_scores: Dict[Awaitable, int] = {}

    def _label(call: Awaitable, model_backend: RemoteModelLike) -> None:
        if call_labels is not None:
            call_labels[call] = str(model_backend)

    for model_backend in model_backends:
        # Try with truncated history
        call = model_backend.generate_chat_response(
            current_message,
            previous_context_summary=previous_context_summary,
            history=history,
            is_professional=is_professional,
            is_logged_in=is_logged_in,
            allow_repeated_reply=allow_repeated_reply,
        )
        _label(call, model_backend)
        calls.append(call)
        # Quadratic scaling to more aggressively prefer higher-quality models
        call_scores[call] = (model_backend.quality() ** 2) // 5

        # Also try with full history if provided and model can handle it
        if full_history and full_history != history:
            full_history_tokens = estimate_history_tokens(full_history)
            model_max_context = model_backend.get_max_context()
            # Leave room for system prompt and response (~8k tokens)
            available_context = model_max_context - 8000

            if full_history_tokens < available_context:
                full_history_call = model_backend.generate_chat_response(
                    current_message,
                    previous_context_summary=previous_context_summary,
                    history=full_history,
                    is_professional=is_professional,
                    is_logged_in=is_logged_in,
                    allow_repeated_reply=allow_repeated_reply,
                )
                _label(full_history_call, model_backend)
                calls.append(full_history_call)
                # Slightly prefer full history for better context
                call_scores[full_history_call] = (model_backend.quality() ** 2) // 4

    return calls, call_scores


def build_llm_calls_for_variants(
    model_backends: List[RemoteModelLike],
    variants: List[MessageVariant],
    previous_context_summary: Optional[str],
    history: List[Dict[str, str]],
    is_professional: bool,
    is_logged_in: bool,
    full_history: Optional[List[Dict[str, str]]] = None,
    allow_repeated_reply: bool = False,
    call_labels: Optional[Dict[Awaitable, str]] = None,
) -> Tuple[
    List[Awaitable[Tuple[Optional[str], Optional[str]]]],
    Dict[Awaitable, int],
    List[Awaitable],
]:
    """
    Build parallel LLM calls from a list of message variants.

    Each variant's ``text_for_llm`` is fanned out across the backends via
    :func:`build_llm_calls`, then the variant's ``score_delta`` is added to every
    resulting call's base score. Only calls from the ``primary_original`` variant
    are returned in ``primary_calls`` (so they keep the is-primary scoring bonus);
    alternative-variant calls do not. A valid primary therefore outranks the
    alternatives, while a failed primary (scored ``-inf`` by ``score_llm_response``)
    lets the best alternative win.

    Args:
        model_backends: List of model backends to call.
        variants: Ordered message variants (primary first when present).
        previous_context_summary: Summary of previous context.
        history: Truncated message history.
        is_professional: Whether the user is a professional.
        is_logged_in: Whether the user is logged in.
        full_history: Full untruncated history (optional).

    Returns:
        Tuple of (all calls, call->score dict, primary-variant calls).
    """
    all_calls: List[Awaitable[Tuple[Optional[str], Optional[str]]]] = []
    all_scores: Dict[Awaitable, int] = {}
    primary_calls: List[Awaitable] = []

    for variant in variants:
        calls, call_scores = build_llm_calls(
            model_backends=model_backends,
            current_message=variant.text_for_llm,
            previous_context_summary=previous_context_summary,
            history=history,
            is_professional=is_professional,
            is_logged_in=is_logged_in,
            full_history=full_history,
            allow_repeated_reply=allow_repeated_reply,
            call_labels=call_labels,
        )
        # Apply the variant's score delta to every call it produced.
        for call in calls:
            call_scores[call] = call_scores.get(call, 0) + variant.score_delta

        all_calls.extend(calls)
        all_scores.update(call_scores)
        if variant.kind == "primary_original":
            primary_calls.extend(calls)

        # Non-PHI logging only: variant kind, delta, and call count.
        logger.debug(
            f"build_llm_calls_for_variants: variant={variant.kind} "
            f"delta={variant.score_delta} calls={len(calls)}"
        )

    return all_calls, all_scores, primary_calls


def build_retry_calls(
    model_backends: List[RemoteModelLike],
    current_message: str,
    previous_context_summary: Optional[str],
    history: List[Dict[str, str]],
    is_professional: bool,
    is_logged_in: bool,
    fallback_backends: Optional[List[RemoteModelLike]] = None,
    temperature: float = 0.7,
    allow_repeated_reply: bool = False,
    call_labels: Optional[Dict[Awaitable, str]] = None,
) -> Tuple[List[Awaitable[Tuple[Optional[str], Optional[str]]]], Dict[Awaitable, int]]:
    """
    Build retry LLM calls with shortened context and fallback backends.

    Args:
        model_backends: Primary model backends to retry
        current_message: Current message to send
        previous_context_summary: Summary of previous context
        history: Message history (will be shortened if needed)
        is_professional: Whether user is a professional
        is_logged_in: Whether user is logged in
        fallback_backends: Optional backup model backends
        temperature: Sampling temperature for the retry calls. Anti-repeat
            retries pass a higher value to break deterministic loops.
        call_labels: Optional mutable dict filled with call -> backend label
            (see build_llm_calls).

    Returns:
        Tuple of (list of call awaitables, dict mapping calls to quality scores)
    """
    calls: List[Awaitable[Tuple[Optional[str], Optional[str]]]] = []
    call_scores: Dict[Awaitable, int] = {}

    def _label(call: Awaitable, model_backend: RemoteModelLike) -> None:
        if call_labels is not None:
            call_labels[call] = str(model_backend)

    # Use shorter history for retry (Python gracefully handles if len < 5)
    start_idx = max(0, len(history) - 5)
    retry_history = ensure_message_alternation(history[start_idx:])

    # Try primary backends with shortened history
    for model_backend in model_backends:
        call = model_backend.generate_chat_response(
            current_message,
            previous_context_summary=previous_context_summary,
            history=retry_history,
            is_professional=is_professional,
            is_logged_in=is_logged_in,
            temperature=temperature,
            allow_repeated_reply=allow_repeated_reply,
        )
        _label(call, model_backend)
        calls.append(call)
        # Quadratic scaling, reduced for retry (lower priority than primary)
        call_scores[call] = (model_backend.quality() ** 2) // 7

    # Also try with full history (some models may handle it)
    for model_backend in model_backends:
        call = model_backend.generate_chat_response(
            current_message,
            previous_context_summary=previous_context_summary,
            history=history,
            is_professional=is_professional,
            is_logged_in=is_logged_in,
            temperature=temperature,
            allow_repeated_reply=allow_repeated_reply,
        )
        _label(call, model_backend)
        calls.append(call)
        call_scores[call] = (model_backend.quality() ** 2) // 2

    # Add fallback backends with both short and full histories
    if fallback_backends:
        for model_backend in fallback_backends:
            # Short history for fallbacks (better success rate)
            call = model_backend.generate_chat_response(
                current_message,
                previous_context_summary=previous_context_summary,
                history=retry_history,
                is_professional=is_professional,
                is_logged_in=is_logged_in,
                temperature=temperature,
                allow_repeated_reply=allow_repeated_reply,
            )
            _label(call, model_backend)
            calls.append(call)
            call_scores[call] = (model_backend.quality() ** 2) // 7

            # Full history for fallbacks (if model can handle it)
            call = model_backend.generate_chat_response(
                current_message,
                previous_context_summary=previous_context_summary,
                history=history,
                is_professional=is_professional,
                is_logged_in=is_logged_in,
                temperature=temperature,
                allow_repeated_reply=allow_repeated_reply,
            )
            _label(call, model_backend)
            calls.append(call)
            call_scores[call] = (model_backend.quality() ** 2) // 5

    return calls, call_scores


# --- Alternate ("side-by-side") answer support ---

# An alternate answer is only offered when it is meaningfully different from
# the primary one; above this similarity it reads as a reworded duplicate.
ALTERNATE_MAX_SIMILARITY = 0.7
# And only when it is a substantial reply on its own.
ALTERNATE_MIN_CHARS = 30


def alternate_is_presentable(
    alternate_text: Optional[str],
    primary_text: str,
    chat_history: Optional[List[Dict[str, str]]] = None,
    current_message: Optional[str] = None,
) -> bool:
    """Whether a runner-up reply is safe and useful to show side-by-side.

    The alternate is shown raw (no tool processing runs on it), so anything
    containing tool calls or action tokens is out, as is anything unsafe,
    near-duplicate of the primary, or itself a repeat of the conversation.
    """
    if not alternate_text:
        return False
    text = alternate_text.strip()
    if len(text) < ALTERNATE_MIN_CHARS:
        return False
    if "🐼" in text:
        return False
    # Screened with the same flags the tool handlers use: a flag-less sweep
    # over the `^...$`-anchored patterns only caught a tool call at the very
    # start of the reply, so an alternate whose tool call followed a line of
    # prose was shown to the user as raw syntax plus its JSON payload.
    if contains_tool_call(text):
        return False
    if BAD_RESPONSE_PATTERNS.search(text):
        return False
    if detect_false_promises(text):
        return False
    if ASKS_FOR_PATIENT_NAME.search(text):
        return False
    if response_similarity(text, primary_text) >= ALTERNATE_MAX_SIMILARITY:
        return False
    if find_repeated_reply(text, chat_history, current_message):
        return False
    return True


# Runner-up must score at least this fraction of the winner for the pair to
# count as "closely tied" (the only case where a side-by-side alternate is
# offered). Base call scores are quadratic in model quality (internal ~8000
# vs external ~1900 at //5), so a cross-tier runner-up never comes close and
# the alternate stays reserved for genuinely comparable answers -- e.g. the
# same model's truncated- vs full-history variants (8000 vs 10000 base,
# ratio 0.8) or two same-tier backends split by content-quality signals.
ALTERNATE_CLOSE_TIE_RATIO = 0.8


def scores_closely_tied(
    best_score: float,
    runner_up_score: float,
    ratio: float = ALTERNATE_CLOSE_TIE_RATIO,
) -> bool:
    """Whether the fan-out's top two scores are close enough that showing
    the user both answers (side-by-side alternate) beats auto-picking.

    Both scores must be positive and finite: a non-positive winner means the
    turn was already degraded (heavy penalties), and -inf/absent runner-ups
    were never usable. With positive scores the ratio test is monotonic and
    scale-free against the quadratic base-score tiers.
    """
    if not (math.isfinite(best_score) and math.isfinite(runner_up_score)):
        return False
    if best_score <= 0 or runner_up_score <= 0:
        return False
    return runner_up_score >= best_score * ratio

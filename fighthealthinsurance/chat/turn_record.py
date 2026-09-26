"""One ChatTurn row per chat turn: which models raced, how each call ended,
which model won, and which side-by-side answer the person picked.

Three pieces:

* ``CallLog`` observes the calls of one LLM pass. The build_* helpers in
  llm_client wrap each backend call with it when one is passed, and the pass
  wraps its scorer with ``CallLog.scoring``. ``finish`` turns the pass into
  plain dicts: model label, backend descriptor, pass, history kind, status,
  class name of any exception, time and score.
* ``TurnRecord`` gathers every pass of one turn in memory.
* ``arecord_chat_turn`` writes the row once, and ``arecord_answer_preference``
  stores the person's side-by-side pick on it. Neither ever raises.

Only metadata is kept: model labels, backend descriptors, scores, times and
the enum values in ChatTurn. No message, reply, summary, history, context,
state hint or document name reaches anything here, and an exception is kept
as its class name only.
"""

import math
import time
import uuid
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Dict,
    List,
    Optional,
    Sequence,
    TypeVar,
)

# channels' database_sync_to_async (not asgiref's sync_to_async): chat runs
# outside the HTTP request cycle, so only its close_old_connections wrapping
# ever closes the DB connections these writes open.
from channels.db import database_sync_to_async
from django.utils import timezone
from loguru import logger

T = TypeVar("T")

PASS_PRIMARY = "primary"
PASS_RETRY = "retry"
PASS_TOOL = "tool"

# How a call ended. scored: returned a usable candidate. repeat: returned
# text the scorer hard-rejected as a repeat of a recent reply. empty:
# returned nothing usable. error: raised. late: still running (or never
# started) when the pass stopped waiting.
STATUS_SCORED = "scored"
STATUS_REPEAT = "repeat"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"
STATUS_LATE = "late"
CALL_STATUSES = (STATUS_SCORED, STATUS_REPEAT, STATUS_EMPTY, STATUS_ERROR, STATUS_LATE)
# Calls that returned an answer, usable or not. Their times feed the medians.
COMPLETED_STATUSES = frozenset({STATUS_SCORED, STATUS_REPEAT, STATUS_EMPTY})

TURN_OUTCOMES = frozenset({"ok", "failed", "timeout"})
# The labels fhi_chat_answer_feedback_total counts, and ChatTurn.preferred's
# non-empty values.
PREFERENCE_LABELS = frozenset({"primary", "alternate"})

# Bounds for the strings a row carries (the CharField sizes on ChatTurn).
_MODEL_LABEL_MAX = 200
_BACKEND_MAX = 300
_ENUM_MAX = 32
_ERROR_NAME_MAX = 64


def _label(backend: Any) -> str:
    return str(backend)[:_MODEL_LABEL_MAX]


def _descriptor(backend: Any) -> str:
    """The backend instance's class, wire model and host:port, when the
    backend can say (test stubs cannot)."""
    describe = getattr(backend, "backend_descriptor", None)
    if not callable(describe):
        return ""
    try:
        return str(describe())[:_BACKEND_MAX]
    except Exception:
        return ""


def _is_external(backend: Any) -> Optional[bool]:
    try:
        value = getattr(backend, "external", None)
    except Exception:
        return None
    return value if isinstance(value, bool) else None


@dataclass
class _CallRecord:
    model: str
    backend: str
    external: Optional[bool]
    history: str
    variant: str = ""
    started: Optional[float] = None
    finished: Optional[float] = None
    error: str = ""
    has_text: bool = False


async def _observed(call: Awaitable[T], record: _CallRecord) -> T:
    """Await one backend call, noting when it ran, whether it raised (class
    name only) and whether it returned any reply text. The text itself is
    never kept."""
    record.started = time.monotonic()
    try:
        result = await call
    except Exception as e:
        record.error = type(e).__name__[:_ERROR_NAME_MAX]
        raise
    finally:
        record.finished = time.monotonic()
    if isinstance(result, tuple) and result:
        record.has_text = bool(result[0])
    return result


class CallLog:
    """The calls of one LLM pass, keyed by the awaitable the pass races."""

    def __init__(self, pass_kind: str, depth: int = 0):
        self.pass_kind = pass_kind
        self.depth = depth
        self._records: Dict[Awaitable, _CallRecord] = {}
        self._scores: Dict[Awaitable, float] = {}

    def observe(
        self, call: Awaitable[T], backend: Any, history: str
    ) -> Coroutine[Any, Any, T]:
        """Wrap one backend call. The returned awaitable replaces the call
        everywhere the pass keys by it (labels, base scores, the race)."""
        record = _CallRecord(
            model=_label(backend),
            backend=_descriptor(backend),
            external=_is_external(backend),
            history=history,
        )
        wrapped = _observed(call, record)
        self._records[wrapped] = record
        return wrapped

    def set_variant(self, call: Awaitable, kind: str) -> None:
        record = self._records.get(call)
        if record is not None:
            record.variant = str(kind)[:_ENUM_MAX]

    def scoring(
        self, score_fn: Callable[[Any, Awaitable], float]
    ) -> Callable[[Any, Awaitable], float]:
        """Wrap the pass's scorer so every score it hands out is noted."""

        def recording(result: Any, task: Awaitable) -> float:
            score = score_fn(result, task)
            self._scores[task] = score
            return score

        return recording

    def finish(self) -> List[Dict[str, Any]]:
        """Each call of the pass as a plain dict, in fan-out order.

        A call the scorer never saw is late unless it raised. A rejected
        score (-inf) becomes null, since Postgres jsonb refuses -Infinity.
        In the primary and tool passes a rejected call with reply text was a
        repeat; the retry scorer never hard-rejects repeats, so there a
        rejected call is always empty.
        """
        out: List[Dict[str, Any]] = []
        for call, record in self._records.items():
            score = self._scores.get(call)
            if record.error:
                status = STATUS_ERROR
            elif score is None:
                status = STATUS_LATE
            elif math.isfinite(score):
                status = STATUS_SCORED
            elif record.has_text and self.pass_kind != PASS_RETRY:
                status = STATUS_REPEAT
            else:
                status = STATUS_EMPTY
            ms: Optional[int] = None
            if (
                status != STATUS_LATE
                and record.started is not None
                and record.finished is not None
            ):
                ms = max(0, int((record.finished - record.started) * 1000))
            out.append(
                {
                    "model": record.model,
                    "backend": record.backend,
                    "external": record.external,
                    "pass": self.pass_kind,
                    "depth": self.depth,
                    "history": record.history,
                    "variant": record.variant,
                    "status": status,
                    "error": record.error,
                    "ms": ms,
                    "score": (
                        float(score)
                        if score is not None and math.isfinite(score)
                        else None
                    ),
                }
            )
        return out


def _finite_or_none(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _ms_since(started: Optional[float]) -> Optional[int]:
    if started is None:
        return None
    return max(0, int((time.monotonic() - started) * 1000))


@dataclass
class TurnRecord:
    """Everything one turn's row will hold, gathered while the turn runs."""

    use_external: bool
    backends: List[str]
    fallback_backends: List[str]
    # Label -> whether that model is an outside one, from the backends the
    # router handed this turn.
    external_by_label: Dict[str, Optional[bool]]
    turn_id: uuid.UUID = field(default_factory=uuid.uuid4)
    started: float = field(default_factory=time.monotonic)
    calls: List[Dict[str, Any]] = field(default_factory=list)
    winner_model: str = ""
    winner_score: Optional[float] = None
    winner_pass: str = ""
    runner_up_model: str = ""
    runner_up_score: Optional[float] = None
    closely_tied: bool = False
    rejected_repeats: int = 0
    delivered_repeat: bool = False
    retry_ran: bool = False
    retry_used: bool = False
    tool_passes: int = 0
    tool_rewrote: bool = False
    fanout_ms: Optional[int] = None
    # The runner-up picked as the side-by-side alternate, until delivery
    # decides whether it is actually shown.
    candidate_alternate_model: str = ""
    candidate_cross_model: bool = False
    alternate_offered: bool = False
    alternate_model: str = ""
    alternate_cross_model: bool = False

    @classmethod
    def start(
        cls,
        use_external: bool,
        primary_models: Sequence[Any],
        fallback_models: Optional[Sequence[Any]] = None,
    ) -> "TurnRecord":
        fallback_models = fallback_models or []
        external_by_label: Dict[str, Optional[bool]] = {}
        for backend in list(primary_models) + list(fallback_models):
            external_by_label.setdefault(_label(backend), _is_external(backend))
        return cls(
            use_external=bool(use_external),
            backends=[_label(b) for b in primary_models],
            fallback_backends=[_label(b) for b in fallback_models],
            external_by_label=external_by_label,
        )

    def mark_fanout_done(self, pass_started: float) -> None:
        self.fanout_ms = _ms_since(pass_started)

    def set_winner(
        self,
        model: Optional[str],
        score: Optional[float],
        from_retry: bool,
        runner_up_model: Optional[str],
        runner_up_score: Optional[float],
        closely_tied: bool,
    ) -> None:
        self.winner_model = (model or "")[:_MODEL_LABEL_MAX]
        self.winner_score = _finite_or_none(score)
        self.winner_pass = PASS_RETRY if from_retry else PASS_PRIMARY
        self.retry_used = from_retry
        self.runner_up_model = (runner_up_model or "")[:_MODEL_LABEL_MAX]
        self.runner_up_score = _finite_or_none(runner_up_score)
        self.closely_tied = bool(closely_tied)

    def set_alternate_candidate(self, model: Optional[str], cross_model: bool) -> None:
        self.candidate_alternate_model = (model or "")[:_MODEL_LABEL_MAX]
        self.candidate_cross_model = bool(cross_model)

    def clear_alternate_candidate(self) -> None:
        self.candidate_alternate_model = ""
        self.candidate_cross_model = False

    def offer_alternate(self) -> None:
        """The candidate alternate passed the delivery checks and is shown."""
        self.alternate_offered = True
        self.alternate_model = self.candidate_alternate_model
        self.alternate_cross_model = self.candidate_cross_model

    def row_fields(self, outcome: str) -> Dict[str, Any]:
        """Keyword arguments for ChatTurn.objects.create (minus the chat)."""
        winner_external = (
            self.external_by_label.get(self.winner_model) if self.winner_model else None
        )
        return {
            "id": self.turn_id,
            "outcome": outcome,
            "use_external": self.use_external,
            "backends": list(self.backends),
            "fallback_backends": list(self.fallback_backends),
            "calls": list(self.calls),
            "winner_model": self.winner_model,
            "winner_score": self.winner_score,
            "winner_pass": self.winner_pass,
            "winner_external": winner_external,
            "runner_up_model": self.runner_up_model,
            "runner_up_score": self.runner_up_score,
            "closely_tied": self.closely_tied,
            "rejected_repeats": max(0, int(self.rejected_repeats)),
            "delivered_repeat": self.delivered_repeat,
            "retry_ran": self.retry_ran,
            "retry_used": self.retry_used,
            "tool_passes": min(max(0, int(self.tool_passes)), 32767),
            "tool_rewrote": self.tool_rewrote,
            "fanout_ms": self.fanout_ms,
            "turn_ms": _ms_since(self.started),
            "alternate_offered": self.alternate_offered,
            "alternate_model": self.alternate_model if self.alternate_offered else "",
            "alternate_cross_model": (
                self.alternate_cross_model if self.alternate_offered else False
            ),
        }


def _record_chat_turn_sync(chat_id: Any, fields: Dict[str, Any]) -> None:
    from fighthealthinsurance.models import ChatTurn

    ChatTurn.objects.create(chat_id=chat_id, **fields)


async def arecord_chat_turn(
    chat_id: Any, turn: Optional[TurnRecord], outcome: str
) -> bool:
    """Write one turn's row. Returns whether it was written; never raises.

    Fails (and says so at WARNING, class name only) when the chat was
    deleted mid-turn: the row would have nothing to belong to.
    """
    if turn is None or outcome not in TURN_OUTCOMES:
        return False
    try:
        await database_sync_to_async(_record_chat_turn_sync)(
            chat_id, turn.row_fields(outcome)
        )
        return True
    except Exception as e:
        logger.warning(
            f"Could not record chat turn for chat {chat_id}: {type(e).__name__}"
        )
        return False


def _as_uuid(value: Any) -> Optional[uuid.UUID]:
    if isinstance(value, uuid.UUID):
        return value
    # Client input: anything that is not a short string is not an id.
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _record_preference_sync(
    chat_id: uuid.UUID, turn_id: uuid.UUID, preferred: str
) -> int:
    from fighthealthinsurance.models import ChatTurn

    # Conditional update: only a turn of this chat that offered an
    # alternate and has no pick yet, so the first pick wins and a repeated
    # frame, or one naming another chat's turn, changes nothing.
    return ChatTurn.objects.filter(
        pk=turn_id,
        chat_id=chat_id,
        alternate_offered=True,
        preferred="",
    ).update(preferred=preferred, preferred_at=timezone.now())


async def arecord_answer_preference(chat_id: Any, turn_id: Any, preferred: Any) -> bool:
    """Store which side-by-side answer the person picked on the turn's row.

    Does nothing unless both ids parse as UUIDs and ``preferred`` is one of
    PREFERENCE_LABELS. Returns whether a row changed; never raises.
    """
    try:
        if not isinstance(preferred, str) or preferred not in PREFERENCE_LABELS:
            return False
        chat_uuid = _as_uuid(chat_id)
        turn_uuid = _as_uuid(turn_id)
        if chat_uuid is None or turn_uuid is None:
            return False
        updated = await database_sync_to_async(_record_preference_sync)(
            chat_uuid, turn_uuid, preferred
        )
        return bool(updated)
    except Exception as e:
        logger.warning(f"Could not record answer preference: {type(e).__name__}")
        return False

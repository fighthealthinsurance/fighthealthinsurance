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

How a turn's row follows fhi_chat_turns_total. The row's outcome is always
the one the metric counted for the turn, and a turn the metric never counts
gets no row:

* A turn that ends normally writes its row after its reply or error frame.
* An exception that escapes a turn after its models were asked (while the
  reply is being saved, say) counts the turn "failed" in the metric, and the
  row says "failed", unless the metric had already counted it; then the row
  keeps that outcome. An exception before the models were asked leaves no
  row and no count.
* A turn cancelled before the metric counted it (a disconnect while the
  models are still answering, for instance) gets no row: the metric does
  not count cancelled turns either, and a row with no outcome would say
  nothing true. A turn cancelled after it was counted (while its reply
  frame was going out) keeps its row with the counted outcome, written by
  ``arecord_chat_turn_isolated``: on a thread of its own, waiting at most
  CANCELLED_TURN_WRITE_SECONDS, so the write can neither hold up the
  connection's teardown nor queue behind anything on the chat's executor.
"""

import asyncio
import math
import threading
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
# returned nothing usable. unscored: returned text, but its pass stopped
# comparing answers before scoring it (the turn budget ran out while the
# race still waited on slower calls, for instance). error: raised. late:
# still running (or never started) when the pass stopped waiting. skipped:
# held back by a staged fan-out and never sent, because one of our own
# models answered first.
STATUS_SCORED = "scored"
STATUS_REPEAT = "repeat"
STATUS_EMPTY = "empty"
STATUS_UNSCORED = "unscored"
STATUS_ERROR = "error"
STATUS_LATE = "late"
STATUS_SKIPPED = "skipped"
CALL_STATUSES = (
    STATUS_SCORED,
    STATUS_REPEAT,
    STATUS_EMPTY,
    STATUS_UNSCORED,
    STATUS_ERROR,
    STATUS_LATE,
    STATUS_SKIPPED,
)
# Calls that returned an answer, usable or not. Their times feed the medians.
COMPLETED_STATUSES = frozenset(
    {STATUS_SCORED, STATUS_REPEAT, STATUS_EMPTY, STATUS_UNSCORED}
)

TURN_OUTCOMES = frozenset({"ok", "failed", "timeout"})
OUTCOME_FAILED = "failed"
# The labels fhi_chat_answer_feedback_total counts, and ChatTurn.preferred's
# non-empty values.
PREFERENCE_LABELS = frozenset({"primary", "alternate"})

# Bounds for the strings a row carries (the CharField sizes on ChatTurn).
_MODEL_LABEL_MAX = 200
_BACKEND_MAX = 300
_ENUM_MAX = 32
# The width of ChatTurn.gate_scorer.
_SCORER_MAX = 80
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
    skipped: bool = False
    # The backend call itself, kept only so a call that is never sent can
    # be closed (see CallLog.mark_skipped).
    inner: Optional[Awaitable[Any]] = None


async def _observed(call: Awaitable[T], record: _CallRecord) -> T:
    """Await one backend call, noting when it ran, whether it raised (class
    name only) and whether it returned any reply text. The text itself is
    never kept.

    A call that is cancelled (still running when its race or the turn
    stopped waiting) gets no finish time: that is what marks it late.
    """
    record.started = time.monotonic()
    try:
        result = await call
    except Exception as e:
        record.finished = time.monotonic()
        record.error = type(e).__name__[:_ERROR_NAME_MAX]
        raise
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
            inner=call,
        )
        wrapped = _observed(call, record)
        self._records[wrapped] = record
        return wrapped

    def mark_skipped(self, calls: Sequence[Awaitable]) -> None:
        """Note calls a staged fan-out held back and never sent.

        Each one is closed along with the backend call it wraps: neither
        will ever be awaited, and closing them keeps Python from warning
        that they never were. A call that did start is left alone.
        """
        for call in calls:
            record = self._records.get(call)
            if record is None or record.started is not None:
                continue
            record.skipped = True
            inner, record.inner = record.inner, None
            for coroutine in (call, inner):
                close = getattr(coroutine, "close", None)
                if callable(close):
                    close()

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

        A rejected score (-inf) becomes null, since Postgres jsonb refuses
        -Infinity. In the primary and tool passes a rejected call with reply
        text was a repeat; the retry scorer never hard-rejects repeats, so
        there a rejected call is always empty.

        A call held back and never sent is skipped, with no time. Any other
        call the scorer never saw goes by its own finish time: one that
        finished is unscored when it returned text and empty when it did
        not, and keeps its time; only a call that never finished is late,
        with no time.
        """
        out: List[Dict[str, Any]] = []
        for call, record in self._records.items():
            record.inner = None
            score = self._scores.get(call)
            if record.skipped:
                status = STATUS_SKIPPED
            elif record.error:
                status = STATUS_ERROR
            elif score is not None:
                if math.isfinite(score):
                    status = STATUS_SCORED
                elif record.has_text and self.pass_kind != PASS_RETRY:
                    status = STATUS_REPEAT
                else:
                    status = STATUS_EMPTY
            elif record.finished is None:
                status = STATUS_LATE
            elif record.has_text:
                status = STATUS_UNSCORED
            else:
                status = STATUS_EMPTY
            ms: Optional[int] = None
            if (
                status not in (STATUS_LATE, STATUS_SKIPPED)
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


@dataclass(frozen=True)
class ReplyCredit:
    """Which model wrote a pass's reply, and from which pass.

    ``pass_kind`` is PASS_PRIMARY for the turn's first race, PASS_RETRY for
    that race's retry, and PASS_TOOL for any tool follow-up (its race or
    its retry); ``from_retry`` says whether a retry produced it.
    """

    model: str
    score: Optional[float]
    pass_kind: str
    from_retry: bool


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
    # The model whose reply was delivered: the first pass's pick, or the
    # tool follow-up's when one wrote the reply (see set_delivered).
    winner_model: str = ""
    winner_score: Optional[float] = None
    winner_pass: str = ""
    # The first pass's pick (its race, or its retry when that replaced the
    # answer), before any tool follow-up. The runner-up, the tie and the
    # side-by-side alternate all compare against this one.
    first_pass_model: str = ""
    first_pass_score: Optional[float] = None
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
    # Not stored on the row: how the turn stands against
    # fhi_chat_turns_total, which decides whether and how it is written (see
    # the module docstring). The outcome the metric counted, "" until then.
    counted_outcome: str = ""
    # The turn's first race has started, so the models were asked.
    reached_models: bool = False
    # How the primary pass started the outside models (the StagedStart
    # outcomes in utils, or "" when it asked none) and how long it would
    # hold them back (None when it asked none).
    external_start: str = ""
    external_delay_seconds: Optional[float] = None
    # The live Jev check on our first usable reply (chat/reply_gate.py):
    # whether the primary pass held the outside models back for it, its
    # outcome, Jev's four answers, the scorer string, how long it took and
    # which model's reply it judged. Numbers and labels only.
    gate_used: bool = False
    gate_outcome: str = ""
    gate_answers: Optional[float] = None
    gate_verdict: Optional[float] = None
    gate_asks_again: Optional[float] = None
    gate_promises: Optional[float] = None
    gate_scorer: str = ""
    gate_ms: Optional[int] = None
    gate_model: str = ""
    # Whether a failed check demoted the judged reply below the outside
    # models' answers, and whether the pass still delivered it (nothing
    # else usable arrived).
    gate_demoted: bool = False
    gate_demoted_delivered: bool = False

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

    def set_external_start(self, start: str, delay_seconds: Optional[float]) -> None:
        """Record how the primary pass started the outside models."""
        self.external_start = str(start)[:_ENUM_MAX]
        self.external_delay_seconds = _finite_or_none(delay_seconds)

    def set_gate(
        self,
        outcome: str,
        scores: Optional[Sequence[Optional[float]]],
        scorer: str,
        ms: Optional[int],
        model: str,
    ) -> None:
        """Record the check the primary pass held the outside models for.
        ``scores`` is (answers, verdict, asks_again, promises), or None when
        Jev gave no answer."""
        self.gate_used = True
        self.gate_outcome = str(outcome or "")[:_ENUM_MAX]
        answers, verdict, asks_again, promises = (
            tuple(scores) if scores is not None else (None, None, None, None)
        )
        self.gate_answers = _finite_or_none(answers)
        self.gate_verdict = _finite_or_none(verdict)
        self.gate_asks_again = _finite_or_none(asks_again)
        self.gate_promises = _finite_or_none(promises)
        self.gate_scorer = str(scorer or "")[:_SCORER_MAX]
        self.gate_ms = max(0, int(ms)) if isinstance(ms, int) else None
        self.gate_model = str(model or "")[:_MODEL_LABEL_MAX]

    def set_gate_demotion(self, demoted: bool, delivered: bool) -> None:
        """Record whether the failed check demoted the judged reply, and
        whether the primary pass still delivered it."""
        self.gate_demoted = demoted is True
        self.gate_demoted_delivered = self.gate_demoted and delivered is True

    def set_winner(
        self,
        model: Optional[str],
        score: Optional[float],
        from_retry: bool,
        runner_up_model: Optional[str],
        runner_up_score: Optional[float],
        closely_tied: bool,
    ) -> None:
        """The first pass's pick and runner-up. Its pick is also the
        delivered one until a tool follow-up's reply replaces it."""
        self.first_pass_model = (model or "")[:_MODEL_LABEL_MAX]
        self.first_pass_score = _finite_or_none(score)
        self.runner_up_model = (runner_up_model or "")[:_MODEL_LABEL_MAX]
        self.runner_up_score = _finite_or_none(runner_up_score)
        self.closely_tied = bool(closely_tied)
        self.set_delivered(
            ReplyCredit(
                model=model or "",
                score=score,
                pass_kind=PASS_RETRY if from_retry else PASS_PRIMARY,
                from_retry=from_retry,
            )
        )

    def set_delivered(self, credit: ReplyCredit) -> None:
        """The model whose reply the turn delivered. Wins on the dashboard
        go to this one."""
        self.winner_model = (credit.model or "")[:_MODEL_LABEL_MAX]
        self.winner_score = _finite_or_none(credit.score)
        self.winner_pass = credit.pass_kind
        self.retry_used = bool(credit.from_retry)

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
            "first_pass_model": self.first_pass_model,
            "first_pass_score": self.first_pass_score,
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
            "external_start": self.external_start,
            "external_delay_seconds": self.external_delay_seconds,
            "gate_used": self.gate_used,
            "gate_outcome": self.gate_outcome,
            "gate_answers": self.gate_answers,
            "gate_verdict": self.gate_verdict,
            "gate_asks_again": self.gate_asks_again,
            "gate_promises": self.gate_promises,
            "gate_scorer": self.gate_scorer,
            "gate_ms": self.gate_ms,
            "gate_model": self.gate_model,
            "gate_demoted": self.gate_demoted,
            "gate_demoted_delivered": self.gate_demoted_delivered,
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


# How long a turn being cancelled waits for its row, and the statement
# timeout that bounds the write itself on PostgreSQL.
CANCELLED_TURN_WRITE_SECONDS = 2.0


def _bound_statements(connection: Any, ms: int) -> None:
    """On PostgreSQL, end any statement of the current transaction that runs
    past ``ms`` (waiting on a lock included). Nothing elsewhere: sqlite in
    tests and development has no statement timeout."""
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        # set_config(..., true) is SET LOCAL: it ends with the transaction.
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", [str(ms)])


def _record_chat_turn_isolated_sync(chat_id: Any, fields: Dict[str, Any]) -> None:
    """Insert the row on the calling thread's own connection, inside a
    transaction bounded by a statement timeout, then close that thread's
    connections (it is a new thread each time, so nothing else would)."""
    from django.db import connection, connections, transaction

    from fighthealthinsurance.models import ChatTurn

    try:
        with transaction.atomic():
            _bound_statements(connection, int(CANCELLED_TURN_WRITE_SECONDS * 1000))
            ChatTurn.objects.create(chat_id=chat_id, **fields)
    finally:
        connections.close_all()


async def arecord_chat_turn_isolated(
    chat_id: Any,
    turn: Optional[TurnRecord],
    outcome: str,
    timeout: float = CANCELLED_TURN_WRITE_SECONDS,
) -> bool:
    """Write one turn's row while the turn is being cancelled. Returns
    whether it was written in time. Never raises, except that a further
    cancellation of the caller still propagates.

    Unlike every other ORM call on the chat path, this one does not go
    through database_sync_to_async or the native async ORM (CLAUDE.md), and
    on purpose: both run on the connection's single thread-sensitive
    executor, and cancelling an await there does not stop the query. A
    write stuck behind a lock would then hold that executor, delaying any
    later ORM call on it and the executor join the socket's teardown waits
    for. So the write runs on a daemon thread with its own database
    connection, closed when it is done, under a statement timeout on
    PostgreSQL; the caller stops waiting after ``timeout`` seconds and
    leaves the thread to finish or fail on its own.
    """
    if turn is None or outcome not in TURN_OUTCOMES:
        return False
    loop = asyncio.get_running_loop()
    done: "asyncio.Future[bool]" = loop.create_future()

    def settle(written: bool) -> None:
        if not done.done():
            done.set_result(written)

    def write(fields: Dict[str, Any]) -> None:
        written = False
        try:
            _record_chat_turn_isolated_sync(chat_id, fields)
            written = True
        except Exception as e:
            logger.warning(
                f"Could not record chat turn for chat {chat_id}: {type(e).__name__}"
            )
        try:
            loop.call_soon_threadsafe(settle, written)
        except RuntimeError:
            # The event loop closed while the write ran: nobody is waiting.
            pass

    try:
        threading.Thread(
            target=write,
            args=(turn.row_fields(outcome),),
            name="fhi-chat-turn-record",
            daemon=True,
        ).start()
    except Exception as e:
        logger.warning(
            f"Could not record chat turn for chat {chat_id}: {type(e).__name__}"
        )
        return False
    try:
        # Shielded, so giving up on the wait leaves the result alone; the
        # thread's write carries on either way.
        return await asyncio.wait_for(asyncio.shield(done), timeout)
    except asyncio.TimeoutError:
        logger.warning(
            f"Chat turn record for chat {chat_id} still being written after "
            f"{timeout}s; not waiting for it"
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

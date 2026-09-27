"""The live Jev check on our own chat reply, inside the chat fan-out
(ml/chat_gate.py holds our own checks, the questions and the decision rule).

When the check is on for a turn, the primary pass holds the outside models'
calls back while ours answer. The first usable reply of ours is checked,
by our own checks first: a reply they reject (too short or a false
promise, the rule the retry uses) fails the check without being sent to
TypeSafe, so our requirements hold whether or not Jev can be reached. An
empty reply is not judged at all: it is recorded as skipped, and the
outside calls start at once. Any other reply goes to Jev with four
questions about the reply and one about the message (is it a crucial
moment: a deadline, a denial, an appeal's next step, coverage). The
outcome:

* pass: the primary pass never sends the outside calls, and picks among
  our models' answers. The retry, which runs only when our own checks
  reject the reply (empty, too short or a false promise), may still ask
  them;
* borderline: the outside calls start at once, and once the race is over
  a second request (chat_gate.rank_replies) scores every deliverable
  candidate, ours included, and the best is delivered (rank());
* fail (from Jev or from our own checks), error or timeout: the outside
  calls start at once, and the usual scoring picks the winner among
  everything that answers. After a fail from Jev (not an error or a
  timeout), and while FHI_CHAT_JEV_GATE_DEMOTE_FAILED is on, the judged
  reply, and any with the same text, ranks just below the best outside
  answer that could be delivered, so it wins only when no such answer
  arrives; our other replies keep their scores
  (utils.best_two_within_timelimit's ``demote_failed``).

Crucial moments. When Jev reads the message as crucial and the chat still
has a side-by-side left (FHI_CHAT_SIDE_BY_SIDES_PER_CHAT), the turn's
reserved call starts too: the side-by-side model (FHI_CHAT_SIDE_BY_SIDE_MODEL,
Kimi-K3), whatever the tier, so the person can compare it with the reply.
It is asked on no other turn: an error, a timeout or a hold that runs out
never starts it.

The outside calls are held for at most FHI_CHAT_JEV_GATE_MAX_WAIT_SECONDS
(or the routing policy's delay, when that is longer), whether or not a
reply of ours has arrived or its check has finished by then. A check never
holds the reply back: it is bounded by FHI_CHAT_JEV_GATE_TIMEOUT_SECONDS,
anything but a pass starts the outside calls, and the fan-out's own windows
still run from the start of the race.

``ReplyGate`` is one turn's check. ``gate_for_turn`` decides whether a turn
gets one: the FHI_CHAT_JEV_GATE_ENABLED switch and the TypeSafe key, the
person's consent to outside models (Jev reads the text), a typed message
(not a document upload or a stored long paste, whose place in the history is
a marker Jev could not read the question from) and at least one of our own
models selectable. The fan-out adds the last condition: the pass must have
outside calls to hold back and calls of ours to start first.

Nothing here stores or logs text. The texts are held in memory while the
check runs; the turn row gets the four numbers, the scorer string, the
outcome, the time the check took and the judged model's label. Errors are
logged by class name only.

Database work. The check's two database steps, the identifier lookup before
the request and the health note after the turn, run through
chat/isolated_db.py: each on a thread of its own with its own connection,
closed afterwards, inside a transaction with a statement timeout on
PostgreSQL. The texts never reach those threads. See _aredactions for why
this steps outside the usual database_sync_to_async.
"""

import asyncio
import time
from typing import Any, Awaitable, Callable, List, Optional, Sequence, Tuple

from loguru import logger

from fighthealthinsurance.chat import isolated_db
from fighthealthinsurance.chat.redaction import chat_redactions
from fighthealthinsurance.chat.safety_filters import llm_requested_delete_handoff
from fighthealthinsurance.chat.tools.patterns import contains_tool_call
from fighthealthinsurance.ml import chat_gate
from fighthealthinsurance.utils import CheckVerdict

# The judged model's label is stored in a column this wide.
_MODEL_LABEL_MAX = 200


# Thread names for chat/isolated_db.py, which counts the threads still
# running under each.
LOOKUP_THREAD = "fhi-chat-reply-check-lookup"
HEALTH_THREAD = "fhi-chat-reply-check-health"
# At most this many threads of each kind still running per process, counting
# any whose turn already stopped waiting. Past it, the lookup is not started
# and nothing is sent (the outside models start), or the health note is left
# out.
MAX_RUNNING = 8
# How long the turn waits for the health note, and the statement timeout that
# bounds it on PostgreSQL.
HEALTH_NOTE_SECONDS = 1.0


class LookupBusy(Exception):
    """Too many earlier identifier lookups are still running."""


async def _aredactions(chat_id: Any, timeout: float) -> List[Tuple[str, str]]:
    """The chat's identifier list (chat/redaction.py), read on a thread of
    its own. Raises TimeoutError after ``timeout`` seconds, LookupBusy when
    MAX_RUNNING earlier lookups are still running, and whatever the lookup
    raised otherwise.

    Unlike other ORM calls on the chat path, this does not go through
    database_sync_to_async or the native async ORM (CLAUDE.md), on purpose.
    Both run the query on the connection's single thread-sensitive
    executor, the chat socket's own, and giving up on the await after the
    check's timeout does not stop a query that is already running. A lookup
    stuck behind a lock would then hold that executor, and the rest of the
    turn (its row, the persisted reply) and every later turn of the chat
    would queue behind it. So the lookup runs through
    isolated_db.run_isolated: its own thread and connection, a statement
    timeout of the same length on PostgreSQL, and a caller that stops
    waiting after ``timeout`` seconds.
    """
    if isolated_db.running(LOOKUP_THREAD) >= MAX_RUNNING:
        raise LookupBusy()
    found: List[Tuple[str, str]] = await isolated_db.run_isolated(
        chat_redactions, chat_id, timeout=timeout, name=LOOKUP_THREAD
    )
    return found


def _note_health(health: str) -> None:
    """The check's ExternalServiceHealth note, for a thread of its own:
    "" for an answer, a failure summary otherwise. Best effort."""
    from fighthealthinsurance.models import ExternalServiceHealth

    if health == "":
        ExternalServiceHealth.note_success(chat_gate.SERVICE)
    else:
        ExternalServiceHealth.note_failure(chat_gate.SERVICE, health)


def gate_for_turn(
    chat_id: Any,
    *,
    external_allowed: bool,
    typed_message: bool,
    ours_selectable: Callable[[], bool],
) -> "Optional[ReplyGate]":
    """A ReplyGate for this turn, or None when the check is off for it.

    ``external_allowed`` must be the person's consent to outside models for
    this chat. ``typed_message`` is False for a document upload or a stored
    long paste. TypeSafe's chat budget must allow a request: when it is
    spent the turn routes by our own rules, as with the check off.
    ``ours_selectable`` says whether the router has one of our own models it
    would pick (asked last, only when everything else holds): with none,
    holding the outside models back would only delay the answer, the same
    reason the routing policy is set aside then.
    """
    if not (external_allowed and typed_message and chat_gate.enabled()):
        return None
    if not chat_gate.budget_allows():
        return None
    if not ours_selectable():
        return None
    return ReplyGate(
        chat_id,
        max_wait_seconds=chat_gate.max_wait_seconds(),
        timeout_seconds=chat_gate.timeout_seconds(),
        demote_failed=chat_gate.demote_failed(),
    )


class ReplyGate:
    """One turn's check on our first usable reply. One check per turn."""

    def __init__(
        self,
        chat_id: Any,
        *,
        max_wait_seconds: float,
        timeout_seconds: float,
        demote_failed: bool = True,
    ):
        self.chat_id = chat_id
        self.max_wait_seconds = float(max_wait_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self.demote_failed = demote_failed is True
        # Whether a fan-out held its outside calls back for this check.
        self.used = False
        self.outcome = ""
        self.scores: Optional[chat_gate.GateScores] = None
        self.scorer = ""
        self.model = ""
        self.ms: Optional[int] = None
        # Jev's read of the message, once answered.
        self.crucial = False
        # The fan-out's outside calls and its reserved side-by-side calls
        # (bind()): what a verdict names to start.
        self._outside: Tuple[Awaitable[Any], ...] = ()
        self._reserved: Tuple[Awaitable[Any], ...] = ()
        # The chat's identifier list, kept in memory for the ranking so it
        # is looked up once per turn. Dropped when the turn is over.
        self._identifiers: Optional[List[Tuple[str, str]]] = None
        # The ranking (rank()), when one ran.
        self.rank_outcome = ""
        self.rank_ms: Optional[int] = None
        self.rank_count: Optional[int] = None
        self._started: Optional[float] = None
        # For ExternalServiceHealth once the turn is over: "" for an answer,
        # a failure summary for an error or timeout from TypeSafe, None when
        # nothing reached TypeSafe or nothing came back to judge it by.
        self._health: Optional[str] = None

    def bind(
        self, outside: Sequence[Awaitable[Any]], reserved: Sequence[Awaitable[Any]]
    ) -> None:
        """The fan-out's held-back calls: the outside models' and the
        reserved side-by-side ones (empty when the chat has none left)."""
        self._outside = tuple(outside)
        self._reserved = tuple(reserved)

    def _verdict(self) -> CheckVerdict:
        """What the race starts, from the outcome: see the module docstring.
        Anything but an answer from Jev leaves the race's default (every
        outside call, never a reserved one)."""
        side = self._reserved if self.crucial else ()
        if self.outcome == chat_gate.PASS:
            return CheckVerdict(passed=True, start=side)
        if self.outcome == chat_gate.BORDERLINE or (
            self.outcome == chat_gate.FAIL and self.scores is not None
        ):
            return CheckVerdict(passed=False, start=self._outside + side)
        return CheckVerdict(passed=False)

    async def judge(
        self, message: Optional[str], reply: Optional[str], model: Optional[str]
    ) -> CheckVerdict:
        """The fan-out's check (utils.best_two_within_timelimit ``check``):
        passed only for a clear pass, and naming the calls to start. Never
        raises (except cancellation, when the fan-out stops waiting for
        it)."""
        if self._started is not None:
            return CheckVerdict(passed=False)
        self._started = time.monotonic()
        self.model = str(model or "")[:_MODEL_LABEL_MAX]
        try:
            await self._judge(message, reply)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Chat reply check failed: {type(e).__name__}")
            self.outcome = chat_gate.ERROR
        finally:
            self.ms = self._elapsed_ms()
        return self._verdict()

    async def _judge(self, message: Optional[str], reply: Optional[str]) -> None:
        # A reply the person would not see as it is: a tool call is followed
        # by another pass, and the data-deletion handoff by a canned reply.
        if (
            not reply
            or contains_tool_call(reply)
            or "🐼" in reply
            or llm_requested_delete_handoff(reply)
            or not chat_gate.judgeable(reply)
            or not chat_gate.judgeable(message)
        ):
            self.outcome = chat_gate.SKIPPED
            return
        # Our own requirements first, and without sending anything: a reply
        # the retry would reject fails here whether or not Jev can be
        # reached. Nothing reached TypeSafe, so there is no health to note.
        if chat_gate.fails_our_checks(reply):
            self.outcome = chat_gate.FAIL
            self.scorer = chat_gate.LOCAL_SCORER
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        try:
            identifiers = await asyncio.wait_for(
                _aredactions(self.chat_id, self.timeout_seconds),
                timeout=self.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # No identifier list means nothing is sent: the generic patterns
            # alone are not the redaction this promises.
            timed_out = isinstance(e, (TimeoutError, asyncio.TimeoutError))
            logger.warning(f"Chat reply check not sent: {type(e).__name__}")
            self.outcome = chat_gate.TIMEOUT if timed_out else chat_gate.ERROR
            return
        self._identifiers = identifiers
        remaining = deadline - loop.time()
        if remaining <= 0:
            self.outcome = chat_gate.TIMEOUT
            return
        result = await chat_gate.check_reply(
            message, reply, identifiers=identifiers, timeout=remaining
        )
        self.outcome = result.outcome
        if result.answered:
            self.scores = result.scores
            self.scorer = result.scorer
            self.crucial = chat_gate.is_crucial(result.scores)
            self._health = ""
        elif result.outcome in (chat_gate.ERROR, chat_gate.TIMEOUT):
            self._health = result.failure

    def wants_rank(self) -> bool:
        """Whether the turn's candidates go to a ranking: only after a
        borderline check."""
        return self.outcome == chat_gate.BORDERLINE

    async def rank(
        self, message: Optional[str], replies: Sequence[str]
    ) -> chat_gate.RankResult:
        """Jev's scores for each candidate (chat_gate.rank_replies), with
        the identifier list the check looked up. Once per turn; never
        raises, except that a cancellation still propagates."""
        if self.rank_outcome or self._identifiers is None:
            return chat_gate.RankResult(outcome=chat_gate.SKIPPED)
        started = time.monotonic()
        self.rank_count = len(replies)
        try:
            if not chat_gate.budget_allows():
                result = chat_gate.RankResult(outcome=chat_gate.SKIPPED)
            else:
                result = await chat_gate.rank_replies(
                    message,
                    replies,
                    identifiers=self._identifiers,
                    timeout=chat_gate.rank_timeout_seconds(),
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Chat reply ranking failed: {type(e).__name__}")
            result = chat_gate.RankResult(outcome=chat_gate.ERROR)
        self.rank_outcome = result.outcome
        self.rank_ms = max(0, int((time.monotonic() - started) * 1000))
        if result.outcome == chat_gate.RANK_PICKED:
            self._health = ""
        elif result.outcome in (chat_gate.ERROR, chat_gate.TIMEOUT) and result.failure:
            self._health = result.failure
        return result

    def forget(self) -> None:
        """Drop the identifier list: once the ranking is done or skipped,
        and when a turn ends without its health note (cancelled, or failed
        before the models)."""
        self._identifiers = None

    def wants_demotion(self) -> bool:
        """The fan-out's demotion rule (utils.best_two_within_timelimit
        ``demote_failed``): only a fail, never an error, a timeout or a
        reply that was not judged, and only while the setting is on."""
        return self.demote_failed and self.outcome == chat_gate.FAIL

    def _elapsed_ms(self) -> Optional[int]:
        if self._started is None:
            return None
        return max(0, int((time.monotonic() - self._started) * 1000))

    def finish(self) -> None:
        """Settle the outcome once the fan-out is done with the check.

        A check the fan-out cut off (the hold ran out first) is a timeout;
        a gate that held the outside calls but never judged a reply (none of
        ours was usable in time) is skipped.
        """
        if not self.used:
            return
        self._outside = self._reserved = ()
        if self._started is None:
            self.outcome = chat_gate.SKIPPED
            return
        if not self.outcome:
            self.outcome = chat_gate.TIMEOUT
        if self.ms is None:
            self.ms = self._elapsed_ms()

    async def anote_health(self) -> None:
        """Record the check's result on its ExternalServiceHealth row, for
        the status pages. Called once the reply is sent, so it never delays
        a reply, and it waits at most HEALTH_NOTE_SECONDS for the note,
        which runs on a thread of its own for the reason _aredactions gives.
        Never raises, except that a cancellation of the caller still
        propagates."""
        health, self._health = self._health, None
        self._identifiers = None
        if health is None:
            return
        if isolated_db.running(HEALTH_THREAD) >= MAX_RUNNING:
            logger.warning("Chat reply check health not noted: too many running")
            return
        try:
            await isolated_db.run_isolated(
                _note_health,
                health,
                timeout=HEALTH_NOTE_SECONDS,
                name=HEALTH_THREAD,
            )
        except Exception as e:
            logger.warning(f"Chat reply check health not noted: {type(e).__name__}")

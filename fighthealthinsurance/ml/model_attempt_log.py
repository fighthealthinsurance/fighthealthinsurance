"""Collect per-model-call attempt records during appeal generation and
persist them against the denial.

Why this exists: when a generation produces no appeals, the only record of
what the models did was a compact ``models_tried=[name:outcome,...]`` string
in a log line. That says a model returned ``no_output`` but not what it
actually said, how long it took, which backend answered, or which stage of the
primary -> backup -> shed ladder it belonged to. The interesting text -- a
refusal, a truncated draft, a runt too short to deliver -- was discarded.

Why the database and not the log: model responses are derived from the denial
letter and are therefore PHI. Logging them would put patient data into log
aggregation with no access control and no deletion path. Rows written here
hang off ``Denial`` with ``on_delete=CASCADE``, so they are deleted with the
denial (see ``ModelCallAttempt``).

Threading: ``record()`` is called from whichever thread is draining the model
generators -- normally the ``make_appeals`` thread, but a generator finished
after ``make_appeals`` returned is drained from the executor thread behind
``sync_iterator_to_async``. Appends are safe under the GIL; the flush cursor
takes a lock so a sync flush and a later async flush cannot write the same
record twice or skip one.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from loguru import logger

# Cap on stored response text. A misbehaving model can return megabytes, and
# these rows exist for debugging, not archival -- past this length the tail
# tells you nothing the head didn't. `response_chars` records the true length,
# so a truncated row still says how much was produced.
MAX_RESPONSE_CHARS = 24_000

# Cap on stored error text, for the same reason: some backends echo the whole
# request back in an error payload.
MAX_ERROR_CHARS = 4_000

_TRUNCATION_MARKER = "\n...[truncated by ModelAttemptRecorder]"


def _clip(text: Optional[str], limit: int) -> Optional[str]:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_MARKER


def summarize_model_outcomes(outcomes: List[Tuple[Optional[str], str]]) -> str:
    """Compact ``model:outcome[,model:outcome...]`` summary from a list of
    ``(model_name, outcome)`` records, for the models_tried diagnostic.

    Deduped per model (the same model is attempted across the primary/backup/
    shed stages). ``ok`` -- the model produced at least one deliverable appeal
    -- wins over any failure outcome for that model. Capped so a large fan-out
    can't bloat the log line.
    """
    if not outcomes:
        return ""
    best: dict[str, str] = {}
    for name, outcome in outcomes:
        key = str(name) if name is not None else "unknown"
        # "ok" is the only success; keep it over any failure reason.
        if best.get(key) == "ok":
            continue
        if key not in best or outcome == "ok":
            best[key] = outcome
    items = [f"{k}:{v}" for k, v in sorted(best.items())]
    max_items = 12
    if len(items) > max_items:
        hidden = len(items) - max_items
        items = items[:max_items] + [f"+{hidden}_more"]
    return ",".join(items)


@dataclass
class ModelAttemptRecord:
    """One model call's outcome, as collected in memory before persistence."""

    model_name: Optional[str]
    outcome: str
    stage: str = ""
    context_level: Optional[str] = None
    infer_type: str = ""
    backend: str = ""
    error_detail: str = ""
    response_text: Optional[str] = None
    response_chars: Optional[int] = None
    prompt_tokens_est: Optional[int] = None
    duration_ms: Optional[int] = None
    started_at: Optional[Any] = None  # datetime; typed loosely to stay import-light


class ModelAttemptRecorder:
    """Accumulates ``ModelAttemptRecord``s for one generation run and writes
    them out as ``ModelCallAttempt`` rows.

    Flushing happens twice, on purpose:

    * synchronously at the end of ``make_appeals``, which runs on a
      ``database_sync_to_async`` thread that cannot be cancelled. This is the
      flush that matters -- a client hanging up mid-stream closes the async
      generator above it, so an async-only flush would lose exactly the runs
      worth debugging.
    * asynchronously at the end of ``generate_appeals``, to pick up the
      generators that were still lazily chained when ``make_appeals``
      returned. On a successful run most outcomes land here, since the winning
      stage short-circuits drainage of the rest.

    Both are idempotent with respect to each other via the flush cursor.
    """

    def __init__(
        self,
        *,
        denial_id: Optional[int],
        generation_id: Optional[str] = None,
        run_kind: str = "live",
    ) -> None:
        self.denial_id = denial_id
        self.generation_id = generation_id or ""
        self.run_kind = run_kind
        self._records: List[ModelAttemptRecord] = []
        self._flushed = 0
        self._lock = threading.Lock()

    def record(self, record: ModelAttemptRecord) -> None:
        self._records.append(record)

    def outcome_pairs(self) -> List[Tuple[Optional[str], str]]:
        """``(model_name, outcome)`` pairs for the compact ``models_tried``
        log summary. Snapshotted so a concurrent append can't mutate the list
        mid-summary."""
        return [(r.model_name, r.outcome) for r in list(self._records)]

    def models_tried_summary(self) -> str:
        """The ``models_tried`` diagnostic string over everything recorded so
        far. Called twice per run: once by make_appeals right after the peek
        phase (a snapshot -- complete for a zero-appeal run, partial for a
        successful one) and again by the streaming flow once every generator
        has been drained."""
        return summarize_model_outcomes(self.outcome_pairs())

    def _take_pending(self) -> List[ModelAttemptRecord]:
        """Records not yet written, advancing the cursor past them."""
        with self._lock:
            snapshot = list(self._records)
            pending = snapshot[self._flushed :]
            self._flushed = len(snapshot)
            return pending

    def _rows(self, pending: List[ModelAttemptRecord], denial_id: int) -> list:
        from fighthealthinsurance.models import ModelCallAttempt

        return [
            ModelCallAttempt(
                for_denial_id=denial_id,
                generation_id=self.generation_id,
                run_kind=self.run_kind,
                model_name=(r.model_name or "unknown")[:200],
                backend=(r.backend or "")[:300],
                stage=(r.stage or "")[:32],
                context_level=(r.context_level or "")[:32],
                infer_type=(r.infer_type or "")[:64],
                outcome=(r.outcome or "unknown")[:64],
                error_detail=_clip(r.error_detail, MAX_ERROR_CHARS) or "",
                response_text=_clip(r.response_text, MAX_RESPONSE_CHARS),
                response_chars=r.response_chars,
                prompt_tokens_est=r.prompt_tokens_est,
                duration_ms=r.duration_ms,
                started_at=r.started_at,
            )
            for r in pending
        ]

    def flush(self) -> int:
        """Persist pending records synchronously. Returns rows written.

        Call this ONLY from inside a callable already wrapped in
        ``database_sync_to_async`` -- today ``make_appeals`` and the speculative
        helper's ``_generate_drafts``, both of which are bridged by their
        callers. Inside such a callable the sync ORM is the correct API and the
        bridge closes the thread's connections around the call; bridging again
        from in there would just churn connections, and there is nothing to
        await from a sync function anyway. From async code use ``aflush``,
        which uses the native async ORM.

        Not a hard error if that is violated: Django raises
        SynchronousOnlyOperation on the event loop, which the except below
        turns into a WARNING naming this function -- diagnostics must not be
        able to break generation, so a misplaced call loses rows loudly rather
        than failing a user's appeal.

        Best-effort for the same reason: a DB failure is logged and swallowed.
        Records are consumed either way -- retrying them on the next flush
        would risk writing a partial batch twice.
        """
        denial_id = self.denial_id
        if denial_id is None:
            return 0
        pending = self._take_pending()
        if not pending:
            return 0
        try:
            from fighthealthinsurance.models import ModelCallAttempt

            ModelCallAttempt.objects.bulk_create(self._rows(pending, denial_id))
            return len(pending)
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to persist {len(pending)} model attempt record(s) for "
                f"denial {self.denial_id}: {e}"
            )
            return 0

    async def aflush(self) -> int:
        """Async counterpart to ``flush``, for the end of the streaming flow.

        Uses the native async ORM (``abulk_create``) rather than bridging the
        sync one, so there is no thread hop and no bridge to pick.
        """
        denial_id = self.denial_id
        if denial_id is None:
            return 0
        pending = self._take_pending()
        if not pending:
            return 0
        try:
            from fighthealthinsurance.models import ModelCallAttempt

            await ModelCallAttempt.objects.abulk_create(self._rows(pending, denial_id))
            return len(pending)
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Failed to persist {len(pending)} model attempt record(s) for "
                f"denial {self.denial_id}: {e}"
            )
            return 0

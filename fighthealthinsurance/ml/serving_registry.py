"""A record of what each backend serves, and which record a draft came from.

The health sweep (ml/health_status.py) asks every OpenAI-compatible backend
for its ``/models`` list each round; RemoteOpenLike.model_is_ok keeps the
entry for the model it checked (and for a backup leg on the same server).
After the round, ``record_backends_async`` records them on a background
thread: a ServingIdentity row whenever a leg's answer differs from every
earlier one, otherwise the matching row's ``last_seen`` moves.

Drafts find their row with ``aserving_id_for``, which never touches the
database: it reads what this process's sweep recorded, or what a background
loader last read from the table (for processes that don't run the sweep). A
backend whose primary and backup legs report different weights attributes
nothing, since either leg may have written a given draft.

Hosted APIs that publish no such list never get a row, and their drafts
point at nothing: there is nothing to record beyond the model id the draft
already carries.
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import re
import threading
import time
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from loguru import logger

# Bump last_seen at most this often for an unchanged answer, so a sweep
# doesn't write every row. A changed answer always moves it.
LAST_SEEN_EVERY = datetime.timedelta(minutes=10)
# How long this process trusts what its own sweep recorded for a backend.
SWEEP_TRUST_SECONDS = 2 * 60 * 60.0
# How often the background loader re-reads the table, and how far back it
# looks: rows not seen for three sweeps no longer describe a live leg.
LOADER_INTERVAL_SECONDS = 300.0
LOADER_WINDOW = datetime.timedelta(hours=3)
# How long one registry statement may run, waiting on a lock included.
STATEMENT_TIMEOUT_MS = 2000

_IDENTITY_FIELDS = (
    "model_id",
    "endpoint",
    "weights",
    "parent",
    "max_model_len",
    "owned_by",
)

_lock = threading.Lock()
# backend descriptor -> (row id to attribute, or None; monotonic time)
_from_sweep: Dict[str, Tuple[Optional[int], float]] = {}
# backend descriptor -> row id to attribute, or None, from the loader
_loaded: Dict[str, Optional[int]] = {}
# (backend, endpoint, model_id) -> the row this process last recorded for it
_last_recorded: Dict[Tuple[str, str, str], int] = {}
_loader_started = False
_recording = threading.Lock()


def fingerprint(backend: str, card: dict) -> str:
    """One hash per distinct answer from one backend leg."""
    identity = {"backend": backend, **{k: card.get(k) for k in _IDENTITY_FIELDS}}
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, default=str).encode()
    ).hexdigest()


def _normalise(card: dict) -> Dict[str, Any]:
    # Blank, not None, for text the server didn't report, so the same answer
    # always hashes the same way.
    return {
        k: (card.get(k) if k == "max_model_len" else str(card.get(k) or ""))
        for k in _IDENTITY_FIELDS
    }


def _what_it_serves(fields: Dict[str, Any]) -> Tuple[Any, Any, Any]:
    """What two legs must share to be the same model: the weights (or, when
    the server doesn't say, the model id), the base model and the context
    length. The endpoint and the name a leg is served under may differ."""
    return (
        fields.get("weights") or fields.get("model_id"),
        fields.get("parent"),
        fields.get("max_model_len"),
    )


@contextlib.contextmanager
def _bounded_statements() -> Iterator[None]:
    """A transaction whose statements give up after STATEMENT_TIMEOUT_MS on
    PostgreSQL. Inside a caller's transaction (tests), only a savepoint: the
    timeout is SET LOCAL and would outlive this block there."""
    from django.db import connection, transaction

    from fighthealthinsurance.ml.chat_policy import _bound_statements

    bound = not connection.in_atomic_block
    with transaction.atomic():
        if bound:
            _bound_statements(connection, STATEMENT_TIMEOUT_MS)
        yield


def record(backend: str, card: dict) -> Optional[int]:
    """Write or refresh the row for one leg of ``backend``; its id.

    Synchronous and may raise; record_backends calls it and contains errors.
    """
    from django.db import IntegrityError
    from django.utils import timezone

    from fighthealthinsurance.models import ServingIdentity

    fields = _normalise(card)
    fp = fingerprint(backend, fields)
    now = timezone.now()
    leg_key = (backend, fields["endpoint"], fields["model_id"])
    with _bounded_statements():
        try:
            row, created = ServingIdentity.objects.get_or_create(
                fingerprint=fp, defaults={"backend": backend[:300], **fields}
            )
        except IntegrityError:
            # Another pod wrote the same row first.
            row, created = ServingIdentity.objects.get(fingerprint=fp), False
        changed = _last_recorded.get(leg_key) != row.pk
        if created:
            logger.info(
                f"Serving registry: {backend} now serves "
                f"{fields.get('weights') or fields.get('model_id')}"
            )
        elif changed or now - row.last_seen >= LAST_SEEN_EVERY:
            # A leg that went back to an earlier answer must become current
            # again at once, not ten minutes later.
            ServingIdentity.objects.filter(pk=row.pk).update(last_seen=now)
    _last_recorded[leg_key] = row.pk
    return row.pk


def _leg_cards(backend: Any) -> Optional[List[Optional[dict]]]:
    """One card per leg of ``backend`` from this round, or None when its
    health check didn't succeed this round. A second leg on another server
    is fetched here, off the sweep; a failed fetch is None for that leg."""
    first = getattr(backend, "last_model_card", None)
    if not isinstance(first, dict):
        return None
    legs_of = getattr(backend, "serving_legs", None)
    legs = legs_of() if callable(legs_of) else []
    cards: List[Optional[dict]] = [first]
    if len(legs) > 1:
        second = getattr(backend, "last_backup_model_card", None)
        if not isinstance(second, dict):
            from fighthealthinsurance.ml.ml_models import fetch_model_card

            _leg, api_base, model = legs[1]
            second = fetch_model_card(api_base, model, getattr(backend, "token", None))
        cards.append(second)
    return cards


def record_backends(backends: Iterable[Any]) -> None:
    """Record every backend whose health check succeeded this round, and what
    its drafts should point at. Never raises."""
    from django.db import close_old_connections

    from fighthealthinsurance.generate_appeal import backend_label

    close_old_connections()
    try:
        for backend in backends:
            try:
                cards = _leg_cards(backend)
                if cards is None:
                    continue
                label = backend_label(backend)
                row_ids = [record(label, c) if c else None for c in cards]
                agree = all(c is not None for c in cards) and (
                    len({_what_it_serves(_normalise(c)) for c in cards if c}) == 1
                )
                attribute = row_ids[0] if agree else None
                with _lock:
                    _from_sweep[label] = (attribute, time.monotonic())
            except Exception as e:
                logger.warning(f"Serving registry: could not record {backend}: {e}")
    finally:
        close_old_connections()


def _background_allowed() -> bool:
    from django.conf import settings

    return bool(getattr(settings, "ML_HEALTH_BACKGROUND_SWEEP", True))


def record_backends_async(backends: Iterable[Any]) -> None:
    """Record on a daemon thread and return at once, so registry trouble can
    never delay the sweep or its next round. A round still recording when
    the next one ends is left to finish; the new one is skipped."""
    if not _background_allowed():
        return
    candidates = list(backends)
    if not _recording.acquire(blocking=False):
        logger.info("Serving registry: previous round still recording; skipping")
        return

    def run() -> None:
        try:
            record_backends(candidates)
        finally:
            _recording.release()

    try:
        threading.Thread(target=run, name="serving-registry", daemon=True).start()
    except Exception as e:
        _recording.release()
        logger.warning(f"Serving registry: could not start recording: {e}")


_LEG_RE = re.compile(r"\((.+?) @ ([^)]+)\)")


def load_current_rows() -> Dict[str, Optional[int]]:
    """From the table: for each backend seen in the last LOADER_WINDOW, the
    row its drafts should point at. The newest row per leg; when the legs
    disagree on what they serve, None. Synchronous; may raise."""
    from django.utils import timezone

    from fighthealthinsurance.models import ServingIdentity

    since = timezone.now() - LOADER_WINDOW
    with _bounded_statements():
        rows = list(
            ServingIdentity.objects.filter(last_seen__gte=since)
            .order_by("-last_seen", "-id")
            .values(
                "id",
                "backend",
                "endpoint",
                "model_id",
                "weights",
                "parent",
                "max_model_len",
            )
        )
    legs: Dict[str, Dict[Tuple[str, str], Dict[str, Any]]] = {}
    for row in rows:
        per_backend = legs.setdefault(row["backend"], {})
        per_backend.setdefault((row["endpoint"], row["model_id"]), dict(row))
    current: Dict[str, Optional[int]] = {}
    for backend, per_leg in legs.items():
        served = {_what_it_serves(r) for r in per_leg.values()}
        if len(served) != 1:
            current[backend] = None
            continue
        primary = _LEG_RE.search(backend)
        first = None
        if primary:
            model, endpoint = primary.group(1), primary.group(2)
            first = per_leg.get((endpoint, model))
        current[backend] = (first or next(iter(per_leg.values())))["id"]
    return current


def _loader_loop() -> None:
    from django.db import close_old_connections

    while True:
        close_old_connections()
        try:
            loaded = load_current_rows()
            with _lock:
                _loaded.clear()
                _loaded.update(loaded)
        except Exception as e:
            logger.warning(f"Serving registry: could not load current rows: {e}")
        finally:
            close_old_connections()
        time.sleep(LOADER_INTERVAL_SECONDS)


def _ensure_loader() -> None:
    global _loader_started
    if _loader_started or not _background_allowed():
        return
    with _lock:
        if _loader_started:
            return
        _loader_started = True
    try:
        threading.Thread(
            target=_loader_loop, name="serving-registry-loader", daemon=True
        ).start()
    except Exception as e:
        logger.warning(f"Serving registry: could not start the loader: {e}")


async def aserving_id_for(backend: Optional[str]) -> Optional[int]:
    """The ServingIdentity row a draft from ``backend`` points at, or None.

    Memory only, never the database: what this process's sweep recorded in
    the last SWEEP_TRUST_SECONDS, otherwise what the background loader last
    read. Before either has anything, None. Async so it sits naturally on
    the save path; it never awaits anything.
    """
    if not backend:
        return None
    _ensure_loader()
    with _lock:
        swept = _from_sweep.get(backend)
        if swept is not None and time.monotonic() - swept[1] < SWEEP_TRUST_SECONDS:
            return swept[0]
        return _loaded.get(backend)


def reset_serving_registry_cache() -> None:
    """Forget everything this process remembers (tests)."""
    with _lock:
        _from_sweep.clear()
        _loaded.clear()
        _last_recorded.clear()

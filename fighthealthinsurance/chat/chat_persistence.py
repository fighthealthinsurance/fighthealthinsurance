"""Transactional persistence helpers for OngoingChat.

Multiple writers touch the same OngoingChat row concurrently: the WebSocket
turn handler, background summary generation, the microsite-context fetch, and
denied-items analysis. A read-modify-``asave()`` against a stale in-memory
object lets the last writer win and silently drops the other writers' updates
(lost user/assistant messages after reconnects, clobbered summaries).

``apersist_chat_turn`` is the shared write path: it re-reads the row under
``select_for_update`` inside a transaction, merges this turn's additions
against the FRESH state, and saves only the fields it changed. On Postgres the
row lock fully serializes writers; on sqlite (tests) ``select_for_update`` is
a no-op but sqlite serializes writes anyway and the merge logic still runs.
"""

import uuid
from typing import Any, Dict, List, Optional

from channels.db import database_sync_to_async
from django.db import transaction
from django.utils import timezone

from loguru import logger


def merge_new_messages(
    history: List[Dict[str, Any]], new_messages: List[Dict[str, str]]
) -> List[Dict[str, Any]]:
    """Append messages to a history list with tail-dedupe and user-merge.

    Mirrors the long-standing turn semantics, but applied against the fresh
    DB tail instead of a possibly-stale in-memory copy:
    - an exact duplicate of the current tail (same role and content, e.g. a
      client retrying the same send after a reconnect) is dropped;
    - two consecutive user messages with no assistant reply between them are
      merged into one message.
    """
    for msg in new_messages:
        role = msg.get("role")
        content = msg.get("content")
        if content is None:
            continue
        if history:
            last = history[-1]
            if last.get("role") == role and last.get("content") == content:
                continue
            if role == "user" and last.get("role") == "user":
                last["content"] = f"{last.get('content')} {content}"
                last["timestamp"] = timezone.now().isoformat()
                continue
        history.append(
            {
                "role": role,
                "content": content,
                "timestamp": timezone.now().isoformat(),
            }
        )
    return history


def _persist_chat_turn_sync(
    chat_id: uuid.UUID,
    new_messages: List[Dict[str, str]],
    new_summaries: List[Optional[str]],
):
    from fighthealthinsurance.models import OngoingChat

    with transaction.atomic():
        fresh = OngoingChat.objects.select_for_update().get(id=chat_id)
        update_fields = ["updated_at"]
        if new_messages:
            history = fresh.chat_history or []
            fresh.chat_history = merge_new_messages(history, new_messages)
            update_fields.append("chat_history")
        if new_summaries:
            summary = fresh.summary_for_next_call or []
            for entry in new_summaries:
                # Same rule as should_store_summary, applied to fresh state:
                # skip empty entries and exact repeats of the current tail.
                if not entry or (summary and summary[-1] == entry):
                    continue
                summary.append(entry)
            fresh.summary_for_next_call = summary
            update_fields.append("summary_for_next_call")
        fresh.save(update_fields=update_fields)
        return fresh


async def apersist_chat_turn(
    chat,
    *,
    new_messages: Optional[List[Dict[str, str]]] = None,
    new_summaries: Optional[List[Optional[str]]] = None,
):
    """Persist a turn's additions against the fresh row; return the fresh row.

    Callers should rebind their in-memory chat object to the returned row so
    later turns and replays see the merged state, not the pre-merge copy.
    """
    fresh = await database_sync_to_async(_persist_chat_turn_sync)(
        chat.id, new_messages or [], new_summaries or []
    )
    logger.debug(f"Persisted chat turn for chat {fresh.id}")
    return fresh

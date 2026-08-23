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

import re
import uuid
from typing import Any, Dict, List, Optional

from channels.db import database_sync_to_async
from django.db import transaction
from django.utils import timezone

from loguru import logger

# Legacy shape of the LLM-context-only link notes, from before they carried an
# explicit `internal` flag. Deliberately anchored to the FULL generated shape
# ("... Appeal #12", "... Prior Auth Request #3f2a...") rather than the bare
# prefix: the prefix alone is text a user can type, and matching it hid their
# own message from their own replay while every server-side view still had it.
# The id part must accept UUIDs, not just integers: Appeal pks are ints but
# PriorAuthRequest pks are UUIDs, and `#\d+` silently missed every legacy
# prior-auth note whose UUID starts with a hex letter -- rendering the
# internal note as a user bubble and letting previews/titles pick it up.
_LEGACY_INTERNAL_NOTE_RE = re.compile(
    r"^(?:Linked this chat to|This chat is already linked to)\s+"
    r"(?:Appeal|Prior Auth Request)\s+#[0-9a-fA-F-]+",
)


def is_internal_history_message(message: Dict[str, Any]) -> bool:
    """Whether a stored history entry is LLM-context-only.

    Internal notes are persisted with role=user so the model sees them, but
    they were never typed by the user and must not be shown as user messages
    -- in replay OR in any other view of the history (REST listings, chat
    titles, previews).
    """
    if message.get("internal"):
        return True
    if message.get("role") != "user":
        return False
    content = message.get("content") or ""
    return bool(_LEGACY_INTERNAL_NOTE_RE.match(content))


def iter_visible_history(
    history: Optional[List[Dict[str, Any]]],
):
    """Lazily yield history entries that are not LLM-context-only.

    Prefer this in consumers that stop at the first match (chat previews,
    titles): ``visible_history`` materializes a full filtered copy, which is
    O(history) per chat just to find one message.
    """
    for m in history or []:
        if not is_internal_history_message(m):
            yield m


def visible_history(
    history: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """History with LLM-context-only messages removed."""
    return list(iter_visible_history(history))


def merge_new_messages(
    history: List[Dict[str, Any]], new_messages: List[Dict[str, Any]]
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
            if (
                role == "user"
                and last.get("role") == "user"
                # Only merge messages of the same internal-ness. This branch
                # returns before the internal flag is applied below, so
                # merging an internal LLM-context note into a user-authored
                # message silently dropped the flag -- and the merged text no
                # longer started with a known internal prefix either, so
                # replay rendered the system note inside the user's own
                # bubble. A failed turn leaves a user-role tail, which is
                # exactly when the next internal note would hit this path.
                and bool(last.get("internal")) == bool(msg.get("internal"))
            ):
                last["content"] = f"{last.get('content')} {content}"
                last["timestamp"] = timezone.now().isoformat()
                continue
        entry: Dict[str, Any] = {
            "role": role,
            "content": content,
            "timestamp": timezone.now().isoformat(),
        }
        # LLM-context-only messages (e.g. appeal-link notes stored with
        # role=user) carry an internal flag so history replay can skip them.
        if msg.get("internal"):
            entry["internal"] = True
        history.append(entry)
    return history


# Upper bound on stored context summaries. Only the last two are ever read
# (chat_interface builds the LLM context from them), but every turn appends
# one -- long chats used to accrete hundreds of KB-sized entries onto the
# row, bloating each read/write of the chat. Placeholders trimmed away
# before their background summary lands are fine: the replacement helper
# no-ops when the tag is gone.
MAX_STORED_SUMMARIES = 20


def _persist_chat_turn_sync(
    chat_id: uuid.UUID,
    new_messages: List[Dict[str, Any]],
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
            if len(summary) > MAX_STORED_SUMMARIES:
                summary = summary[-MAX_STORED_SUMMARIES:]
            fresh.summary_for_next_call = summary
            update_fields.append("summary_for_next_call")
        fresh.save(update_fields=update_fields)
        return fresh


async def apersist_chat_turn(
    chat,
    *,
    new_messages: Optional[List[Dict[str, Any]]] = None,
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


MICROSITE_MARKER = "Microsite context:"


def _persist_microsite_context_sync(chat_id: uuid.UUID, microsite_entry: str):
    # Lazy import: context_manager will grow a dependency on this module.
    from fighthealthinsurance.chat.context_manager import MISSING_CONTEXT_PREFIX
    from fighthealthinsurance.models import OngoingChat

    with transaction.atomic():
        fresh = OngoingChat.objects.select_for_update().get(id=chat_id)
        summary = fresh.summary_for_next_call or []
        if any(MICROSITE_MARKER in str(e or "") for e in summary):
            # Already stored (a concurrent fetch beat us) -- idempotent.
            return fresh
        last_entry = str(summary[-1]) if summary else ""
        if not summary or MISSING_CONTEXT_PREFIX in last_entry:
            # Never append to a placeholder entry: that would corrupt the tag
            # background_generate_summary matches on. Own entry instead.
            summary.append(microsite_entry)
        else:
            summary[-1] = (
                f"{last_entry}\n\n{microsite_entry}" if last_entry else microsite_entry
            )
        fresh.summary_for_next_call = summary
        fresh.save(update_fields=["summary_for_next_call", "updated_at"])
        return fresh


async def apersist_microsite_context(chat, microsite_entry: str):
    """Store fetched microsite context on the chat without clobbering other
    writers (the background fetch races the main turn's persist)."""
    fresh = await database_sync_to_async(_persist_microsite_context_sync)(
        chat.id, microsite_entry
    )
    logger.debug(f"Persisted microsite context for chat {fresh.id}")
    return fresh


def _replace_summary_placeholder_sync(
    chat_id: uuid.UUID, placeholder_tag: str, summary_text: str
) -> bool:
    from fighthealthinsurance.models import OngoingChat

    with transaction.atomic():
        try:
            fresh = OngoingChat.objects.select_for_update().get(id=chat_id)
        except OngoingChat.DoesNotExist:
            return False
        entries = fresh.summary_for_next_call or []
        replaced = False
        for i, entry in enumerate(entries):
            if str(entry) == placeholder_tag:
                entries[i] = summary_text
                replaced = True
                break
        if not replaced:
            return False
        fresh.summary_for_next_call = entries
        fresh.save(update_fields=["summary_for_next_call", "updated_at"])
        return True


async def areplace_summary_placeholder(
    chat_id: uuid.UUID, placeholder_tag: str, summary_text: str
) -> bool:
    """Swap a background-generated summary in for its placeholder, against
    fresh row state. Returns False when the placeholder is gone (superseded
    by a newer real summary) or the chat no longer exists."""
    replaced: bool = await database_sync_to_async(_replace_summary_placeholder_sync)(
        chat_id, placeholder_tag, summary_text
    )
    return replaced

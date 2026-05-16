"""Helpers for growing ``Denial.qa_context`` and ``Denial.plan_context``.

Three call sites historically wrote ``denial.qa_context`` directly with
slightly different schemas, and one of them (``QAResponseViewSet``) rebuilt
the field from ``DenialQA`` rows alone — clobbering keys like
``"medical_context"`` that the appeal generator had set.  These helpers make
"merge, never overwrite" the only available operation.

The helpers mutate the in-memory ``denial`` instance but never call ``save``
/ ``aupdate`` themselves — the caller picks the persistence mechanism that
matches its context (sync ``.save``, async ``.asave``, atomic
``.aupdate``).
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping, Optional

from loguru import logger

_DROPPED_VALUES = {"", "unknown", "UNKNOWN", "none", "None", None}


def load_qa(denial: Any) -> dict[str, str]:
    """Decode ``denial.qa_context`` into a dict.

    Falls back to ``{"misc": <raw>}`` on JSONDecodeError to preserve any
    pre-existing free-text content (matches the historical behavior at
    ``views.GenerateAppeal.post``).
    """
    raw = getattr(denial, "qa_context", None)
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"misc": str(raw)}
    if not isinstance(decoded, dict):
        return {"misc": str(raw)}
    return {str(k): str(v) for k, v in decoded.items() if v is not None}


def merge_qa(
    denial: Any,
    updates: Mapping[str, Any],
    *,
    source: str,
) -> dict[str, str]:
    """Merge ``updates`` into the denial's ``qa_context`` JSON.

    - Existing keys are preserved unless the update has a truthy, non-junk
      replacement value.
    - Empty / ``UNKNOWN`` / ``None`` updates are dropped so a sparse form
      submission cannot blank out previously captured answers.
    - The denial instance's ``qa_context`` attribute is assigned the new
      JSON string but no ``save`` is issued.

    ``source`` is logged so the merge history is traceable when fields
    later look surprising.
    """
    existing = load_qa(denial)
    changed_keys: list[str] = []
    for key, value in updates.items():
        if key is None or key == "":
            continue
        if value in _DROPPED_VALUES:
            continue
        text = str(value).strip()
        if not text or text in _DROPPED_VALUES:
            continue
        if existing.get(key) == text:
            continue
        existing[key] = text
        changed_keys.append(key)
    if changed_keys:
        denial.qa_context = json.dumps(existing)
        logger.debug(
            f"qa_context merged from {source}: keys={changed_keys} "
            f"denial_id={getattr(denial, 'denial_id', '?')}"
        )
    return existing


def merge_plan_context(
    denial: Any,
    fragments: Iterable[str],
) -> Optional[str]:
    """Append unique plan-context fragments to ``denial.plan_context``.

    Old code gated assignment on ``denial.plan_context is None`` — meaning
    any later plan info (e.g. from a re-submission or chat) was silently
    dropped.  Here we keep all distinct fragments and join them with a
    blank separator so the prompt-side ``str(denial.plan_context)`` still
    works.
    """
    cleaned = [str(f).strip() for f in fragments if f and str(f).strip()]
    if not cleaned:
        return getattr(denial, "plan_context", None)

    existing_raw = getattr(denial, "plan_context", None) or ""
    existing_fragments = [
        chunk.strip() for chunk in existing_raw.split("\n\n") if chunk.strip()
    ]
    # Preserve insertion order; dict.fromkeys deduplicates without re-sorting.
    merged_keys = list(dict.fromkeys(existing_fragments + cleaned))
    new_value = "\n\n".join(merged_keys)
    if new_value != existing_raw:
        denial.plan_context = new_value
        logger.debug(
            f"plan_context grew by {len(merged_keys) - len(existing_fragments)} "
            f"fragment(s) denial_id={getattr(denial, 'denial_id', '?')}"
        )
    return new_value

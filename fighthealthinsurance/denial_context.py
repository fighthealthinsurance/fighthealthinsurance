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

import hashlib
import json
from typing import Any, Iterable, Mapping, Optional, Sequence

from loguru import logger

# Match the historical filter at views.GenerateAppeal.post: only the
# empty string and the form-sentinel "UNKNOWN" mean "user hasn't
# answered."  Lowercase "unknown", "none", and "None" are legitimate
# substantive answers to questions like "Other treatments tried (if
# any)?" or "Comorbidities (if any)?" and must not be dropped.
_DROPPED_VALUES = {"", "UNKNOWN", None}


# Form-field prefix for the questions a model generated for one denial.
GENERATED_QUESTION_PREFIX = "appeal_generated_question_"

# ``qa_context`` keys the review step owns.  A generated question is stored
# under its own text, so a model that asks "date of service" would collide
# with the review step's date in both directions; these keys are therefore
# never read as, nor written from, an answer to a generated question.
#
# Not a "nothing on the page may write these" list: ``in_network`` is also a
# checkbox every patient sees, and ``InsuranceQuestions.__init__`` removes
# that field exactly when the review step owns it instead.
RESERVED_QA_KEYS = frozenset(
    {"denial date", "date of service", "date_of_service", "in_network"}
)


def _question_text(row: Any) -> Optional[str]:
    """The question out of one ``generated_questions`` row.

    Rows are written as ``(question, suggested_answer)`` tuples and come
    back from the JSONField as two-element lists, so index rather than
    unpack.  Anything else is model output we cannot read, and is skipped.
    """
    if isinstance(row, str):
        question = row
    elif isinstance(row, (list, tuple)) and row:
        question = row[0]
    else:
        return None
    if not isinstance(question, str):
        return None
    question = question.strip()
    return question or None


def _question_default(row: Any) -> str:
    """The suggested answer out of one ``generated_questions`` row, if any."""
    if isinstance(row, (list, tuple)) and len(row) > 1 and isinstance(row[1], str):
        return row[1]
    return ""


def question_field_name(question: str) -> str:
    """The form-field name for one generated question.

    Derived from the question text, so the identity survives the list being
    reordered between the page being rendered and the answers being
    submitted.
    """
    digest = hashlib.sha256(question.strip().encode("utf-8")).hexdigest()[:16]
    return f"{GENERATED_QUESTION_PREFIX}{digest}"


def generated_question_fields(
    generated_questions: Optional[Sequence[Any]],
) -> dict[str, tuple[str, str]]:
    """Map field name -> (question text, suggested answer).

    Insertion order is the stored order.  Two rows carrying the same
    question collapse onto one field, so the same sentence is asked once.
    """
    fields: dict[str, tuple[str, str]] = {}
    for row in generated_questions or []:
        question = _question_text(row)
        if question is None:
            continue
        fields.setdefault(
            question_field_name(question), (question, _question_default(row))
        )
    return fields


def question_text_for_field(
    field_name: str, generated_questions: Optional[Sequence[Any]]
) -> Optional[str]:
    """The question text a posted generated-question field belongs to.

    Returns None when the field cannot be resolved, which is the caller's
    signal to keep the answer under its raw posted name rather than file it
    against the wrong question.

    Reads both shapes: the current content-derived name, and the 1-based
    positional name that pages rendered before this change still post.
    """
    if not field_name.startswith(GENERATED_QUESTION_PREFIX):
        return None
    fields = generated_question_fields(generated_questions)
    if field_name in fields:
        return fields[field_name][0]
    suffix = field_name[len(GENERATED_QUESTION_PREFIX) :]
    if suffix.isdigit():
        rows = list(generated_questions or [])
        index = int(suffix)
        # 1-based; reject 0/negative so a malformed key cannot alias
        # Python's negative indexing onto the wrong question.
        if 1 <= index <= len(rows):
            return _question_text(rows[index - 1])
    return None


def qa_key_for_question(question: str) -> str:
    """The ``qa_context`` key an answer to one generated question is filed under.

    The question text, because the appeal prompt (``generate_appeal``) and
    the regulator letter read ``qa_context`` as prose and an identifier
    there would read as noise.  The exception is a question whose text is a
    key the review step owns: that one is filed under its field name
    instead, so answering it cannot overwrite the review step's value.
    """
    if question in RESERVED_QA_KEYS:
        return question_field_name(question)
    return question


def stored_answer_for_question(
    question: str, existing_answers: Mapping[str, str]
) -> Optional[str]:
    """The answer already stored for one generated question, if any.

    Answers are filed by ``qa_key_for_question``, not by field name, so the
    lookup has to go through it.  A reserved key is read only under the
    field name; the review step's own value under that key is never shown
    back as the person's answer.  An answer that a failed mapping left
    filed under the raw field name is still read back.
    """
    value = existing_answers.get(qa_key_for_question(question))
    if value is None:
        value = existing_answers.get(question_field_name(question))
    return value


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
    withdraw: Iterable[str] = (),
) -> dict[str, str]:
    """Merge ``updates`` into the denial's ``qa_context`` JSON.

    - Existing keys are preserved unless the update has a truthy, non-junk
      replacement value.
    - Empty / ``UNKNOWN`` / ``None`` updates are dropped so a sparse form
      submission cannot blank out previously captured answers.
    - ``withdraw`` names keys to remove. Only a caller that knows a blank
      was a decision (a page that posts every field it rendered, or a
      value derived from the current answers) should send one.
    - The denial instance's ``qa_context`` attribute is assigned the new
      JSON string but no ``save`` is issued.

    ``source`` is logged so the merge history is traceable when fields
    later look surprising.
    """
    existing = load_qa(denial)
    changed_keys: list[str] = []
    for key in withdraw:
        if key in existing:
            del existing[key]
            changed_keys.append(key)
    for key, value in updates.items():
        if key is None or key == "":
            continue
        # Handle None explicitly rather than `value in _DROPPED_VALUES` so an
        # unhashable update value (list/dict from a form/serializer) can't
        # raise TypeError. The stringified-text check below covers the
        # "" / "UNKNOWN" sentinels.
        if value is None:
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

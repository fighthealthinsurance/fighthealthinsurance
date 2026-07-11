"""Canonical reporting identity for ML models.

Everything that *persists* or *aggregates* a model name (ProposedAppeal rows,
ChooserCandidate rows, the model-usage staff dashboard, backfills) must derive
it through this module rather than ``str(model)`` / ``repr(model)`` so the
stored identity is:

* stable across processes and restarts (never a ``<... object at 0x...>``
  repr, which embeds a per-process memory address),
* stable across different Python instances of the same configured model,
* human readable, and
* specific enough to keep genuinely different configured models apart
  (two ``DeepInfra`` instances serving different underlying models must not
  collapse just because they share a class).

Identity preference order (see ``canonical_model_name``):

1. The router-stamped friendly name (``ModelDescription.name`` — the
   ``MLRouter.models_by_name`` registry key). This is also exactly what the
   appeal-generation pipeline records on ``ProposedAppeal.model_name``, so
   using it first keeps every reporting source keyed the same way. Multiple
   backend instances registered under one name intentionally aggregate.
2. The explicit configured provider model identifier (``model.model``, e.g.
   ``"google/gemma-4-26B-A4B-it"``) for instances the router has not stamped.
3. The class name, as a documented last resort. It cannot distinguish two
   differently-configured instances of the same class, but it is stable and
   address-free; it should only ever be hit for hand-constructed instances
   outside the router registry.

The special ``synthesized`` bucket (drafts combined from multiple model
outputs) is a reserved pseudo-model name and passes through normalization
unchanged.
"""

import re
from typing import Any, Optional

# Reserved pseudo-model name for outputs synthesized from multiple drafts.
SYNTHESIZED_MODEL_NAME = "synthesized"

# Reporting label stamped by the backfill onto historical *chosen*
# ProposedAppeal rows whose generating model cannot be recovered from any
# related record (they predate model tracking, or no draft matches). Kept
# distinct from live "(unattributed)" picks so legacy gaps stay visible
# instead of being blamed on a guessed model.
LEGACY_UNATTRIBUTED_LABEL = "legacy-unattributed"

_LEGACY_UNRESOLVED_PREFIX = "legacy-unresolved"

# Default CPython object repr, e.g.
# "<fighthealthinsurance.ml.ml_models.DeepInfra object at 0x7f81456da840>".
# Historical ChooserCandidate.model_name values were written through
# str(model) before RemoteModelLike defined __str__, so this exact shape
# reached the database.
OBJECT_REPR_RE = re.compile(
    r"^<(?P<path>[A-Za-z_][A-Za-z0-9_\.]*) object at 0x[0-9a-fA-F]+>$"
)

# Persisted model-name columns are CharField(max_length=200).
MODEL_NAME_MAX_LENGTH = 200


def is_object_repr(value: Optional[str]) -> bool:
    """Return True when ``value`` is a default Python object repr."""
    if not value:
        return False
    return OBJECT_REPR_RE.match(value.strip()) is not None


def legacy_unresolved_label(class_name: str) -> str:
    """Reporting label for a legacy row where only the implementation class
    survived (via a stored object repr) and no related data can recover the
    configured model. Aggregates at class granularity, without the address."""
    return f"{_LEGACY_UNRESOLVED_PREFIX} ({class_name})"


def canonical_model_name(model: Any) -> Optional[str]:
    """Derive the stable reporting identity for an ML model instance.

    Never returns a default object repr; returns None only for ``model is
    None`` so NOT NULL columns are always safe to fill from a real instance.
    """
    if model is None:
        return None
    name = getattr(model, "name", None)
    if isinstance(name, str) and name.strip() and not is_object_repr(name):
        return name.strip()[:MODEL_NAME_MAX_LENGTH]
    configured = getattr(model, "model", None)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()[:MODEL_NAME_MAX_LENGTH]
    return type(model).__name__[:MODEL_NAME_MAX_LENGTH]


def normalize_model_label(raw: Optional[str]) -> Optional[str]:
    """Normalize a *stored* model-name string for reporting.

    * ``None`` / empty / whitespace → ``None`` (caller decides the bucket).
    * Default object reprs → ``legacy-unresolved (ClassName)``: the class is
      the only stable information in the repr; the memory address is dropped
      so equivalent instances aggregate into one row.
    * Anything else (including ``synthesized`` and the legacy labels) passes
      through stripped and unchanged.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    match = OBJECT_REPR_RE.match(text)
    if match:
        class_name = match.group("path").rsplit(".", 1)[-1]
        return legacy_unresolved_label(class_name)
    return text[:MODEL_NAME_MAX_LENGTH]

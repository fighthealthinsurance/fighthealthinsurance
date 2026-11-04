"""Sort denied procedures into broad categories with an LLM, for staff charts.

The staff procedures-denied chart groups by the procedure string people
typed, which spreads one kind of care over dozens of spellings and brand
names. This module adds a coarser view: each distinct (normalized) procedure
gets a broad category such as "Imaging" or "Mental health", stored in
``ProcedureCategory`` so the chart is a read of that table and never calls a
model.

Categorizing runs in two passes, the first feeding the second:

1. **Discover.** The most-denied procedures go to the model, which proposes
   a short list of broad categories. Categories already stored are fed back
   in and kept as given, so re-running the backfill extends the taxonomy
   rather than renaming it, and old and new rows stay comparable.
2. **Assign.** The procedures that still need one are sent in batches with
   that fixed list, and the model picks one category per procedure. An
   answer outside the list becomes ``OTHER``; a procedure the model skipped
   stays uncategorized and is retried on the next run.

Only our own general-purpose internal models are asked
(``infer_with_fallback``'s default pool): procedure strings are free text
people typed about their own care, so they are not sent to a hosted model.
Run it with ``manage.py categorize_procedures``.
"""

import json
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence

from django.db.models import Q
from loguru import logger

from fighthealthinsurance.constants import TEST_ACCOUNT_EMAILS
from fighthealthinsurance.ml import llm_usage
from fighthealthinsurance.ml.ml_inference import infer_with_fallback
from fighthealthinsurance.models import Denial, ProcedureCategory

# The catch-all the model may use, and what an off-list answer becomes.
OTHER = "Other"
# What the chart shows for a procedure no run has categorized yet.
UNCATEGORIZED = "Uncategorized"
# How many of the most-denied procedures pass 1 sees.
DISCOVERY_SAMPLE = 200
# How many procedures one pass-2 prompt sorts.
ASSIGN_BATCH_SIZE = 40
# The most categories pass 1 may add in one run.
MAX_NEW_CATEGORIES = 15
CATEGORY_MAX_LENGTH = 80

_DISCOVER_SYSTEM = (
    "You organize medical procedures, treatments, drugs and services that "
    "health insurers denied into a small set of broad, non-overlapping "
    "categories for a chart. Answer with JSON only."
)
_ASSIGN_SYSTEM = (
    "You sort medical procedures, treatments, drugs and services into a "
    "fixed list of categories. Use only the category names you are given, "
    "spelled exactly as given. Answer with JSON only."
)


def normalize_procedure(procedure: Optional[str]) -> str:
    """Lower-case and collapse whitespace: the chart's grouping key."""
    return " ".join((procedure or "").lower().split())


def _denied_procedures_queryset():
    """Procedure strings of every denial the staff chart counts."""
    excluded = [Denial.get_hashed_email(e) for e in TEST_ACCOUNT_EMAILS]
    return (
        Denial.objects.exclude(hashed_email__in=excluded)
        .exclude(Q(procedure__isnull=True) | Q(procedure=""))
        .values_list("procedure", flat=True)
    )


def count_procedures(procedures: Iterable[Optional[str]]) -> "Counter[str]":
    counts: "Counter[str]" = Counter()
    for procedure in procedures:
        normalized = normalize_procedure(procedure)
        if normalized:
            counts[normalized] += 1
    return counts


def denied_procedure_counts() -> "Counter[str]":
    """Denials per normalized procedure, test accounts left out."""
    return count_procedures(_denied_procedures_queryset())


async def adenied_procedure_counts() -> "Counter[str]":
    return count_procedures([p async for p in _denied_procedures_queryset()])


def category_counts(
    procedure_counts: "Counter[str]", categories: Dict[str, str]
) -> "Counter[str]":
    """Roll procedure counts up into category counts; a procedure with no
    stored category counts as ``UNCATEGORIZED``."""
    totals: "Counter[str]" = Counter()
    for procedure, count in procedure_counts.items():
        totals[categories.get(procedure, UNCATEGORIZED)] += count
    return totals


def stored_categories() -> Dict[str, str]:
    return dict(ProcedureCategory.objects.values_list("procedure", "category"))


def clean_category(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.split()).strip(" .,;:-\"'")[:CATEGORY_MAX_LENGTH]


def _parse_json(text: Optional[str]) -> Any:
    """The first JSON array or object in a model reply (models like to wrap
    it in a code fence or a sentence), or None."""
    if not text:
        return None
    match = re.search(r"[\[{].*[\]}]", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except ValueError:
        return None


def parse_category_list(text: Optional[str]) -> List[str]:
    parsed = _parse_json(text)
    if isinstance(parsed, dict):
        parsed = parsed.get("categories")
    if not isinstance(parsed, list):
        return []
    return [c for c in (clean_category(item) for item in parsed) if c]


def merge_categories(existing: Sequence[str], proposed: Sequence[str]) -> List[str]:
    """Existing categories first and unchanged, then up to
    ``MAX_NEW_CATEGORIES`` new ones; case-insensitive duplicates dropped and
    ``OTHER`` always last."""
    merged: List[str] = []
    seen = {OTHER.lower(), UNCATEGORIZED.lower()}
    added = 0
    for category, is_new in [(c, False) for c in existing] + [
        (c, True) for c in proposed
    ]:
        key = category.lower()
        if not category or key in seen:
            continue
        if is_new:
            if added >= MAX_NEW_CATEGORIES:
                continue
            added += 1
        seen.add(key)
        merged.append(category)
    merged.append(OTHER)
    return merged


def parse_assignments(
    text: Optional[str], batch: Sequence[str], categories: Sequence[str]
) -> Dict[str, str]:
    """Map each procedure of ``batch`` the reply answered to its category.

    The prompt numbers the procedures and the reply is keyed by those
    numbers, so a model that re-spells a procedure cannot misfile it. An
    off-list category becomes ``OTHER``; an unanswered number is left out.
    """
    parsed = _parse_json(text)
    if not isinstance(parsed, dict):
        return {}
    by_key = {c.lower(): c for c in categories}
    assignments: Dict[str, str] = {}
    for key, value in parsed.items():
        try:
            index = int(str(key).strip()) - 1
        except ValueError:
            continue
        if not 0 <= index < len(batch):
            continue
        assignments[batch[index]] = by_key.get(clean_category(value).lower(), OTHER)
    return assignments


async def propose_categories(
    procedures: Sequence[str], existing: Sequence[str]
) -> List[str]:
    """Pass 1: a category list covering ``procedures``, built on
    ``existing``. Returns ``existing`` (plus ``OTHER``) if no model answers."""
    listed = "\n".join(f"- {p}" for p in procedures)
    keep = ""
    if existing:
        keep = (
            "These categories are already in use. Keep every one of them, "
            "spelled exactly as written, and add new ones only for kinds of "
            "care none of them covers:\n"
            + "\n".join(f"- {c}" for c in existing)
            + "\n\n"
        )
    prompt = (
        "Here are procedures, treatments, drugs and services health insurers "
        "denied, most often denied first:\n"
        f"{listed}\n\n"
        f"{keep}"
        "Propose between 6 and 15 broad categories that together cover them "
        '(for example "Imaging", "Mental health", "Specialty drugs", '
        '"Durable medical equipment"). Use short names of one to four words. '
        f'Do not include a catch-all; "{OTHER}" is added for you.\n'
        'Answer with only a JSON array of strings, like ["Imaging", "Surgery"].'
    )
    reply = await infer_with_fallback(
        system_prompts=[_DISCOVER_SYSTEM],
        prompt=prompt,
        temperature=0.2,
        timeout=90.0,
        label="procedure category discovery",
        validator=lambda text: bool(parse_category_list(text)),
    )
    proposed = parse_category_list(reply)
    if not proposed:
        logger.warning("No model proposed procedure categories")
    return merge_categories(existing, proposed)


async def assign_categories(
    procedures: Sequence[str],
    categories: Sequence[str],
    batch_size: int = ASSIGN_BATCH_SIZE,
) -> Dict[str, str]:
    """Pass 2: one category from ``categories`` per procedure the model
    answered for."""
    listed_categories = "\n".join(f"- {c}" for c in categories)
    assignments: Dict[str, str] = {}
    for start in range(0, len(procedures), batch_size):
        batch = list(procedures[start : start + batch_size])
        numbered = "\n".join(f"{i}. {p}" for i, p in enumerate(batch, start=1))
        prompt = (
            f"Categories:\n{listed_categories}\n\n"
            f"Procedures:\n{numbered}\n\n"
            "Put each procedure in the one category that fits it best, using "
            f'"{OTHER}" only when none fits. Answer with only a JSON object '
            "mapping each procedure's number to its category, like "
            f'{{"1": "{categories[0]}", "2": "{OTHER}"}}.'
        )
        reply = await infer_with_fallback(
            system_prompts=[_ASSIGN_SYSTEM],
            prompt=prompt,
            temperature=0.0,
            timeout=60.0,
            label="procedure category assignment",
            # Called before this iteration ends, so ``batch`` is current.
            validator=lambda text: bool(parse_assignments(text, batch, categories)),
        )
        answered = parse_assignments(reply, batch, categories)
        if len(answered) < len(batch):
            logger.info(
                f"Procedure categories: {len(batch) - len(answered)} of "
                f"{len(batch)} left for the next run"
            )
        assignments.update(answered)
    return assignments


@llm_usage.staff_entry("procedure_categories")
async def categorize_procedures(
    *,
    recategorize: bool = False,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Backfill ``ProcedureCategory`` for denied procedures (both passes).

    By default only procedures with no stored category are sorted, against
    the stored taxonomy plus whatever pass 1 adds. ``recategorize`` starts
    the taxonomy fresh and re-sorts every procedure. ``limit`` caps how many
    procedures (most-denied first) pass 2 sorts. ``dry_run`` asks the model
    but writes nothing.
    """
    counts = await adenied_procedure_counts()
    stored: Dict[str, str] = {}
    if not recategorize:
        stored = {
            row.procedure: row.category async for row in ProcedureCategory.objects.all()
        }
    existing = [c for c, _ in Counter(stored.values()).most_common() if c != OTHER]
    todo = [p for p, _ in counts.most_common() if p not in stored]
    if limit is not None:
        todo = todo[:limit]
    stats: Dict[str, Any] = {
        "procedures": len(counts),
        "to_categorize": len(todo),
        "categorized": 0,
        "categories": existing,
    }
    if not todo:
        return stats

    sample = [p for p, _ in counts.most_common(DISCOVERY_SAMPLE)]
    categories = await propose_categories(sample, existing)
    stats["categories"] = categories
    if categories == [OTHER]:
        # No taxonomy to sort into: don't file everything under Other.
        return stats

    assignments = await assign_categories(todo, categories)
    stats["categorized"] = len(assignments)
    stats["assignments"] = assignments
    if not dry_run:
        for procedure, category in assignments.items():
            await ProcedureCategory.objects.aupdate_or_create(
                procedure=procedure, defaults={"category": category}
            )
    return stats

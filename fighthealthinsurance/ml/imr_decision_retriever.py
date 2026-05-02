"""Retrieval helper for prior independent medical review / external appeal decisions.

Surfaces public CA DMHC IMR and NY DFS external appeal decisions that look
similar to a denial so the appeal-generation prompt can reference precedent-like
outcomes ("similar prior decisions"). These are jurisdiction- and fact-specific
and intentionally framed as illustrative rather than legally binding.
"""

import re
from typing import List, Optional

from django.db.models import Case, IntegerField, Q, QuerySet, Value, When
from loguru import logger

from fighthealthinsurance.models import Denial, IMRDecision

_TOKEN_RE = re.compile(r"[A-Za-z0-9]{3,}")
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "into",
    "treatment",
    "therapy",
    "procedure",
    "service",
    "services",
    "care",
    "medical",
    "patient",
    "patients",
    "disease",
    "disorder",
    "condition",
    "syndrome",
    "system",
    "non",
    "use",
    "used",
    "type",
}


def _tokens(text: Optional[str]) -> List[str]:
    if not text:
        return []
    return [
        t.lower()
        for t in _TOKEN_RE.findall(text)
        if t.lower() not in _STOPWORDS and len(t) >= 3
    ]


def _build_text_q(field: str, tokens: List[str]) -> Optional[Q]:
    """Build an OR'd icontains Q across the supplied tokens for a field."""
    if not tokens:
        return None
    q: Optional[Q] = None
    for t in tokens:
        clause = Q(**{f"{field}__icontains": t})
        q = clause if q is None else q | clause
    return q


class IMRDecisionRetriever:
    """Retrieves similar prior IMR / external appeal decisions for a denial."""

    DEFAULT_LIMIT = 5
    PER_DECISION_FINDINGS_CHARS = 600

    @classmethod
    def _build_queryset(
        cls,
        procedure: str,
        diagnosis: str,
        state: Optional[str],
    ) -> QuerySet:
        # Query against the denormalized `search_text` column populated at
        # save() time. Postgres has a GIN trigram index on it (migration 0162);
        # SQLite falls back to a sequential icontains scan.
        all_tokens = _tokens(procedure) + _tokens(diagnosis)
        if not all_tokens:
            return IMRDecision.objects.none()

        match_q = _build_text_q("search_text", all_tokens)
        if match_q is None:
            return IMRDecision.objects.none()
        qs = IMRDecision.objects.filter(match_q)

        # Score: overturned > overturned_in_part > other > upheld; same-state bonus.
        determination_score = Case(
            When(determination=IMRDecision.DETERMINATION_OVERTURNED, then=Value(3)),
            When(
                determination=IMRDecision.DETERMINATION_OVERTURNED_IN_PART,
                then=Value(2),
            ),
            When(determination=IMRDecision.DETERMINATION_OTHER, then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
        if state:
            state_score = Case(
                When(state=state.upper(), then=Value(2)),
                default=Value(0),
                output_field=IntegerField(),
            )
        else:
            state_score = Value(0, output_field=IntegerField())

        return qs.annotate(
            _det_score=determination_score, _state_score=state_score
        ).order_by("-_det_score", "-_state_score", "-decision_year", "-id")

    @classmethod
    async def retrieve_for_denial(
        cls, denial: Denial, limit: int = DEFAULT_LIMIT
    ) -> List[IMRDecision]:
        """Return up to ``limit`` IMRDecision rows similar to the denial."""
        try:
            procedure = (denial.procedure or "").strip()
            diagnosis = (denial.diagnosis or "").strip()
            state = (getattr(denial, "state", None) or "").strip() or None
            if not procedure and not diagnosis:
                return []
            qs = cls._build_queryset(procedure, diagnosis, state)
            results: List[IMRDecision] = [decision async for decision in qs[:limit]]
            logger.debug(
                f"IMR retrieval returned {len(results)} decisions for "
                f"procedure={procedure!r} diagnosis={diagnosis!r} state={state}"
            )
            return results
        except Exception as e:
            logger.opt(exception=True).warning(
                f"IMR retrieval failed for denial {getattr(denial, 'denial_id', '?')}: {e}"
            )
            return []

    @classmethod
    def format_for_appeal(cls, decisions: List[IMRDecision]) -> Optional[str]:
        """Render decisions as a prompt-friendly bullet list, or None if empty.

        Phrased as "similar public appeal decisions", not legal precedent.
        """
        if not decisions:
            return None
        lines = [
            "Similar public independent medical review / external appeal "
            "decisions (illustrative, not legal precedent):",
        ]
        for d in decisions:
            label = {
                IMRDecision.SOURCE_CA_DMHC: "CA DMHC IMR",
                IMRDecision.SOURCE_NY_DFS: "NY DFS External Appeal",
            }.get(d.source, d.source)
            year = f" {d.decision_year}" if d.decision_year else ""
            determination = d.get_determination_display()
            treatment = d.treatment or d.treatment_category or "(unspecified)"
            diagnosis = d.diagnosis or d.diagnosis_category or "(unspecified)"
            findings = (d.findings or "").strip().replace("\n", " ")
            if len(findings) > cls.PER_DECISION_FINDINGS_CHARS:
                findings = findings[: cls.PER_DECISION_FINDINGS_CHARS].rstrip() + "..."
            line = (
                f"- {label}{year} (case {d.case_id}): {determination} - "
                f"treatment {treatment} for {diagnosis}."
            )
            if findings:
                line += f" Reviewer findings: {findings}"
            lines.append(line)
        return "\n".join(lines)

    @classmethod
    async def get_context_for_denial(
        cls, denial: Denial, limit: int = DEFAULT_LIMIT
    ) -> Optional[str]:
        """Convenience wrapper: retrieve + format. Returns None when nothing matched."""
        decisions = await cls.retrieve_for_denial(denial, limit=limit)
        return cls.format_for_appeal(decisions)

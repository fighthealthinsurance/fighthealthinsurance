"""UCR (Usual & Customary Rate) enrichment pipeline.

Computes a benchmark comparison between what an insurer paid for an OON service
and an independent UCR benchmark for the same procedure + geography. The output
gets written to `Denial.ucr_context` and surfaced in the appeal letter prompt
(see UCR-OON-Reimbursement-Plan.md §5.2 and §6.1).

The helper is sync-only on purpose; async callers (the refresh actor) wrap calls
in `sync_to_async`. Keeps the helper simple and directly testable from sync
TestCase classes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from loguru import logger

from fighthealthinsurance.models import (
    Denial,
    UCRGeographicArea,
    UCRLookup,
    UCRRate,
)
from fighthealthinsurance.ucr_constants import (
    UCR_CONTEXT_HASH_KEY,
    UCR_CONTEXT_STATUS_KEY,
    UCR_CONTEXT_STATUS_PENDING,
    UCR_CONTEXT_STATUS_READY,
    UCR_PERCENTILES,
    UCR_SOURCE_PRIORITY,
    UCRAreaKind,
    UCRSource,
)

# 5-digit CPT or 1-letter + 4-digit HCPCS Level II (e.g. J3490, A0428).
# Phase-1 extraction; ML-based extraction lands in phase 2 (§5.3).
_PROCEDURE_CODE_RE = re.compile(r"\b([A-Z]\d{4}|\d{5})\b")


@dataclass
class RateRow:
    """Plain-data view of a UCRRate row, decoupled from ORM for hashing/JSON."""

    percentile: int
    amount_cents: int
    source: str
    effective_date: str
    is_derived: bool = False
    modifier: str = ""


@dataclass
class Comparison:
    """Output of build_comparison; written to Denial.ucr_context."""

    procedure_code: str
    area_kind: str
    area_code: str
    billed_cents: Optional[int]
    allowed_cents: Optional[int]
    paid_cents: Optional[int]
    rates: list[RateRow] = field(default_factory=list)
    gap_p80_cents: Optional[int] = None
    gap_p80_pct: Optional[float] = None
    gap_p90_cents: Optional[int] = None
    gap_p90_pct: Optional[float] = None
    narrative: str = ""
    hash: str = ""

    def to_jsonable(self) -> dict:
        d = {
            "procedure_code": self.procedure_code,
            "area_kind": self.area_kind,
            "area_code": self.area_code,
            "billed_cents": self.billed_cents,
            "allowed_cents": self.allowed_cents,
            "paid_cents": self.paid_cents,
            "rates": [r.__dict__ for r in self.rates],
            "gap_p80_cents": self.gap_p80_cents,
            "gap_p80_pct": self.gap_p80_pct,
            "gap_p90_cents": self.gap_p90_cents,
            "gap_p90_pct": self.gap_p90_pct,
            "narrative": self.narrative,
            UCR_CONTEXT_HASH_KEY: self.hash,
            UCR_CONTEXT_STATUS_KEY: UCR_CONTEXT_STATUS_READY,
        }
        return d


# Map from (procedure_code, area_id) -> list[RateRow]; keys are stable so the
# actor can pre-fetch one batch query and pass this dict in.
RateCache = Mapping[tuple[str, int], list[RateRow]]


class UCREnrichmentHelper:
    """Pricing comparison helper. Class-level functions match the project's
    `DenialCreatorHelper` / `AppealsBackendHelper` style (see CLAUDE.md
    'Important Patterns')."""

    # ------------------------------------------------------------------ public

    @classmethod
    def maybe_enrich(
        cls,
        denial: Denial,
        *,
        force: bool = False,
        rates: Optional[RateCache] = None,
    ) -> Optional[Comparison]:
        """Compute (and usually persist) a UCR comparison for `denial`.

        Returns the Comparison whenever one could be produced; only returns
        None if the denial was *skipped* (finalized appeal, no code resolved,
        no area resolved, or no rates available). When the comparison hash
        matches what's already persisted, the helper short-circuits the
        UCRLookup insert and the ucr_context rewrite (only `ucr_refreshed_at`
        is bumped) but still returns the Comparison so callers can render or
        inspect it.
        """
        if cls._is_finalized(denial):
            logger.debug("UCR enrich skipped: denial {} is finalized", denial.pk)
            return None

        code = cls.resolve_procedure_code(denial)
        if not code:
            logger.debug("UCR enrich skipped: no procedure code for {}", denial.pk)
            return None

        area = cls.resolve_geographic_area(denial)
        if area is None:
            logger.debug("UCR enrich skipped: no area resolved for {}", denial.pk)
            return None

        rate_rows = cls._lookup_rates(
            code, area, rates, modifier=denial.procedure_modifier or ""
        )
        if not rate_rows:
            logger.debug("UCR enrich skipped: no rates for code={} area={}", code, area)
            return None

        comparison = cls.build_comparison(
            procedure_code=code,
            area=area,
            billed_cents=denial.get_billed_cents(),
            allowed_cents=denial.get_allowed_cents(),
            paid_cents=denial.get_paid_cents(),
            rates=rate_rows,
        )

        if not force and cls._hash_unchanged(denial, comparison):
            cls._mark_refreshed(denial)
            logger.debug("UCR enrich short-circuit (hash match) for {}", denial.pk)
            return comparison

        cls._persist(denial, comparison)
        cls.prune_lookups(denial)
        return comparison

    @classmethod
    def bulk_load_rates(cls, denials: Sequence[Denial]) -> RateCache:
        """Pre-fetch rates for the (code, area) pairs across `denials`.

        Returns a dict the actor can hand to subsequent maybe_enrich() calls so
        each one is a dict lookup instead of a DB round-trip (§10.4).
        """
        pairs: set[tuple[str, int]] = set()
        for d in denials:
            code = cls.resolve_procedure_code(d)
            area = cls.resolve_geographic_area(d)
            if code and area is not None:
                pairs.add((code, area.pk))

        if not pairs:
            return {}

        codes = {p[0] for p in pairs}
        area_ids = {p[1] for p in pairs}
        # Order by -effective_date so the per-percentile dedupe applied later
        # (when we know the per-denial modifier) prefers newer rows.
        qs = (
            UCRRate.objects.filter(
                procedure_code__in=codes,
                geographic_area_id__in=area_ids,
                percentile__in=UCR_PERCENTILES,
            )
            .filter(
                Q(expires_date__isnull=True)
                | Q(expires_date__gte=timezone.now().date())
            )
            .order_by("-effective_date")
        )

        cache: dict[tuple[str, int], list[RateRow]] = {}
        for rate in qs:
            key = (rate.procedure_code, rate.geographic_area_id)
            if key not in pairs:
                continue
            cache.setdefault(key, []).append(cls._rate_to_row(rate))
        return cache

    @classmethod
    def resolve_procedure_code(cls, denial: Denial) -> str:
        """Pull a CPT/HCPCS code from `denial`, preferring explicit fields."""
        for explicit in (
            denial.procedure_code,
            getattr(denial, "verified_procedure", "") or "",
            denial.procedure or "",
        ):
            if not explicit:
                continue
            match = _PROCEDURE_CODE_RE.search(explicit.upper())
            if match:
                return match.group(1)
        return ""

    @classmethod
    def resolve_geographic_area(cls, denial: Denial) -> Optional[UCRGeographicArea]:
        """ZIP3 from service_zip if available, fall back to state, then national."""
        if denial.service_zip and len(denial.service_zip) >= 3:
            zip3 = denial.service_zip[:3]
            area = UCRGeographicArea.objects.filter(
                kind=UCRAreaKind.ZIP3, code=zip3
            ).first()
            if area is not None:
                return area
        state = (denial.your_state or "").strip().upper()
        if state:
            area = UCRGeographicArea.objects.filter(
                kind=UCRAreaKind.STATE, code=state
            ).first()
            if area is not None:
                return area
        return UCRGeographicArea.objects.filter(kind=UCRAreaKind.NATIONAL).first()

    @classmethod
    def build_comparison(
        cls,
        *,
        procedure_code: str,
        area: UCRGeographicArea,
        billed_cents: Optional[int],
        allowed_cents: Optional[int],
        paid_cents: Optional[int],
        rates: Iterable[RateRow],
    ) -> Comparison:
        rates_list = list(rates)
        rates_list.sort(key=lambda r: r.percentile)
        comparison = Comparison(
            procedure_code=procedure_code,
            area_kind=area.kind,
            area_code=area.code,
            billed_cents=billed_cents,
            allowed_cents=allowed_cents,
            paid_cents=paid_cents,
            rates=rates_list,
        )
        cls._compute_gaps(comparison)
        comparison.narrative = cls.build_narrative(comparison)
        comparison.hash = cls._hash_comparison(comparison)
        return comparison

    @classmethod
    def build_narrative(cls, c: Comparison) -> str:
        """Produce the [UCR PRICING CONTEXT] block injected into ML prompts (§6.1)."""
        lines: list[str] = ["[UCR PRICING CONTEXT]"]
        lines.append(f"Procedure: {c.procedure_code}")
        lines.append(f"Geographic area: {c.area_kind} {c.area_code}")
        if c.billed_cents is not None:
            lines.append(f"Billed: {_dollars(c.billed_cents)}")
        if c.allowed_cents is not None:
            lines.append(f"Insurer allowed: {_dollars(c.allowed_cents)}")
        if c.paid_cents is not None:
            lines.append(f"Insurer paid: {_dollars(c.paid_cents)}")
        if c.rates:
            # The narrative emphasizes the proxy-p80 gap, so attribute the
            # header to the p80 row when available; otherwise fall back to
            # the highest-priority rate by source priority then newest date.
            representative = next((r for r in c.rates if r.percentile == 80), None)
            if representative is None:
                priority_index = {s: i for i, s in enumerate(UCR_SOURCE_PRIORITY)}
                representative = max(
                    c.rates,
                    key=lambda r: (
                        priority_index.get(r.source, -1),
                        r.effective_date,
                    ),
                )
            lines.append(
                f"Independent benchmark ({representative.source}, "
                f"effective {representative.effective_date}):"
            )
            for r in c.rates:
                tag = " (derived)" if r.is_derived else ""
                lines.append(f"  - p{r.percentile}: {_dollars(r.amount_cents)}{tag}")
        if c.gap_p80_cents is not None and c.gap_p80_pct is not None:
            lines.append(
                f"Gap vs. proxy-p80 benchmark: {_dollars(c.gap_p80_cents)} "
                f"({c.gap_p80_pct:.0f}%) under-reimbursed"
            )
        lines.append("[/UCR PRICING CONTEXT]")
        return "\n".join(lines)

    @classmethod
    def prune_lookups(cls, denial: Denial) -> int:
        """Trim oldest UCRLookup rows over UCR_LOOKUP_RETENTION_PER_DENIAL.

        Always preserves Denial.latest_ucr_lookup_id (§10.5).
        Returns the number of rows deleted.
        """
        cap = settings.UCR_LOOKUP_RETENTION_PER_DENIAL
        keepers = list(
            UCRLookup.objects.filter(denial=denial)
            .order_by("-created")
            .values_list("id", flat=True)[:cap]
        )
        protected = set(keepers)
        if denial.latest_ucr_lookup_id:
            protected.add(denial.latest_ucr_lookup_id)
        deleted, _ = (
            UCRLookup.objects.filter(denial=denial).exclude(id__in=protected).delete()
        )
        return deleted

    # ----------------------------------------------------------------- private

    @staticmethod
    def _is_finalized(denial: Denial) -> bool:
        return bool(denial.appeal_result)

    @classmethod
    def _lookup_rates(
        cls,
        code: str,
        area: UCRGeographicArea,
        rate_cache: Optional[RateCache],
        modifier: str = "",
    ) -> list[RateRow]:
        if rate_cache is not None:
            cached = rate_cache.get((code, area.pk))
            if cached is not None:
                # bulk_load_rates already applied dedupe; filter to the
                # caller's modifier preference.
                return cls._select_modifier_rows(cached, modifier)

        modifier_filter = (
            Q(modifier=modifier) | Q(modifier="") if modifier else Q(modifier="")
        )
        qs = (
            UCRRate.objects.filter(
                procedure_code=code,
                geographic_area=area,
                percentile__in=UCR_PERCENTILES,
            )
            .filter(modifier_filter)
            .filter(
                Q(expires_date__isnull=True)
                | Q(expires_date__gte=timezone.now().date())
            )
            .order_by("-effective_date")
        )
        rows = [cls._rate_to_row(r, modifier=r.modifier) for r in qs]
        return cls._dedupe_by_priority(rows, prefer_modifier=modifier)

    @staticmethod
    def _rate_to_row(rate: UCRRate, *, modifier: str = "") -> RateRow:
        return RateRow(
            percentile=rate.percentile,
            amount_cents=rate.amount_cents,
            source=rate.source,
            effective_date=rate.effective_date.isoformat(),
            is_derived=bool(rate.metadata and rate.metadata.get("derived_from")),
            modifier=modifier or rate.modifier,
        )

    @staticmethod
    def _dedupe_by_priority(
        rows: list[RateRow], *, prefer_modifier: str = ""
    ) -> list[RateRow]:
        """Pick one row per percentile.

        Tiebreakers (highest wins):
          1. Exact modifier match (vs blank fallback) when prefer_modifier is set.
          2. Source priority per UCR_SOURCE_PRIORITY (§3.1).
          3. Newer effective_date.
        """
        priority_index = {s: i for i, s in enumerate(UCR_SOURCE_PRIORITY)}

        def score(r: RateRow) -> tuple[int, int, str]:
            modifier_match = (
                1 if prefer_modifier and r.modifier == prefer_modifier else 0
            )
            return (
                modifier_match,
                priority_index.get(r.source, -1),
                r.effective_date,
            )

        by_pct: dict[int, RateRow] = {}
        for r in rows:
            existing = by_pct.get(r.percentile)
            if existing is None or score(r) > score(existing):
                by_pct[r.percentile] = r
        return sorted(by_pct.values(), key=lambda r: r.percentile)

    @staticmethod
    def _select_modifier_rows(cached: list[RateRow], modifier: str) -> list[RateRow]:
        """Filter a pre-cached rate list down to (modifier match preferred, blank
        fallback) per percentile. Mirrors the DB-side filter in _lookup_rates so
        bulk-prefetched callers see the same selection as on-demand callers."""
        candidates = [r for r in cached if not modifier or r.modifier in (modifier, "")]
        return UCREnrichmentHelper._dedupe_by_priority(
            candidates, prefer_modifier=modifier
        )

    @staticmethod
    def _compute_gaps(c: Comparison) -> None:
        if c.allowed_cents is None:
            return
        for r in c.rates:
            if r.percentile == 80:
                c.gap_p80_cents = r.amount_cents - c.allowed_cents
                if r.amount_cents > 0:
                    c.gap_p80_pct = round(c.gap_p80_cents / r.amount_cents * 100, 1)
            elif r.percentile == 90:
                c.gap_p90_cents = r.amount_cents - c.allowed_cents
                if r.amount_cents > 0:
                    c.gap_p90_pct = round(c.gap_p90_cents / r.amount_cents * 100, 1)

    @staticmethod
    def _hash_comparison(c: Comparison) -> str:
        # Hash inputs that drive the narrative; intentionally excludes `narrative`
        # itself (since narrative is derived) and `hash` (chicken-and-egg).
        material = {
            "procedure_code": c.procedure_code,
            "area_kind": c.area_kind,
            "area_code": c.area_code,
            "billed_cents": c.billed_cents,
            "allowed_cents": c.allowed_cents,
            "paid_cents": c.paid_cents,
            "rates": [r.__dict__ for r in c.rates],
        }
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True).encode()
        ).hexdigest()
        return digest

    @staticmethod
    def _hash_unchanged(denial: Denial, comparison: Comparison) -> bool:
        prior = denial.ucr_context or {}
        return prior.get(UCR_CONTEXT_HASH_KEY) == comparison.hash

    @staticmethod
    def _mark_refreshed(denial: Denial) -> None:
        denial.ucr_refreshed_at = timezone.now()
        denial.save(update_fields=["ucr_refreshed_at"])

    @classmethod
    def _persist(cls, denial: Denial, comparison: Comparison) -> None:
        with transaction.atomic():
            lookup = UCRLookup.objects.create(
                denial=denial,
                procedure_code=comparison.procedure_code,
                modifier=denial.procedure_modifier or "",
                service_zip=denial.service_zip or "",
                matched_area_id=cls._area_id_from_comparison(comparison),
                rates_snapshot=[r.__dict__ for r in comparison.rates],
                billed_amount_cents=comparison.billed_cents,
                allowed_amount_cents=comparison.allowed_cents,
                paid_amount_cents=comparison.paid_cents,
            )
            denial.ucr_context = comparison.to_jsonable()
            denial.latest_ucr_lookup = lookup
            denial.ucr_refreshed_at = timezone.now()
            denial.save(
                update_fields=[
                    "ucr_context",
                    "latest_ucr_lookup",
                    "ucr_refreshed_at",
                ]
            )

    @staticmethod
    def _area_id_from_comparison(comparison: Comparison) -> Optional[int]:
        """Look up the area id from the comparison's (kind, code) tuple."""
        area = UCRGeographicArea.objects.filter(
            kind=comparison.area_kind, code=comparison.area_code
        ).first()
        return area.pk if area else None


def make_pending_context() -> dict:
    """Returned to API callers when enrichment hasn't completed yet (§7)."""
    return {UCR_CONTEXT_STATUS_KEY: UCR_CONTEXT_STATUS_PENDING}


def _dollars(cents: int) -> str:
    return f"${cents / 100:.2f}"

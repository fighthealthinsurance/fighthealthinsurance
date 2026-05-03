"""UCR (Usual & Customary Rate) enrichment pipeline.

Computes a benchmark comparison between what an insurer paid for an OON service
and an independent UCR benchmark for the same procedure + geography. The output
gets written to `Denial.ucr_context` and surfaced in the appeal letter prompt
via AppealGenerator.make_open_prompt's UCR PRICING block.

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

from fighthealthinsurance.medical_code_extractor import extract_procedure_codes
from fighthealthinsurance.models import (
    Denial,
    UCRGeographicArea,
    UCRLookup,
    UCRRate,
)
from fighthealthinsurance.ucr_constants import (
    UCR_CONTEXT_HASH_KEY,
    UCR_PERCENTILES,
    UCR_SOURCE_PRIORITY,
    UCR_SOURCE_URLS,
    UCRAreaKind,
    UCRSource,
)

# ---------------------------------------------------- OON heuristic detector

# Compiled at import so the gate is a constant-cost check on every denial. All
# three patterns must match (case-insensitive) before we bother dispatching
# enrichment, otherwise we'd run rate lookups against denials that have no
# under-reimbursement context to argue.
_OON_KEYWORDS_RE = re.compile(
    r"\b(?:out[\s-]?of[\s-]?network|non[\s-]?participating|non[\s-]?par|OON)\b",
    re.IGNORECASE,
)
_ALLOWED_AMOUNT_RE = re.compile(
    r"\ballowed\s+(?:amount|charge|amounts|charges)\b",
    re.IGNORECASE,
)
_USUAL_AND_CUSTOMARY_RE = re.compile(
    r"\b(?:usual\s+and\s+customary|usual,\s*customary,?\s*and\s+reasonable|UCR)\b",
    re.IGNORECASE,
)


def is_under_reimbursement_claim(denial_text: Optional[str]) -> bool:
    """Heuristic gate: does this denial look like an OON under-reimbursement?

    Returns True only when the text contains all three signals. The gate is
    intentionally conservative — false negatives (missed enrichment) are
    cheaper than false positives (wasted rate lookups + an irrelevant UCR
    block in the appeal prompt).
    """
    if not denial_text:
        return False
    return bool(
        _OON_KEYWORDS_RE.search(denial_text)
        and _ALLOWED_AMOUNT_RE.search(denial_text)
        and _USUAL_AND_CUSTOMARY_RE.search(denial_text)
    )


def dispatch_ucr_refresh(denial_id: int) -> None:
    """Fire-and-forget UCRRefreshActor.refresh_denial.remote(...).

    The work itself is DB-only — UCREnrichmentHelper.maybe_enrich resolves
    procedure/area/rates against existing UCRRate/UCRGeographicArea rows and
    writes a UCRLookup snapshot. CSV downloads only happen on the actor's
    independent source-refresh loop, never on this per-denial path.

    Failures (Ray not available, e.g. in tests) fall back to a synchronous
    enrichment so the user-facing flow still works. The actor is the primary
    path in production.
    """
    try:
        from fighthealthinsurance.ucr_refresh_actor_ref import ucr_refresh_actor_ref

        actor, _task = ucr_refresh_actor_ref.get  # type: ignore[misc]
        actor.refresh_denial.remote(denial_id)
        return
    except Exception:
        logger.opt(exception=True).warning(
            "UCR actor dispatch unavailable; falling back to inline enrich"
        )

    try:
        denial = Denial.objects.get(pk=denial_id)
        UCREnrichmentHelper.maybe_enrich(denial, force=True)
    except Exception:
        logger.opt(exception=True).error(
            "UCR inline fallback failed for denial_id={}", denial_id
        )


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
    rates: list[RateRow] = field(default_factory=list)
    narrative: str = ""
    hash: str = ""

    def to_jsonable(self) -> dict:
        return {
            "procedure_code": self.procedure_code,
            "area_kind": self.area_kind,
            "area_code": self.area_code,
            "rates": [r.__dict__ for r in self.rates],
            "narrative": self.narrative,
            UCR_CONTEXT_HASH_KEY: self.hash,
        }


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

        # Skip paths below all bump ucr_refreshed_at before returning so the
        # actor's stale-batch query (NULL OR < cutoff) doesn't reselect this
        # denial on every cycle and starve real work.
        code = cls.resolve_procedure_code(denial)
        if not code:
            logger.debug("UCR enrich skipped: no procedure code for {}", denial.pk)
            cls._mark_refreshed(denial)
            return None

        area = cls.resolve_geographic_area(denial)
        if area is None:
            logger.debug("UCR enrich skipped: no area resolved for {}", denial.pk)
            cls._mark_refreshed(denial)
            return None

        rate_rows = cls._lookup_rates(code, area, rates, modifier="")
        if not rate_rows:
            logger.debug("UCR enrich skipped: no rates for code={} area={}", code, area)
            cls._mark_refreshed(denial)
            return None

        comparison = cls.build_comparison(
            procedure_code=code,
            area=area,
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
        each one is a dict lookup instead of a DB round-trip.
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
        """Pull a CPT/HCPCS code from `denial`'s free-text fields.

        Delegates extraction to the project's centralized
        `medical_code_extractor` so we don't drift from the canonical regex.
        """
        for explicit in (
            getattr(denial, "verified_procedure", "") or "",
            denial.procedure or "",
            denial.denial_text or "",
        ):
            if not explicit:
                continue
            codes = sorted(extract_procedure_codes(explicit))
            if codes:
                return codes[0]
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
        rates: Iterable[RateRow],
    ) -> Comparison:
        rates_list = list(rates)
        rates_list.sort(key=lambda r: r.percentile)
        comparison = Comparison(
            procedure_code=procedure_code,
            area_kind=area.kind,
            area_code=area.code,
            rates=rates_list,
        )
        comparison.narrative = cls.build_narrative(comparison)
        comparison.hash = cls._hash_comparison(comparison)
        return comparison

    @classmethod
    def build_narrative(cls, c: Comparison) -> str:
        """Produce the [UCR PRICING CONTEXT] block injected into ML prompts.

        Rate-table only — billing-amount math is intentionally absent; the
        appeal LLM decides whether the benchmark evidence is useful for the
        argument it's constructing.
        """
        lines: list[str] = ["[UCR PRICING CONTEXT]"]
        lines.append(f"Procedure: {c.procedure_code}")
        lines.append(f"Geographic area: {c.area_kind} {c.area_code}")
        if c.rates:
            # Attribute the header to p80 when available; otherwise fall back
            # to the highest-priority rate by source priority then newest date.
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
            source_url = UCR_SOURCE_URLS.get(representative.source)
            if source_url:
                lines.append(f"Source: {source_url}")
            for r in c.rates:
                tag = " (derived)" if r.is_derived else ""
                lines.append(f"  - p{r.percentile}: {_dollars(r.amount_cents)}{tag}")
        lines.append("[/UCR PRICING CONTEXT]")
        return "\n".join(lines)

    @classmethod
    def prune_lookups(cls, denial: Denial) -> int:
        """Trim oldest UCRLookup rows over UCR_LOOKUP_RETENTION_PER_DENIAL.

        Always preserves Denial.latest_ucr_lookup_id.
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
          2. Source priority per UCR_SOURCE_PRIORITY.
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
        if modifier:
            candidates = [r for r in cached if r.modifier in (modifier, "")]
        else:
            # Empty modifier means "the unspecified-modifier rate set" — must
            # not silently mix in modifier-specific rows from the cache.
            candidates = [r for r in cached if r.modifier == ""]
        return UCREnrichmentHelper._dedupe_by_priority(
            candidates, prefer_modifier=modifier
        )

    @staticmethod
    def _hash_comparison(c: Comparison) -> str:
        # Hash inputs that drive the narrative; intentionally excludes `narrative`
        # itself (since narrative is derived) and `hash` (chicken-and-egg).
        material = {
            "procedure_code": c.procedure_code,
            "area_kind": c.area_kind,
            "area_code": c.area_code,
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
                service_zip=denial.service_zip or "",
                matched_area_id=cls._area_id_from_comparison(comparison),
                rates_snapshot=[r.__dict__ for r in comparison.rates],
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


def _dollars(cents: int) -> str:
    return f"${cents / 100:.2f}"

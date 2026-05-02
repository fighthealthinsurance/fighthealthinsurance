# Plan: Usual & Customary (UCR) Data for OON Under-Reimbursement Appeals

## 1. Problem statement

When a patient sees an out-of-network (OON) provider, the insurer typically reimburses
based on its own "usual, customary, and reasonable" (UCR) determination rather than the
billed amount. Insurers routinely use proprietary or low UCR data, leaving the patient
exposed to large balance bills. Appeals against these under-reimbursements are most
persuasive when they cite an independent UCR benchmark for the same CPT/HCPCS code in
the same geographic area at a defensible percentile (typically 80th or 90th).

FHI today has no structured concept of billed/allowed/paid amounts, no procedure-code
field (only freeform `procedure` text), and no UCR reference data. This plan adds that
capability without disrupting the existing denial/appeal flow.

## 2. Scope

In scope
- A queryable UCR rate table keyed on CPT/HCPCS code + geographic area + percentile.
- Capture of billed/allowed/paid amounts and service ZIP on intake (optional fields).
- Code extraction so freeform procedure text resolves to a CPT/HCPCS code.
- A helper that computes a UCR comparison and injects it into the ML appeal context.
- REST + UI surfaces to display the comparison and let users edit/override it.
- Seed loader for CMS Medicare Physician Fee Schedule (free baseline data).

Out of scope (initial release)
- Direct integration with FAIR Health, Context4, or other commercial datasets
  (deferred to phase 2 once licensing is decided — see §12).
- No Surprises Act / IDR submission automation.
- Anesthesia conversion factor / time-unit pricing (separate engine).
- DME, rx, and lab pricing (different reference datasets).

## 3. Data sourcing strategy

Phased approach so we can ship something useful without waiting on commercial licensing.

### 3.1 Canonical references (defined once, used throughout)

- **Percentiles tracked**: 50, 80, 90. (Schema permits any integer; the loader and
  helper only emit these three by default.)
- **Source identifiers** (used in `UCRRate.source` and lookup priority):
  `medicare_pfs`, `fair_health`, `fhi_aggregate`. Defined as a `TextChoices` enum
  in §4.1.
- **Source priority** when more than one source matches a query:
  `fhi_aggregate > fair_health > medicare_pfs`. The priority is data; later
  sections cite this list rather than redefining it.
- **Default Medicare → percentile multipliers** (proxy values used until commercial
  data lands): p50 = 1.5×, p80 = 2.0×, p90 = 2.5×. Configurable via
  `UCR_MEDICARE_PERCENTILE_MULTIPLIERS` (§10.4 settings).

### 3.2 Sources

| Phase | Source | Cost | Granularity | Why |
|-------|--------|------|-------------|-----|
| 1 | CMS Medicare Physician Fee Schedule (PFS) | Free | National + locality (MAC) | Public, downloadable, defensible baseline. Insurers commonly pay 100–300% of Medicare. |
| 1 | Static percentile multipliers on Medicare (see §3.1) | — | — | Lets us emit a benchmark even before commercial data lands. Cite as "Medicare × N", not as a measured percentile, until §12.2 review. |
| 2 | FAIR Health (FH Benchmarks API or licensed flatfiles) | $$ | ZIP3 + percentile | Industry-standard post-Ingenix benchmark; the "name brand" dataset most appeals cite. |
| 2 | Self-reported EOB amounts from FHI users (consented) | Free | Builds over time | Long-term: aggregate de-identified billed/allowed pairs into our own dataset. |
| 3 | Context4 / MultiPlan-comparable datasets | $$ | Variable | Only if we need to rebut insurer-cited data point-for-point. |

The schema below is source-agnostic — every rate row carries a `source` and
`effective_date`, so multiple datasets coexist and are selected by §3.1's priority.

## 4. Data model changes

### 4.1 New models (in `fighthealthinsurance/models.py`)

String-typed values (source identifiers, area kinds) are declared once as
`TextChoices` enums in a new `fighthealthinsurance/ucr_constants.py` so callers
import constants instead of repeating string literals. See §3.1 for the
canonical set of values.

```python
# ucr_constants.py
class UCRSource(models.TextChoices):
    MEDICARE_PFS = "medicare_pfs", "CMS Medicare PFS"
    FAIR_HEALTH = "fair_health", "FAIR Health"
    FHI_AGGREGATE = "fhi_aggregate", "FHI Aggregate"


class UCRAreaKind(models.TextChoices):
    ZIP3 = "zip3", "ZIP3"
    MSA = "msa", "MSA"
    STATE = "state", "State"
    NATIONAL = "national", "National"
    MEDICARE_LOCALITY = "medicare_locality", "Medicare Locality"


# models.py
class UCRGeographicArea(models.Model):
    """A geographic billing area. Independent of source so multiple datasets share it."""
    kind = models.CharField(max_length=24, choices=UCRAreaKind.choices)
    code = models.CharField(max_length=32, db_index=True)  # e.g. "941", "31080", "CA"
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = [("kind", "code")]


class UCRRate(models.Model):
    """A single UCR rate observation. Source-agnostic; see §3.1 for value sets."""
    procedure_code = models.CharField(max_length=10, db_index=True)  # CPT or HCPCS
    modifier = models.CharField(max_length=4, blank=True, default="")
    geographic_area = models.ForeignKey(UCRGeographicArea, on_delete=models.PROTECT)
    percentile = models.PositiveSmallIntegerField()   # see §3.1
    amount_cents = models.PositiveIntegerField()      # cents to avoid float
    source = models.CharField(max_length=32, choices=UCRSource.choices)
    effective_date = models.DateField()
    expires_date = models.DateField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)  # MAC locality, conversion factor, etc.

    class Meta:
        indexes = [
            # Range scans during lookup ("rates for CPT in area on date D"):
            models.Index(fields=["procedure_code", "geographic_area",
                                 "effective_date", "expires_date"]),
            models.Index(fields=["source", "effective_date"]),
        ]
        unique_together = [("procedure_code", "modifier", "geographic_area",
                            "percentile", "source", "effective_date")]


class UCRLookup(models.Model):
    """Audit log of UCR queries tied to a denial. Persists what we showed the user
    even if upstream rates change."""
    denial = models.ForeignKey("Denial", on_delete=models.CASCADE, related_name="ucr_lookups")
    procedure_code = models.CharField(max_length=10)
    modifier = models.CharField(max_length=4, blank=True, default="")
    service_zip = models.CharField(max_length=5, blank=True)
    matched_area = models.ForeignKey(UCRGeographicArea, null=True, on_delete=models.SET_NULL)
    rates_snapshot = models.JSONField()  # list of {percentile, amount_cents, source}
    billed_amount_cents = models.PositiveIntegerField(null=True, blank=True)
    allowed_amount_cents = models.PositiveIntegerField(null=True, blank=True)
    paid_amount_cents = models.PositiveIntegerField(null=True, blank=True)
    created = models.DateTimeField(db_default=Now())
```

### 4.2 Extensions to `Denial`

Six billing fields (encrypted — see §10.3), one timestamp, one FK to the active
audit snapshot, and one JSON context field. All nullable/blank so existing
intake keeps working.

```python
# Inside Denial (around models.py:1235+)
# Billing/geographic intake fields. Amounts are stored as EncryptedCharField
# (string of decimal cents) per the §10.3 phase-1 encryption requirement;
# helpers wrap parse/format so callers see ints.
service_zip = models.CharField(max_length=5, blank=True, default="")
procedure_code = models.CharField(max_length=10, blank=True, default="", db_index=True)
procedure_modifier = EncryptedCharField(max_length=4, blank=True, default="")
billed_amount_cents = EncryptedCharField(max_length=16, blank=True, default="")
allowed_amount_cents = EncryptedCharField(max_length=16, blank=True, default="")
paid_amount_cents = EncryptedCharField(max_length=16, blank=True, default="")

# Enrichment state.
# ucr_refreshed_at: flat indexed DateTimeField so the refresh actor can find
#   stale rows without scanning JSON paths (see §10.4). NULL = needs enrichment.
# latest_ucr_lookup: durable pointer to the active audit snapshot so retention
#   pruning (see §10.5) keeps the right row instead of inferring from JSON.
ucr_refreshed_at = models.DateTimeField(null=True, blank=True, db_index=True)
latest_ucr_lookup = models.ForeignKey(
    "UCRLookup", null=True, blank=True,
    on_delete=models.SET_NULL, related_name="+",
)
ucr_context = models.JSONField(null=True, blank=True)
```

`ucr_context` is a sibling of `ml_citation_context` (`models.py:1339`), not a
replacement — citation context is medical-necessity evidence; UCR context is
structured pricing data with a different shape and audit lifecycle.

### 4.3 Migrations

One migration per logical change so they're easy to revert:
1. `000X_add_ucr_models.py` — `UCRGeographicArea`, `UCRRate`, `UCRLookup`.
2. `000X_denial_financial_fields.py` — the six fields above.
3. Data migration `000X_seed_ucr_geographic_areas.py` — populates national + state
   areas (small, fits in fixture). ZIP3 and Medicare localities come via management
   command (large).

## 5. Detection & enrichment pipeline

### 5.1 When does UCR enrichment fire?

A denial is an under-reimbursement candidate when **any** of:
- `provider_in_network is False` AND `paid_amount_cents < billed_amount_cents`
- The denial text contains classifier hits for "out of network", "balance bill",
  "allowed amount", "UCR", "usual and customary" (cheap regex first, ML classifier
  later).
- The user explicitly indicates "underpaid claim" on intake (new form option).

### 5.2 Pipeline (new helper: `fighthealthinsurance/ucr_helper.py`)

```text
DenialCreatorHelper.create_or_update_denial()       # common_view_logic.py:940+
  └─ dispatches (does not await) UCRRefreshActor.refresh_denial.remote(denial.id)
       (§10.4). Request returns immediately with ucr_context = {"status":
       "pending"}.

UCREnrichmentHelper.maybe_enrich(denial)            # called by the actor
  ├─ resolve_procedure_code(denial.procedure | denial.verified_procedure)
  │     §5.3
  ├─ resolve_geographic_area(denial.service_zip[:3] | denial.your_state)
  │     ZIP5 truncated to ZIP3; fall back to state, then national
  ├─ query_rates(code, area)
  │     one query: percentile__in=§3.1 percentiles, ordered by source priority
  ├─ build_comparison(billed, allowed, paid, rates)
  │     returns dict with gaps + narrative + a hash of the comparison
  ├─ skip-if-unchanged: if hash matches denial.ucr_context["hash"],
  │     bump ucr_refreshed_at only — no UCRLookup insert, no context rewrite
  ├─ otherwise: insert UCRLookup, write denial.ucr_context, set ucr_refreshed_at
  └─ save
```

Same helper is invoked by the periodic refresh actor (§10.4) for stale denials
and after upstream source refreshes.

### 5.3 Procedure-code extraction

`Denial.procedure` is freeform text. We need a CPT/HCPCS code to query UCR.

Options (in order of cost/quality):
1. Regex extraction of explicit CPT codes from the denial text or procedure field
   (handles ~30% of cases where the denial document quotes the code).
2. ML lookup: prompt the existing model to map the procedure description to a
   CPT code, with the canonical CPT code-set as part of context. Reuse the same
   ML routing pattern as `ml_citation_context`. Confidence threshold required.
3. User confirmation: if extracted, show "we believe this is CPT 99213 — confirm"
   in the chat interface. Captured via a new `procedure_code` field on intake.

Phase 1: ship #1 + #3 (regex + manual entry on intake form). #2 follows in phase 2.

## 6. Appeal generation integration

### 6.1 Prompt context

`AppealAssemblyHelper.create_or_update_appeal()` (`common_view_logic.py:209`) and
the upstream ML prompt assembly need a new section. Pattern: when
`denial.ucr_context` is non-null and the denial is OON, append a structured block:

```text
[UCR PRICING CONTEXT]
Procedure: 99213 (Office visit, established patient)
Geographic area: ZIP3 941 (San Francisco)
Billed: $250.00
Insurer allowed: $80.00
Insurer paid: $64.00 (after 20% coinsurance)
Independent benchmark (CMS Medicare PFS, 2026, locality 5):
  - Medicare allowed amount: $98.42
  - 200% of Medicare (proxy p80 per §3.1): $196.84
  - 250% of Medicare (proxy p90 per §3.1): $246.05
Gap vs. proxy-p80 benchmark: $116.84 (59%) under-reimbursed
Source: medicare_pfs effective 2026-01-01 (multipliers are derived, not measured)
[/UCR PRICING CONTEXT]
```

The model is then instructed (via system prompt update) to cite the gap and the
source explicitly when generating the appeal letter, and to request reprocessing
under the plan's "out-of-network allowable" methodology.

### 6.2 New appeal template

Add a new `AppealTemplates` row keyed for under-reimbursement appeals, separate
from medical-necessity templates. Loaded via `fighthealthinsurance/fixtures/initial.yaml`.

## 7. REST API surface

Pattern matches existing ViewSet + `@action` style in `rest_views.py`.

```http
POST /ziggy/rest/denial/{id}/set_billing_info/
  body: {service_zip, procedure_code, procedure_modifier,
         billed_amount_cents, allowed_amount_cents, paid_amount_cents}
  (keys match Denial field names from §4.2 — the serializer is a direct
   ModelSerializer with no implicit renaming)
  dispatches upar.refresh_denial.remote(id); returns 202 immediately

GET  /ziggy/rest/denial/{id}/ucr_context/
  returns the resolved UCRLookup snapshot + comparison
  (or {"status": "pending"} when ucr_refreshed_at is NULL)

POST /ziggy/rest/denial/{id}/refresh_ucr/
  same dispatch as set_billing_info but without a body change; for "try again"

GET  /ziggy/rest/ucr/lookup/?cpt=99213&zip=94110
  unauthenticated, rate-limited. Backed by Django cache keyed on
  (cpt, zip3, latest_effective_date) with a 24h TTL — invalidated by the
  source-refresh loop in §10.4 when a new effective_date lands. Never queries
  per-denial data.
```

Serializer additions in `rest_serializers.py`:
- Extend `DenialResponseInfoSerializer` with the six new billing/geographic fields
  plus `ucr_refreshed_at`.
- New `UCRContextSerializer` for the `ucr_context` and `UCRLookup` payload.
- New `UCRRateLookupSerializer` for the unauthenticated lookup endpoint.

## 8. Frontend changes

Webpack-bundled React in `fighthealthinsurance/static/js/`.

### 8.1 Intake
Add an optional "Billing details" expandable section to the denial intake form
(currently collected via the question system in `forms/questions.py`). Captures
service ZIP, CPT, billed/allowed/paid amounts. Default collapsed; nudge open if
the user indicates OON.

### 8.2 Chat / appeal display
New `UCRComparisonPanel.tsx` component, surfaced in `chat_interface.tsx` when
`denial.ucr_context` is present:
- Headline: "Your insurer paid $X — independent benchmark suggests $Y."
- Source attribution and effective date.
- "Why this matters" expandable explainer.
- Edit button → opens billing-info modal that POSTs to `set_billing_info`.

### 8.3 Appeal letter preview
No UI change — UCR language is part of the letter body and renders via existing
components.

## 9. Provider-side workflows (deferred / phase 3)

For ProfessionalUser / UserDomain:
- Bulk under-reimbursement audit: scan all denials in a domain, flag those where
  paid < UCR-80th. Surface in the professional dashboard.
- Per-payer report: aggregate under-reimbursement rates by insurer, useful for
  contract negotiation evidence.
- Optional consent flag at the UserDomain level: contribute de-identified
  billed/allowed pairs to the FHI aggregate dataset (`source=fhi_aggregate`).
  Builds the long-term proprietary benchmark.

These should not block phase 1.

## 10. Loaders & operational concerns

### 10.1 Medicare PFS loader

New management command: `fighthealthinsurance/management/commands/load_medicare_pfs.py`.
- Downloads the annual CMS PFS RVU and locality files in parallel
  (`asyncio.gather` over the two independent HTTP fetches).
- Materializes one `UCRRate` row per (HCPCS, locality, effective year) for the
  Medicare-allowed amount, plus one row per percentile in §3.1 by applying the
  multipliers from `UCR_MEDICARE_PERCENTILE_MULTIPLIERS`. Multipliers are flagged
  as derived in `metadata.derived_from = "medicare_pfs"`.
- Idempotent — uses the `unique_together` from §4.1 as upsert key.

### 10.2 Refresh cadence
The polling actor (§10.4) runs a daily outer loop; per-source `_if_due`
predicates do the actual gating:
- Medicare PFS: yearly (predicate compares stored `effective_date.year` to
  current year).
- FAIR Health: weekly or monthly per subscription terms.
- FHI aggregate: weekly recompute over the most recent rolling window.

Per-denial enrichment is triggered when the user edits billing info, when a
source publishes data newer than what the denial's `UCRLookup` snapshot used,
or when `Denial.ucr_refreshed_at` is older than `UCR_DENIAL_STALE_TTL_DAYS` and
the appeal is not yet finalized.

### 10.3 PII / HIPAA
Billed/allowed/paid amounts on a `Denial` are PHI-adjacent in combination with
the denial, so phase 1 stores them as `EncryptedCharField` (the same
`django-encrypted-model-fields` package referenced in CLAUDE.md). Cents are
serialized as decimal strings; a thin helper on `Denial` (or the serializer)
wraps parse/format so callers see ints. Migration plan:
1. New columns are added empty in the schema migration; no backfill needed
   (no existing data — these are new fields).
2. `FIELD_ENCRYPTION_KEY` (or whatever the project's env name is) must be set
   in `Dev`, `Test*`, and `Prod` configuration classes; `.env.example` is
   updated to document it.
3. Round-trip test in `tests/sync/test_ucr_models.py` verifies that an
   integer-valued `set_billed_amount_cents(12345)` reads back as `12345` and
   that the underlying column is not plaintext-readable.

The unauthenticated `/ucr/lookup/` endpoint never sees patient data — just CPT
and ZIP3.

### 10.4 UCR refresh polling actor

Follows the existing polling-actor pattern (`email_polling_actor.py`,
`fax_polling_actor.py`, `chooser_refill_actor.py`) — a long-lived Ray actor that
sleeps and loops, registered from `polling_actor_setup.py` and supervised by
`actor_health_status.relaunch_actors`.

New file: `fighthealthinsurance/ucr_refresh_actor.py`

```python
@ray.remote
class UCRRefreshActor:
    """Periodically refreshes UCR reference data and re-enriches stale denials.

    Two independent loops run concurrently inside one actor; a slow upstream
    source does not stall per-denial refreshes.
    """

    async def run(self):
        # Both loops run forever; if either crashes the actor exits and
        # relaunch_actors restarts it (matches existing actor pattern).
        await asyncio.gather(self.source_refresh_loop(),
                             self.denial_refresh_loop())

    async def refresh_denial(self, denial_id: int):
        """Out-of-cycle trigger called from REST (§7)."""
        denial = await sync_to_async(Denial.objects.get)(id=denial_id)
        await UCREnrichmentHelper.maybe_enrich(denial, force=True)

    async def source_refresh_loop(self):
        while True:
            try:
                # Each predicate decides whether enough time has elapsed for
                # its own source (§10.2). The outer cadence is just the poll.
                advanced_sources = []
                if await self._refresh_medicare_pfs_if_due():
                    advanced_sources.append(UCRSource.MEDICARE_PFS)
                if await self._refresh_fair_health_if_due():     # phase 2 stub
                    advanced_sources.append(UCRSource.FAIR_HEALTH)
                if await self._refresh_fhi_aggregate_if_due():   # phase 3 stub
                    advanced_sources.append(UCRSource.FHI_AGGREGATE)
                # Only fan out denial refreshes for sources that actually changed.
                for src in advanced_sources:
                    await self._mark_denials_using_source_stale(src)
            except Exception:
                logger.exception("UCR source refresh failed")
            base = settings.UCR_SOURCE_REFRESH_INTERVAL_HOURS * 3600
            await asyncio.sleep(random.uniform(base * 0.85, base * 1.15))

    async def denial_refresh_loop(self):
        while True:
            try:
                ttl = settings.UCR_DENIAL_STALE_TTL_DAYS
                batch_size = settings.UCR_DENIAL_REFRESH_BATCH_SIZE
                stale_qs = (Denial.objects
                    .filter(appeal_result__isnull=True)            # not finalized
                    .filter(Q(ucr_refreshed_at__isnull=True) |     # never enriched
                            Q(ucr_refreshed_at__lt=now() - timedelta(days=ttl)))
                    .order_by("id")[:batch_size])
                stale = await sync_to_async(list)(stale_qs)
                # Pre-fetch rates for the (cpt, area) pairs in this batch so each
                # maybe_enrich() call is a dict lookup, not a DB round-trip.
                rate_cache = await UCREnrichmentHelper.bulk_load_rates(stale)
                # return_exceptions=True so one bad denial doesn't cancel the
                # batch and we get per-denial visibility into failures.
                results = await asyncio.gather(*(
                    UCREnrichmentHelper.maybe_enrich(d, force=True, rates=rate_cache)
                    for d in stale
                ), return_exceptions=True)
                for denial, result in zip(stale, results):
                    if isinstance(result, Exception):
                        logger.error("UCR enrich failed for denial_id=%s",
                                     denial.id, exc_info=result)
            except Exception:
                logger.exception("UCR denial refresh loop crashed")
            base = settings.UCR_DENIAL_REFRESH_INTERVAL_MINUTES * 60
            await asyncio.sleep(random.uniform(base * 0.75, base * 1.25))
```

Wiring (mirrors how `epar`/`fpar`/`cpar` are wired today): register a singleton
named `upar` (UCR polling actor reference) in `polling_actor_setup.py`;
`launch_polling_actors.py` picks it up automatically; `actor_health_status`
inherits health monitoring and the `--force` relaunch path. REST handlers in §7
call `upar.refresh_denial.remote(denial_id)` for on-demand work; the polling
loop is the safety net, not the primary path.

Configuration (new settings on `Dev`/`Prod` configuration classes):
- `UCR_SOURCE_REFRESH_INTERVAL_HOURS` (default 24)
- `UCR_DENIAL_REFRESH_INTERVAL_MINUTES` (default 60)
- `UCR_DENIAL_STALE_TTL_DAYS` (default 90)
- `UCR_DENIAL_REFRESH_BATCH_SIZE` (default 50)
- `UCR_MEDICARE_PERCENTILE_MULTIPLIERS` (default `{50: 1.5, 80: 2.0, 90: 2.5}`)

Idempotency & safety:
- `UCRLookup` rows are append-only; `Denial.ucr_context` is repointed to the
  new snapshot. Finalized denials (`appeal_result` set) are skipped so we never
  edit a letter that's already been faxed/mailed.
- Lock contention is avoided by selecting a bounded, `id`-ordered batch.
- Change detection: `maybe_enrich` hashes the computed comparison and short-
  circuits when the hash matches `denial.ucr_context["hash"]` (bumps
  `ucr_refreshed_at` only). Prevents the ~90-day TTL cycle from generating a
  no-op `UCRLookup` insert and `Denial` save when nothing actually changed.

Tests: `tests/sync-actor/test_ucr_refresh_actor.py` under the existing
`py313-django52-sync-actor` tox env. Cover stale-detection (TTL boundary +
NULL `ucr_refreshed_at`), batch bounding via the setting, finalized-denial
skip, source-advanced fan-out, the hash-unchanged short-circuit, and the
per-denial error path: when one denial in a batch raises, the others still
complete and the failure is logged with the offending `denial_id`.

### 10.5 Retention

Both `UCRRate` and `UCRLookup` grow without bound otherwise.

- `UCRRate`: rough upper bound is ~10k HCPCS × ~110 Medicare localities × 3
  percentiles × N years × M sources. Add a yearly pruning step inside the
  source-refresh loop: delete rates where `expires_date < now() - 2 years`
  AND a more recent row exists for the same `(procedure_code, modifier,
  geographic_area, percentile, source)` key. `UCRLookup` snapshots remain
  intact; that's why audit data is decoupled from live rates.
- `UCRLookup`: append-only by design (§10.4) but capped per denial — keep at
  most `UCR_LOOKUP_RETENTION_PER_DENIAL` rows (default 10), trimming oldest
  on insert. The row pointed to by `Denial.latest_ucr_lookup` (§4.2) is always
  preserved regardless of age, so pruning is driven by a durable FK rather than
  inferred from `ucr_context` JSON. When a denial is finalized, prune all
  rows except `Denial.latest_ucr_lookup`.

## 11. Testing strategy

Following the project's `tox -e ...` conventions.

`tests/sync/test_ucr_models.py`
- UCRRate uniqueness, area resolution fallback chain (ZIP3 → state → national).
- UCRLookup snapshot persistence.
- Encrypted billing-amount round-trip (§10.3): integer in → integer out, raw
  column is not plaintext-readable.
- `latest_ucr_lookup` FK is preserved by retention pruning (§10.5).

`tests/sync/test_ucr_helper.py`
- Comparison math: gap calculations, percentile selection, source priority.
- Detection logic: under-reimbursement classifier hits/misses.
- Procedure-code regex extraction.

`tests/async/test_ucr_enrichment.py`
- End-to-end async enrichment of a denial.
- ML mocking for code extraction (mock the ML backend per project conventions).

`tests/sync/test_ucr_rest.py`
- The four endpoints in §7. Auth / rate-limit / serializer round-trip.

`tests/sync/test_load_medicare_pfs.py`
- Loader idempotency on a small fixture CSV; parallel fetch of RVU + locality.
- `UCRRate` retention pruning (§10.5).

Selenium: optional smoke test for the new intake billing section.

## 12. Open questions / decisions needed before phase 2

12.1 **FAIR Health licensing.** What are the terms for a 501(c)(3)? Per-call
API or licensed flatfile? Decision affects whether we keep the static-multiplier
model long term or replace it.

12.2 **Multiplier defaults.** The 1.5/2.0/2.5× Medicare multipliers used as a
stand-in for percentile data should be reviewed by someone with payer-pricing
expertise before they ship in user-facing letters. Phase 1 is gated on this —
until §12.2 closes, the appeal letters cite "Medicare benchmark × N", **not**
"Nth percentile" (see §13).

12.3 **De-identification policy for FHI aggregate.** Need explicit user consent
and a minimum sample size per (CPT, ZIP3) bucket before we publish any aggregate.

12.4 **Anesthesia / time-unit codes.** Defer entirely or partial coverage with
conversion factors? Recommend defer.

12.5 **Display to patients vs. professionals.** Should the UCR comparison appear
to patient users without a professional's review, or only to professional users?
Default proposal: show to patients with a clear "estimate" disclaimer; let
ProfessionalUser disable display per UserDomain if desired.

## 13. Phasing & rough sequencing

Phase 1 — MVP (1–2 sprints)
- Models (§4), migrations, `Denial` field extensions including
  `ucr_refreshed_at`, `latest_ucr_lookup` FK, and `ucr_context`;
  `ucr_constants.py` enums; `EncryptedCharField` for billing amounts (§10.3).
- Medicare PFS loader; retention pruning (§10.5).
- Regex code extraction + manual entry on intake.
- `UCREnrichmentHelper` with multiplier-based benchmarks. Per §12.2, letters
  cite "Medicare × N" wording, **not** "Nth percentile" — the percentile
  framing is unlocked by phase 2 once expert review closes §12.2.
- `ucr_context` injection into appeal prompts.
- New AppealTemplate for under-reimbursement appeals.
- REST: `set_billing_info`, `ucr_context`, `refresh_ucr` (the `/ucr/lookup/`
  public endpoint is phase 2).
- `UCRRefreshActor` scaffold + Medicare branch only — FAIR Health and
  FHI-aggregate branches are stubbed `_if_due` predicates that no-op.
- Frontend: intake billing section + `UCRComparisonPanel`.
- Tests across sync/async/REST/sync-actor.

Phase 2 — Quality
- ML-based code extraction.
- FAIR Health integration (pending §12.1).
- Source-priority logic exercised once we have >1 source.
- Public `/ucr/lookup/` endpoint with caching (§7).
- Per §12.2 closure, switch letter wording to percentile framing.

Phase 3 — Provider workflows
- Bulk audit, per-payer reports, FHI aggregate dataset opt-in.

## 14. Files this plan will touch

New
- `fighthealthinsurance/ucr_constants.py` (UCRSource, UCRAreaKind enums + percentile list)
- `fighthealthinsurance/ucr_helper.py`
- `fighthealthinsurance/ucr_refresh_actor.py`
- `fighthealthinsurance/management/commands/load_medicare_pfs.py`
- `fighthealthinsurance/migrations/000X_add_ucr_models.py`
- `fighthealthinsurance/migrations/000X_denial_financial_fields.py`
- `fighthealthinsurance/migrations/000X_seed_ucr_geographic_areas.py`
- `fighthealthinsurance/static/js/UCRComparisonPanel.tsx`
- `tests/sync/test_ucr_models.py`
- `tests/sync/test_ucr_helper.py`
- `tests/sync/test_ucr_rest.py`
- `tests/sync/test_load_medicare_pfs.py`
- `tests/async/test_ucr_enrichment.py`
- `tests/sync-actor/test_ucr_refresh_actor.py`

Modified
- `fighthealthinsurance/models.py` (new models + Denial field additions, ~1235+)
- `fighthealthinsurance/common_view_logic.py` (DenialCreatorHelper, AppealAssemblyHelper)
- `fighthealthinsurance/rest_views.py` (DenialViewSet @action methods)
- `fighthealthinsurance/rest_serializers.py` (DenialResponseInfoSerializer + new serializers)
- `fighthealthinsurance/forms/questions.py` (optional billing-details questions)
- `fighthealthinsurance/static/js/chat_interface.tsx` (mount the comparison panel)
- `fighthealthinsurance/polling_actor_setup.py` (register `upar` UCRRefreshActor singleton)
- `fighthealthinsurance/actor_health_status.py` (include UCRRefreshActor in relaunch set)
- `fighthealthinsurance/fixtures/initial.yaml` (UCRGeographicArea seed rows + new AppealTemplate)

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

| Phase | Source | Cost | Granularity | Why |
|-------|--------|------|-------------|-----|
| 1 | CMS Medicare Physician Fee Schedule (PFS) | Free | National + locality (MAC) | Public, downloadable, defensible baseline. Insurers commonly pay 100–300% of Medicare; 250% is a defensible benchmark for many specialties. |
| 1 | Static percentile multipliers (50/70/80/90) on Medicare | — | — | Lets us emit "estimated 80th percentile" language even before commercial data lands. Cite multiplier as derived, not measured. |
| 2 | FAIR Health (FH Benchmarks API or licensed flatfiles) | $$ | ZIP3 + percentile | Industry-standard post-Ingenix benchmark; the "name brand" dataset most appeals cite. |
| 2 | Self-reported EOB amounts from FHI users (consented) | Free | Builds over time | Long-term: aggregate de-identified billed/allowed pairs into our own dataset to corroborate or replace commercial data. |
| 3 | Context4 / MultiPlan-comparable datasets | $$ | Variable | Only if we need to rebut insurer-cited data point-for-point. |

The schema below is source-agnostic — every rate row carries a `source` and
`effective_date`, so multiple datasets can coexist and be selected by priority.

## 4. Data model changes

### 4.1 New models (in `fighthealthinsurance/models.py`)

```python
class UCRGeographicArea(models.Model):
    """A geographic billing area. Independent of source so multiple datasets share it."""
    kind = models.CharField(max_length=16, choices=[("zip3", "ZIP3"), ("msa", "MSA"),
                                                    ("state", "State"), ("national", "National"),
                                                    ("medicare_locality", "Medicare Locality")])
    code = models.CharField(max_length=32, db_index=True)  # e.g. "941", "31080", "CA"
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = [("kind", "code")]


class UCRRate(models.Model):
    """A single UCR rate observation. Source-agnostic."""
    procedure_code = models.CharField(max_length=10, db_index=True)  # CPT or HCPCS
    modifier = models.CharField(max_length=4, blank=True, default="")  # 22, 26, TC, etc.
    geographic_area = models.ForeignKey(UCRGeographicArea, on_delete=models.PROTECT)
    percentile = models.PositiveSmallIntegerField()  # 50, 70, 80, 90, 95; 0 = mean
    amount_cents = models.PositiveIntegerField()      # store as cents to avoid float
    source = models.CharField(max_length=32)          # "medicare_pfs", "fair_health", "fhi_aggregate"
    effective_date = models.DateField()
    expires_date = models.DateField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)  # MAC locality, conversion factor, etc.

    class Meta:
        indexes = [
            models.Index(fields=["procedure_code", "geographic_area", "effective_date"]),
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

### 4.2 Extensions to `Denial` (`models.py:1235+`)

Add the missing financial + geographic fields. All optional so they don't break
existing intake.

```python
# Inside Denial
service_zip = models.CharField(max_length=5, blank=True, default="")
procedure_code = models.CharField(max_length=10, blank=True, default="", db_index=True)
procedure_modifier = models.CharField(max_length=4, blank=True, default="")
billed_amount_cents = models.PositiveIntegerField(null=True, blank=True)
allowed_amount_cents = models.PositiveIntegerField(null=True, blank=True)
paid_amount_cents = models.PositiveIntegerField(null=True, blank=True)
ucr_context = models.JSONField(null=True, blank=True)  # parallel to ml_citation_context
```

Why a separate `ucr_context` JSONField rather than reusing `ml_citation_context`:
the citation context is for medical-necessity citations; UCR context is structured
pricing data with a different shape and audit lifecycle. Parallel field, parallel
helper, parallel UI panel.

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

```
DenialCreatorHelper.create_or_update_denial()       # common_view_logic.py:940+
  └─> UCREnrichmentHelper.maybe_enrich(denial)
        ├─ resolve_procedure_code(denial.procedure | denial.verified_procedure)
        │     uses ML — see §5.3
        ├─ resolve_geographic_area(denial.service_zip | denial.your_state)
        │     ZIP3 → ZIP3 area, fall back to state, fall back to national
        ├─ query_rates(code, area, [50, 80, 90])
        │     prefer source priority: fhi_aggregate > fair_health > medicare_pfs
        ├─ build_comparison(billed, allowed, paid, rates)
        │     returns dict: {gap_vs_p80, gap_vs_p90, narrative, citations}
        ├─ persist UCRLookup row (snapshot for audit)
        └─ write to denial.ucr_context and save
```

Runs async (same Ray actor pattern as existing `extract_procedure_diagnosis_finished`).

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

```
[UCR PRICING CONTEXT]
Procedure: 99213 (Office visit, established patient)
Geographic area: ZIP3 941 (San Francisco)
Billed: $250.00
Insurer allowed: $80.00
Insurer paid: $64.00 (after 20% coinsurance)
Independent benchmark (CMS Medicare PFS, 2026, locality 5):
  - Medicare allowed amount: $98.42
  - 250% of Medicare (commercial proxy 80th pct): $246.05
Gap vs. 80th-pct benchmark: $166.05 (67%) under-reimbursed
Source: medicare_pfs effective 2026-01-01
[/UCR PRICING CONTEXT]
```

The model is then instructed (via system prompt update) to cite the gap and the
source explicitly when generating the appeal letter, and to request reprocessing
under the plan's "out-of-network allowable" methodology.

### 6.2 New appeal template

Add a new `AppealTemplates` row (`models.py:490`) keyed for under-reimbursement
appeals, separate from medical-necessity templates. Loaded via fixture.

## 7. REST API surface

Pattern matches existing ViewSet + `@action` style in `rest_views.py`.

```
POST /ziggy/rest/denial/{id}/set_billing_info/
  body: {service_zip, procedure_code, modifier, billed_cents, allowed_cents, paid_cents}
  triggers UCR enrichment async

GET  /ziggy/rest/denial/{id}/ucr_context/
  returns the resolved UCRLookup snapshot + comparison

POST /ziggy/rest/denial/{id}/refresh_ucr/
  re-runs enrichment (after user edits billing info)

GET  /ziggy/rest/ucr/lookup/?cpt=99213&zip=94110&percentiles=50,80,90
  unauthenticated cached lookup endpoint, rate-limited; useful for marketing
  pages and pre-intake "is this worth appealing?" widgets
```

Serializer additions in `rest_serializers.py`:
- Extend `DenialResponseInfoSerializer` with the six new financial/geographic fields.
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
The generated appeal letter already renders via existing components; no UI
change needed since UCR language is part of the letter body. Optionally add a
"UCR section" anchor link.

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
- Downloads the annual CMS PFS RVU and locality files (well-known URLs at cms.gov).
- Materializes one `UCRRate` row per (HCPCS, locality, effective year).
- Computes percentile rows by applying static multipliers (configurable; defaults
  Medicare × 1.5 / 2.0 / 2.5 for p50/p80/p90). Multipliers are flagged as
  derived in `metadata`.
- Idempotent on `(procedure_code, modifier, geographic_area, percentile, source, effective_date)`.

### 10.2 Refresh cadence
Yearly for Medicare (Q1 each year when CMS publishes). On-demand via management
command, not cron, until volume justifies automation.

### 10.3 PII / HIPAA
Billed/allowed/paid amounts are not PHI by themselves but in combination with the
denial they are. Store on `Denial` (already encrypted-field-aware) and treat them
as sensitive. The unauthenticated `/ucr/lookup/` endpoint never sees patient data —
just CPT + ZIP3.

## 11. Testing strategy

Following the project's `tox -e ...` conventions.

`tests/sync/test_ucr_models.py`
- UCRRate uniqueness, area resolution fallback chain (ZIP3 → state → national).
- UCRLookup snapshot persistence.

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
- Loader idempotency on a small fixture CSV.

Selenium: optional smoke test for the new intake billing section.

## 12. Open questions / decisions needed before phase 2

1. **FAIR Health licensing.** What are the terms for a 501(c)(3)? Per-call API or
   licensed flatfile? Decision affects whether we keep the static-multiplier model
   long term or replace it.
2. **Multiplier defaults.** The 1.5/2.0/2.5× Medicare multipliers used as a stand-in
   for percentile data should be reviewed by someone with payer-pricing expertise
   before they ship in user-facing letters. Conservative default: cite as "Medicare
   benchmark × 2.0" rather than as "80th percentile".
3. **De-identification policy for FHI aggregate.** Need explicit user consent and a
   minimum sample size per (CPT, ZIP3) bucket before we publish any aggregate.
4. **Anesthesia / time-unit codes.** Defer entirely or partial coverage with
   conversion factors? Recommend defer.
5. **Display to patients vs. professionals.** Should the UCR comparison appear to
   patient users without a professional's review, or only to professional users?
   Default proposal: show to patients with a clear "estimate" disclaimer; let
   ProfessionalUser disable display per UserDomain if desired.

## 13. Phasing & rough sequencing

Phase 1 — MVP (1–2 sprints)
- Models, migrations, `Denial` field extensions.
- Medicare PFS loader.
- Regex code extraction + manual entry on intake.
- `UCREnrichmentHelper` with multiplier-based percentiles.
- `ucr_context` injection into appeal prompts.
- New AppealTemplate for under-reimbursement appeals.
- REST: `set_billing_info`, `ucr_context`, `refresh_ucr`.
- Frontend: intake billing section + `UCRComparisonPanel`.
- Tests across sync/async/REST.

Phase 2 — Quality
- ML-based code extraction.
- FAIR Health integration (pending §12.1).
- Source-priority logic actually exercised once we have >1 source.
- Public `/ucr/lookup/` endpoint.

Phase 3 — Provider workflows
- Bulk audit, per-payer reports, FHI aggregate dataset opt-in.

## 14. Files this plan will touch

New
- `fighthealthinsurance/ucr_helper.py`
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

Modified
- `fighthealthinsurance/models.py` (new models + Denial field additions, ~1235+)
- `fighthealthinsurance/common_view_logic.py` (DenialCreatorHelper, AppealAssemblyHelper)
- `fighthealthinsurance/rest_views.py` (DenialViewSet @action methods)
- `fighthealthinsurance/rest_serializers.py` (DenialResponseInfoSerializer + new serializers)
- `fighthealthinsurance/forms/questions.py` (optional billing-details questions)
- `fighthealthinsurance/static/js/chat_interface.tsx` (mount the comparison panel)
- `fighthealthinsurance/fixtures/initial.yaml` (UCRGeographicArea seed rows + new AppealTemplate)

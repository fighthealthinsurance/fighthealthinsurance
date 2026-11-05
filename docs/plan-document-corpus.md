# The public plan-document corpus

Appeals read better when they quote the member's own plan document (an
Evidence of Coverage, a Summary of Benefits and Coverage, a member handbook)
instead of general statements about what plans usually cover. Most people
upload nothing with their case, so the app keeps a corpus of plan documents it
fetched itself from public sources, loads it with a bundle importer, and
matches each denial to the documents that appear to be the member's plan. The
relevant, page-labelled passages then go into appeal generation as their own
guarded prompt section.

The corpus holds public documents only, stored as plain text, with no PHI.
Nothing a person uploads ever enters it: those are `PlanDocuments` and
`PolicyDocument` rows, which carry encryption, hashed identifiers and
remove-my-data semantics, and the matcher never reads them. The only patient
data that moves during matching is the denial letter excerpt a tie-break or
search-term prompt carries, and that goes to our own models only
(`infer_with_fallback`'s default internal models; the matcher never passes
`models=`). Logs carry ids, scores and counts, never a keyword phrase, an
employer name, the letter or a document's text.

Code: `fighthealthinsurance/plan_document_bundle.py` (the bundle contract and
the importer), `fighthealthinsurance/management/commands/import_plan_documents.py`
(the command), `fighthealthinsurance/plan_document_matcher.py` (matching, the
passages, the prompt section). The pipeline that writes bundles lives in the
private `healthinsurance-llm-internal` repo; its exporter has to stay in step
with `plan_document_bundle.py`, whose record fields and limits are the
contract.

## What the corpus is, and is not

| Model | What it holds | Who puts it there | How it is stored |
|---|---|---|---|
| `PublicPlanDocument` | A plan document we fetched from a public source | `import_plan_documents`, or a staff member in the admin | Plain text in `[Page N]` blocks; the original file, when kept, in the clear on `COMBINED_STORAGE` |
| `PlanDocuments` | Files a person uploaded with their own appeal | The person | Encrypted |
| `PolicyDocument` | Explain My Policy uploads and consented shares | The person | Encrypted, with a hashed email, consent and removal |

A bundle record must carry a published plan document and nothing else: no
text from a person's upload, no letter, no annotation that could name a
member. The matcher's candidates are `PublicPlanDocument` rows only, and when
a denial has `PlanDocuments` of its own the matched public document is not
rendered at all, because the person's documents govern.

## The two models

`PublicPlanDocument` (`models.py`) is one fetched document: its identity
(`document_key`, `insurance_company`, an optional `insurance_plan`, `title`,
`document_type`, `coverage_kind`, `line_of_business`), the matching columns
(`state`, `region`, `employer_name`, `plan_year`, `plan_id_prefixes`,
`match_keywords`, `match_regex`, `negative_regex`), provenance (`source_url`,
`fetched_at`, `source_sha256`, `pipeline_version`, `import_batch`, `notes`),
the text (`text`, `text_sha256`, `page_count`, `char_count`, `stored_file`)
and `is_active`. `citation_name()` is the title, with the plan year appended
when the title does not already carry it; that is how a passage is cited.
Only active documents are matched. The model is in `STORED_FILE_MODELS`, so
deleting a row removes its stored file.

`PlanDocumentMatch` is one document scored for one denial, unique per
(denial, document):

| Field | Meaning |
|---|---|
| `denial`, `document`, `company` | The denial, the candidate document, and the carrier the row was computed for |
| `score`, `signals`, `rank` | The points; a JSON object of points per signal plus `keyword_hits` (a count) and `identifying` (how many identifying signals fired); the position among the denial's candidates, 1 being best |
| `selected` | Whether this document's text goes to the appeal |
| `method` | `rules`, or `model` when a tie-break chose it |
| `relevant_text`, `summary` | The page-labelled passages and their model-written summary, on the rank-1 selected row only |
| `inputs_hash` | What the selection was computed from; a row whose hash no longer matches the denial is stale and is never read |
| `relevance_key` | The procedure and diagnosis the passages were built for |

Every candidate that survives the hard excludes is stored, selected or not,
up to `MAX_STORED` (10) per denial, so staff can see why a document was or
was not chosen.

## The bundle contract

A bundle is a JSON array of records, an object
`{"bundle_version": 1, "documents": [...]}`, or JSON Lines with one record per
line. Problems with the bundle as a whole are refused before anything is
written: more than `MAX_BUNDLE_BYTES` (256 MiB) of UTF-8 text, more than
`MAX_DOCUMENTS` (5,000) records, a JSON object that gives a field twice, a
record that is not an object, or a `bundle_version` other than 1.

A record is one document. A record with a field outside the table is refused.
The limits are the constants in `plan_document_bundle.py`.

| Field | Type | Required | Meaning and limits |
|---|---|---|---|
| `document_key` | string matching `[A-Za-z0-9._-]{1,128}` | yes | The pipeline's stable id and the upsert key: a re-import with the same key updates the row |
| `insurer` | string, at most 300 characters | yes, unless `insurance_company_id` is given | Resolved to an `InsuranceCompany` by `pa_requirements.resolve_insurance_company_by_name`: the exact name (case-insensitive), then an exact alt-name line, then the company's own regex applied to the name. No match refuses the record |
| `insurance_company_id` | integer | no | The carrier's primary key; wins over `insurer`; must exist |
| `plan_name` | string, at most 300 | no | An existing `InsurancePlan` of that carrier, by name (case-insensitive): the row for `plan_state` when given, else the row for the document's `state`, else the national row (blank state), else the only plan of that name. Not found: a warning, and the plan is left blank. No plan rows are created |
| `plan_state` | two letters | no | Narrows `plan_name` to one state's row |
| `title` | string, at most `MAX_TITLE` (500) | yes | |
| `document_type` | one of `evidence_of_coverage`, `summary_of_benefits`, `summary_plan_description`, `member_handbook`, `certificate_of_coverage`, `formulary`, `medical_policy`, `other` | yes | |
| `coverage_kind` | one of `medical`, `dental`, `vision`, `pharmacy`, `behavioral`, `other` | no, default `medical` | |
| `line_of_business` | one of `commercial`, `medicare_advantage`, `medicaid`, `exchange`, `dsnp`, `other`, `all` | no, default `all` | `all` and `other` both mean "not specific to one line" |
| `state` | two letters, or blank | no | Upper-cased; blank means national |
| `region` | string, at most `MAX_REGION` (120) | no | The carrier's region or service area, for example Northern California |
| `employer_name` | string, at most `MAX_EMPLOYER` (300) | no | Only for a document specific to one employer's plan |
| `plan_year` | integer from 2000 to 2100 | no | |
| `plan_id_prefixes` | list of strings | no | Prefixes of the member or group ids the plan issues. Whitespace is collapsed, the text upper-cased, duplicates and empties dropped |
| `match_keywords` | list of strings | no | Phrases that place a letter with this document. Whitespace is collapsed, the text lower-cased, duplicates and empties dropped. Matched as whole phrases (below) |
| `match_regex` | string, at most `MAX_REGEX` (800) | no | Must compile (case-insensitive, Unicode, multiline) |
| `negative_regex` | string, at most 800 | no | Must compile. A pattern that matches the probe `NEGATIVE_REGEX_PROBE` (`medical necessity medically necessary medical center`) draws a warning, and refuses the record under `--strict` |
| `source_url` | string, at most `MAX_URL` (2000) | no | Must start with `http://` or `https://` |
| `fetched_at` | ISO 8601 datetime string | no | A naive time is taken as UTC |
| `source_sha256` | 64 hex characters | no | SHA-256 of the original file. Computed from the bytes when a `file` record leaves it out |
| `pages` | non-empty list of strings, or of `{"page": N, "text": "..."}` objects | exactly one of `pages`, `text` and `file` | At most `MAX_PAGES` (2,000). Plain strings are numbered from 1; an object's `page` must be a whole number of 1 or more. Blank pages are dropped, and a record whose pages are all blank is refused |
| `text` | non-empty string | one of the three | Kept as is when it has lines that read exactly `[Page N]`; otherwise wrapped as page 1 |
| `file` | relative path ending in `.pdf`, `.docx` or `.txt` | one of the three | Read under `--files-root`; an absolute path, or one that leaves the root, is refused. At most `MAX_FILE_BYTES` (50 MiB). Parsed with `ml_document_extraction.extract_text_from_bytes`, the readers the appeal flow uses, into page blocks. A file that yields no text (a scanned PDF) is refused |
| `is_active` | boolean | no, default `true` | |
| `pipeline_version` | string, at most `MAX_VERSION` (64) | no | |
| `notes` | string | no | Free text; anything but a string is stored as blank |

Whichever way the text arrives, it is stored as `[Page N]` blocks joined by
blank lines (the shape `MLPlanDocHelper`'s passage selection reads, so a
quoted passage can name its page), and may hold at most `MAX_TEXT_CHARS`
(3,000,000 characters). The row's `text_sha256`, `page_count` and
`char_count` are computed from it, and `import_batch` is the run's label.

### A worked example: three Kaiser Permanente records

The first record carries its pages inline, the second points at a file, the
third carries page-marked text. In JSON the regex backslashes are doubled.

```json
{
  "bundle_version": 1,
  "documents": [
    {
      "document_key": "kaiser-norcal-eoc-2025-hmo",
      "insurer": "Kaiser Permanente",
      "title": "Kaiser Permanente Northern California 2025 HMO Evidence of Coverage",
      "document_type": "evidence_of_coverage",
      "coverage_kind": "medical",
      "line_of_business": "commercial",
      "state": "CA",
      "region": "Northern California",
      "plan_year": 2025,
      "match_keywords": ["northern california", "san francisco", "sacramento",
                         "san jose", "santa clara", "walnut creek"],
      "negative_regex": "\\bsouthern\\s+california\\b|\\blos\\s+angeles\\b|\\bsenior\\s+advantage\\b|\\bmedi[-\\s]cal\\b",
      "source_url": "https://example.com/plan-documents/norcal-eoc-2025.pdf",
      "fetched_at": "2025-01-15T08:00:00Z",
      "pipeline_version": "plan-docs-0.3",
      "pages": [
        {"page": 1, "text": "Evidence of Coverage ..."},
        {"page": 2, "text": "..."}
      ]
    },
    {
      "document_key": "kaiser-norcal-sbc-2025-hmo",
      "insurer": "Kaiser Permanente",
      "title": "Kaiser Permanente Northern California 2025 HMO Summary of Benefits and Coverage",
      "document_type": "summary_of_benefits",
      "coverage_kind": "medical",
      "line_of_business": "commercial",
      "state": "CA",
      "region": "Northern California",
      "plan_year": 2025,
      "match_keywords": ["northern california", "san francisco"],
      "negative_regex": "\\bsouthern\\s+california\\b|\\blos\\s+angeles\\b|\\bsenior\\s+advantage\\b",
      "source_url": "https://example.com/plan-documents/norcal-sbc-2025.pdf",
      "file": "kaiser/norcal-sbc-2025.pdf"
    },
    {
      "document_key": "kaiser-senior-advantage-eoc-2025",
      "insurer": "Kaiser Permanente",
      "plan_name": "Medicare Advantage",
      "title": "Kaiser Permanente Senior Advantage 2025 Evidence of Coverage",
      "document_type": "evidence_of_coverage",
      "coverage_kind": "medical",
      "line_of_business": "medicare_advantage",
      "state": "CA",
      "plan_year": 2025,
      "match_keywords": ["senior advantage", "medicare"],
      "text": "[Page 1]\nEvidence of Coverage ...\n\n[Page 2]\n..."
    }
  ]
}
```

The third record's `plan_name` resolves only when the carrier already has an
`InsurancePlan` named "Medicare Advantage"; otherwise the import warns and
leaves the plan blank, and the document still matches on its line of business
and keywords.

## Producing bundles in llm-internal

**Stable keys.** `document_key` is the document's identity for every later
import: one key per document and plan year (`kaiser-norcal-eoc-2025-hmo`),
never derived from a URL or a file hash that changes when the file is fetched
again. A changed key creates a second row and leaves the old one active
unless the run passes `--deactivate-missing`.

**Text inline for piped imports.** A bundle piped into a pod with
`kubectl exec -i` carries nothing but itself, so its records use `pages` (the
pipeline's usual shape) or `text`. A `file` record is for a run where the
files were copied in beside the bundle and `--files-root` names their
directory; a bundle with `file` records and no `--files-root` is refused
before anything is written, naming the records.

**Region, and the other region's names.** The region lives on the document
(`region`) and in its keywords: the region's name and the cities and medical
centers that place a letter there. Put the *other* regions' names in
`negative_regex`, so a letter from Southern California is excluded from the
Northern California document rather than merely scored lower; each regional
document's negative regex is the mirror of the others' keywords. Product
names that mean another plan go the same way (`senior advantage` on the
commercial EOC's negative regex, and `line_of_business` set on the Medicare
Advantage document). This is what makes the match fail closed: a letter that
names both San Francisco and Los Angeles hits both negative regexes and
matches nothing.

**Keywords are whole phrases.** `document_grep.whole_phrase_pattern` matches
a keyword case-insensitively, lets any run of whitespace stand for a space,
and requires a non-alphanumeric character (or the end of the text) on both
sides, so `target` does not match "targeted therapy" and `san jose` does not
match "San Joseph". Write keywords as the words letters use, and list
variants as separate phrases rather than stems.

**The letterhead and address pitfall.** Keywords and regexes are searched in
the letter together with the typed insurer, employer, plan id and plan
context. Carrier letterhead, the carrier's headquarters city (Oakland is
Kaiser's), grievance and appeals addresses, P.O. boxes and member-services
numbers appear on every letter of the carrier, in every region. A keyword
that names one makes every letter look like a match; a negative regex that
names one rules the document out of every letter. The same goes for words
such as "member", "plan" or "medical".

**The `medi-?cal` pitfall and the import's probe.** `medi-?cal` makes the
hyphen optional, so it matches "medical", which every denial letter contains;
write `\bmedi[-\s]cal\b`, which requires a hyphen or a space. The import runs
each negative regex against `NEGATIVE_REGEX_PROBE` (`medical necessity
medically necessary medical center`) and warns when it matches, or refuses the
record under `--strict`. The probe catches only this case; check new keyword
and regex lists against a few made-up letters before shipping them.

**Per-carrier bundles and `--deactivate-missing`.** Write one bundle per
carrier holding that carrier's complete current set. `--deactivate-missing`
retires the active documents of the carriers the bundle names whose keys it
does not carry, and touches no other carrier, so a Kaiser bundle never
retires an Aetna row; a partial bundle with the flag would retire the rest of
the carrier's documents. Without the flag nothing is retired: a superseded
document stays active until it is deactivated in the admin or sent again with
`"is_active": false`. Two plan years of one product within 15 points of each
other are rivals that only the tie-break can separate, so retire last year's
document when this year's arrives unless you want the model to decide.

## Running the import

Run it with `--dry-run` first: it validates and resolves every record, prints
what would be skipped and why, and rolls back.

```bash
# From a file, writing nothing
python manage.py import_plan_documents --source bundle.json --dry-run

# For real, with a label for the rows this run writes
python manage.py import_plan_documents --source bundle.json --batch kaiser-2025-01

# Into a pod from stdin: the bundle never lands on the pod's disk
kubectl exec -i <pod> -- python manage.py import_plan_documents --source - --dry-run < bundle.json
kubectl exec -i <pod> -- python manage.py import_plan_documents --source - < bundle.json

# Records that name files: copy the files in first, then name their directory
kubectl cp ./plan-docs <pod>:/tmp/plan-docs
kubectl exec <pod> -- python manage.py import_plan_documents \
    --source /tmp/plan-docs/bundle.json --files-root /tmp/plan-docs --store-file
```

| Option | Meaning |
|---|---|
| `--source PATH`, `-s PATH` | The bundle file, or `-` (the default) for stdin |
| `--format auto\|json\|jsonl` | `auto` (the default) reads a `.jsonl` source, or text that does not start with `[` or `{`, as JSON Lines, and anything else as JSON |
| `--files-root DIR` | The directory the records' relative `file` paths are read under. Required when any record names a `file` |
| `--store-file` | Keep the original of each `file` record on storage, named `<document_key><extension>` (the storage adds a token of its own to the name). A re-import with the same bytes and extension leaves the kept file alone; different bytes replace it |
| `--batch LABEL` | Stored as `import_batch` on every row the run writes; default a UTC timestamp such as `20250115T080000Z` |
| `--deactivate-missing` | Retire the named carriers' active documents the bundle does not list |
| `--dry-run` | Do everything inside the transaction, then roll it back |
| `--strict` | Refuse the whole bundle, writing nothing, on the first bad record or the first warning |

The command prints `Loaded N record(s) from '<source>'`, one
`Skipped record <index> (<key>): <reason>` line on stderr per refused record
(the reason names the field, never the text), one
`Record <index> (<key>): <warning>` line per warning (an unresolved
`plan_name`, a negative regex that matches the probe), and the summary
`Plan document import done: created=.. updated=.. unchanged=.. skipped=..
deactivated=.. dry_run=..` (`deactivated=n/a` without `--deactivate-missing`).
The summary is styled as a success, as a warning when anything was skipped or
warned about, and as an error when records were skipped and nothing was
written or unchanged; the exit status is 0 in all three cases, so read the
counts. A bundle that cannot be read, a `file` record without `--files-root`,
and a `--strict` refusal are `CommandError`s with a non-zero exit. Nothing the
command prints contains a document's text or pages.

Per record, the outcome is `created`, `updated` (some field other than
`import_batch` differs from the row, or a file was stored) or `unchanged`.
An update saves the row, which moves its `updated_at` and so the carrier's
corpus stamp (see Staleness below); a changed text also deletes that
document's `PlanDocumentMatch` rows, since the passages built from the old
text no longer quote it. An unchanged record saves nothing. Everything runs
in one transaction. A dry run writes no files, and a run that `--strict`
abandons removes the files it had written, so a rolled-back run leaves
storage as it found it.

## Matching

### When it runs

- **At extraction**, as the `plan_document_corpus` step
  (`DenialCreatorHelper.match_public_plan_documents`), registered in
  `_extract_entity_body` only while the flag is on, within
  `MLPlanDocHelper.TIMEOUT_SECONDS` (60 s). The step reports `found`,
  `nothing_found` or `failed` like the other extraction tasks. It resolves the
  carrier itself, with the matcher's resolver, because the insurer and plan
  steps run beside it and the row's carrier columns may still be empty.
- **At appeal time**, in `AppealsBackendHelper._generate_appeals_body`'s
  research gather, appended after the NICE slot while the flag is on
  (substep `plan_documents`, 40 s). This brings the rows up to date when a
  carrier or plan was corrected on the review page, and covers a denial whose
  extraction skipped the step.
- `make_appeals` then reads the rows (next section). The chat and the
  speculative precompute call `make_appeals` too, so they get the section
  when current rows exist, but they do not run the refresh.
- When the letter is replaced, `_invalidate_denial_text_artifacts` deletes
  the denial's rows, and a refresh still running for the old letter finds the
  row changed and writes nothing.

### The carrier

Candidates come from one carrier only. `resolve_carrier(denial)` takes, in
order:

1. the structured company on the row (`insurance_company_obj`);
2. else the insurer the person typed (`insurance_company`), resolved the
   strict way through `resolve_company_from_text`: exact name, exact alt-name
   line, then the company's regex applied to the typed name;
3. else the carriers' own regexes over the letter (`InsuranceCompany.regex`,
   with `negative_regex` as a veto), accepted only when exactly one carrier
   matches, and refused when the typed insurer disagrees with it
   (`DenialCreatorHelper._names_disagree`: neither name contains the other).
   The person's words outrank the letter, so a typed "Acme Health" beside a
   Kaiser letter resolves nothing.

No carrier, no match and no rows, ever: a document is never attached to
another insurer's denial.

### What the matcher reads from the denial

`denial_facts` reads the denial once:

| Fact | Source |
|---|---|
| State | `denial.state`, else `denial.your_state`; a code or a state's name, normalised to the two-letter code |
| Line of business, confirmed | The denial's plan sources, when exactly one line follows from them: Medicare Advantage; Medicaid; State Marketplace / Affordable Care Act (exchange); Employer -- State Government, Employer -- Federal Government, Employer -- Other Government, Employer -- Private, Union and Other Group (commercial). Medicare Regular, Veterans Affairs, Other and Don't know say nothing |
| Line of business, inferred | Otherwise `pa_requirements.infer_line_of_business` over the letter and plan fields; a hint, never grounds to exclude, since its patterns are loose |
| Dental | `denial.dental_claim`, or the words dental, dentist or orthodont- in the letter |
| Plan | `denial.insurance_plan_obj` |
| Plan id text | `denial.plan_id`, upper-cased for prefix matching |
| Employer | `denial.employer_name` |
| Year | The year of `denial.denial_date`, else the first `20xx` in `date_of_service` |
| The blob | `denial_text`, `insurance_company`, `employer_name`, `plan_id` and `plan_context` joined (`collect_denial_text`); keywords and regexes are searched in it |
| Procedure, diagnosis | `value_or_candidate`; they shape the passages, never the selection |

### Hard excludes

`score_candidate` returns nothing for these, and nothing is stored:

| Rule | A document is out when |
|---|---|
| E1 carrier | It belongs to another carrier: the candidate query is the resolved carrier's documents only |
| E2 inactive | `is_active` is false (also the candidate query) |
| E3 state | Its `state` and the denial's state are both set and differ |
| E4 line of business | Its line is specific (not `all` or `other`), the denial's line is confirmed by a plan source, and they differ. A line inferred from the letter never excludes |
| E5 coverage kind | It is a dental document and the claim is not dental, or the claim is dental and it is not a dental document |
| E6 negative regex | Its `negative_regex` matches the blob |
| E7 plan | Its `insurance_plan` and the denial's plan are both set and differ |

### Scoring

The surviving candidates are scored. Identifying signals say *this* plan;
the rest only agree with it.

| Signal | Identifying | Points | Fires when |
|---|---|---|---|
| `same_plan` | yes | 40 | The document's plan and the denial's plan are both set and the same |
| `plan_id_prefix` | yes | 30 | The denial's plan id, upper-cased, starts with one of the document's prefixes |
| `employer` | yes | 30 | The document's employer name is found as a whole phrase in the denial's employer field; or, when the name has two or more words or six or more characters, anywhere in the blob |
| `match_regex` | yes | 25 | The document's `match_regex` matches the blob |
| `keyword` | yes | 10 per distinct phrase found, at most 30 | A `match_keywords` phrase is found as a whole phrase in the blob. The row stores the count as `keyword_hits`, never the phrases |
| `state` | no | 10 | Both set and equal |
| `line_of_business` | no | 10, or -10 | The document's line is specific and the denial's line is known: 10 when they agree, -10 when they differ and the denial's line was only inferred from the letter (a confirmed line that differs is E4) |
| `plan_year` | no | 10, or 5 | Equal to the denial's year, or one year off |
| `document_type` | no | 6 to 0 | Evidence of Coverage 6; Certificate of Coverage and Summary Plan Description 5; member handbook 3; Summary of Benefits and formulary 2; medical policy 1; other 0 |
| `coverage_kind` | no | 3 | The document's kind agrees with the claim: dental on a dental claim, any other kind on a non-dental claim (always, once E5 has passed) |

A document is **selectable** only with a score of at least `MIN_SCORE` (35)
*and* at least one identifying signal. The agreeing signals alone cannot
select, so a corpus holding only the Kaiser Northern California EOC is not
attached to every Kaiser letter.

The arithmetic the thresholds are set around: one region keyword (10) plus
the state (10), the plan year (10), an EOC (6) and a medical document on a
medical claim (3) is 39, which selects. The same keyword without the state
and the year is 19, which does not. Two keywords without state and year is
29: still nothing.

Against the example bundle plus a Southern California EOC (keywords
`southern california`, `los angeles`, `san diego`; negative regex naming
Northern California and San Francisco) and a Kaiser dental EOC, the made-up
letter "Kaiser Permanente San Francisco Medical Center denied an MRI as not
medically necessary. Northern California Region." with the state CA and a
2025 denial date gives: the Northern California EOC 49 (keyword 20 for two
hits, state 10, plan year 10, document type 6, coverage kind 3) and its SBC 45,
both selected; the Southern California EOC excluded by E6; the dental EOC
excluded by E5; the Senior Advantage EOC scored 29 with no identifying signal,
stored as a candidate and not selected.

### Selection, the margin and the tie-break

`select` orders the selectable candidates by score, then identifying count,
then document type (an EOC before its SBC), then id. The first is the top and
is selected. Each later one that conflicts with a selected document is a
**rival** when the top leads it by less than `MARGIN` (15 points), and is
skipped when the margin holds; one that conflicts with nothing selected rides
along, up to `MAX_SELECTED` (3), which is how a plan's SBC sits beside its
EOC. Two documents conflict (`conflicts`) when their plan, state, region,
plan year or employer are both set and differ, or both have a specific line
of business and the lines differ.

Any rival means the rules select nothing. The top and its rivals go to
`tie_break`: a prompt listing them as numbered lines (title, plan, region,
state or national, year, line of business, employer) over the first 2,000
characters of the letter, asking for the number of the member's plan or 0,
answered by `infer_with_fallback` at temperature 0 with a 20 s timeout over
at most 2 of the internal general-purpose models (`models=` is never passed,
so the excerpt stays with our models; the call is counted as the
`entity_extraction` task). Only a whole number from 0 to the count is
accepted. A pick and the candidates that do not conflict with it are selected
with `method = "model"`; 0, an invalid answer or no answer selects nothing,
and the rows are stored unselected. Fail closed.

The tie-break fires only for conflicts the regexes do not settle: two plan
years of one product, an HMO and a Deductible HMO of the same region, two
employers' SPDs. A region conflict encoded as negative regexes never reaches
it: a letter naming San Francisco and Los Angeles hits both regional
documents' negative regexes, both are hard-excluded before scoring, and
nothing is selected without a model call.

### Relevant passages

`arefresh_match` stores the rows first and builds the text second, so a
timeout keeps the selection. `build_relevance` quotes the selected documents
whole when `MLPlanDocHelper.whole_text` fits `PASSAGE_BUDGET_CHARS` (4,500
characters); otherwise `MLPlanDocHelper.generate_search_terms` asks a model
for search strings (the model-free fallback terms when it fails) and
`relevant_passages(documents, terms, budget_chars=4500)` picks the passages,
each labelled `[<citation name>, page P]`. `summarize_relevant_sections`
writes the summary from exactly that text, cut at `SUMMARY_MAX_CHARS`
(1,500). Both land on the rank-1 selected row with the `relevance_key` they
were built for.

### Staleness: `inputs_hash` and `relevance_key`

Every row carries `inputs_hash`, a SHA-256 over what the selection depends
on: the letter (its own hash), the carrier, the state, whether the claim is
dental, and the carrier's corpus stamp (`corpus_stamp`: how many of its
documents are active and the latest `updated_at` among them). The plan, the
plan id text, the employer and the line of business join the hash only when
some document of the carrier keys on them (has a plan, prefixes, an employer
name, a specific line), so a plan id written later by a sibling extraction
step does not stale the rows of a corpus that never looks at it. Procedure
and diagnosis are not in it; they form `relevance_key`, which only rebuilds
the text.

A refresh that finds rows with the current hash keeps them: if none is
selected it returns at once (the denial was already judged against this
letter and corpus), and if the rank-1 selected row's `relevance_key` differs
it rebuilds the passages and summary only. Otherwise the documents are scored
again and the rows replaced.

| Change | Effect |
|---|---|
| The letter is replaced | The rows are deleted, and the new letter is matched afresh |
| The carrier changes (a correction on the review page, a typed insurer) | A new hash: re-matched at the next refresh |
| The state, or whether the claim is dental, changes | A new hash |
| The carrier's corpus changes: an import creates or updates one of its documents, a document is activated or deactivated in the admin, `--deactivate-missing` retires one | The stamp moves: every denial of that carrier is re-matched at its next refresh |
| The plan, plan id, employer or line of business changes | A new hash only when a document of the carrier keys on that fact |
| The procedure or diagnosis changes | `relevance_key` differs: the passages and summary are rebuilt, the selection kept |
| A re-import that changes nothing | Nothing: the row is not saved and `updated_at` stays |
| A refresh timed out before the text was built | `relevance_key` is blank, so the next refresh builds the text and keeps the selection |

### What the reader returns

`get_plan_document_context_for_denial(denial)` is what `make_appeals` calls.
It returns the section, or `""` when: the denial is not a saved `Denial`;
the person uploaded plan documents of their own (`PlanDocuments` rows exist
for the denial); no carrier resolves; no selected row carries the current
`inputs_hash` with an active document; or the rank-1 selected row has no
passages yet. Rows are read in rank order with `document__is_active=True`, so
deactivating a document stops its text at once.

### What is logged

`match_documents` logs, at debug, the denial id, the carrier id and four
counts: candidates, scored, selected, rivals. A failed refresh logs a warning
with the denial id and the exception's class name. The carrier resolver logs
ids and counts. The signals stored on each row are points per signal, the
keyword hit count and the identifying count. The import logs its five counts.
Nothing logs a keyword phrase, an employer name, the letter or a document's
text: a city beside a denial id is location data.

## The prompt section

`format_plan_document_context(rows)` renders the selected rows, in rank
order, as:

```
<PLAN_DOCUMENT_GUARD>
Matched document(s):
- <citation name> -- <document type>, <state or national>, <region>, <source URL>
Relevant passages (page-labelled):
<relevant_text, at most PASSAGE_BUDGET_CHARS>
Summary of these passages (written by a model; only the passages above may be quoted):
<summary, at most SUMMARY_MAX_CHARS>
END OF PLAN DOCUMENT EXCERPTS.
```

Blank parts of a document line are left out, the passages and summary lines
appear only when there is text for them, and the body is cut at
`CONTEXT_MAX_CHARS` (6,000) less the closing marker, so the marker always
closes the block; a trailing newline keeps whatever the prompt adds next
outside it. The passages come before the summary so a cut keeps the quotable
part.

The guard, `PLAN_DOCUMENT_GUARD` in `plan_document_matcher.py`, reads:

> PLAN DOCUMENT EXCERPTS (matched automatically from public documents): These
> passages come from published plan documents that appear to be this
> member's plan, matched automatically by insurer, region, plan and year. The
> match may be wrong and the plan year or product may differ. They are NOT
> documents the patient supplied: the PLAN DETAILS the patient gave take
> precedence. Quote only the passages below, attribute them as "the published
> \<document title\>" with the page, and do not assert any term of the plan
> that is not quoted here; if the appeal turns on a provision not quoted, say
> the member's plan documents should be checked.

and the closing marker, `PLAN_DOCUMENT_END`, is `END OF PLAN DOCUMENT
EXCERPTS.`

`make_appeals(..., plan_document_context=None)` builds the section itself
from the stored rows when the caller passes nothing and the flag is on (a
failure there costs the section, never the appeal); `""` sends none, and a
string is rendered as given. Both prompt builders, `make_open_prompt` and
`make_sectioned_open_prompt`, place it right after the payer-policy block,
whose last sentence tells the model to defer to the patient's own plan
documents and must not be read as pointing at these automatically matched
excerpts. Under prompt shedding the section is cut to
`_PLAN_DOCUMENT_CONTEXT_TIER2_CAP` (3,500 characters) at tier 2, like
`plan_context`, and is never dropped at tier 1: it is the member's own plan
and outranks the comparative enrichment.

## The flag and the Experiments page

`PLAN_DOCUMENT_MATCHING_ENABLED` is read from the environment by
`settings._env_flag` (`1`, `true`, `yes` or `on` turn it on) and is off by
default, with no Dev or Test override. `plan_document_matcher.enabled()` is
the one place that reads it. It gates the extraction step, the appeal-time
refresh and the prompt section. The importer, the admin and the staff text
view work without it, so a corpus can be loaded and reviewed before any
letter changes.

The staff Experiments page, `/timbit/help/experiments`, lists it as "Plan
document matching" with `plan_document_matcher.enabled` as its live check
(`experiments.py`). `scripts/run_local.sh` lists it in `EXPERIMENTAL_FLAGS`,
so `--all-experiments` turns it on locally.

The test suites pin it off: `tests/sync/conftest.py` sets it to `False` in
its autouse `override_settings`, and `tests/async-unit/conftest.py` scrubs
the variable from the environment, since tox passes the environment through.
A test that needs the behaviour uses
`override_settings(PLAN_DOCUMENT_MATCHING_ENABLED=True)`.

## Admin and the staff text view

**Public plan documents**
(`/timbit/admin/fighthealthinsurance/publicplandocument/`,
`PublicPlanDocumentAdmin`). The list shows key, carrier, title, type,
coverage kind, line of business, state, region, plan year, page count, active
and updated; it filters on carrier, type, coverage kind, line, state, active
and plan year, and searches key, title, region, employer and source URL. The
form has four groups: identity, Matching (the state, region, employer, plan
year, prefixes, keywords and the two regexes, with a reminder to keep them
away from letterhead and grievance addresses), Source, and Text. The `text`
column is never on the form. The Text group's `upload` field takes a PDF,
Word or text file (`validate_upload_size` against the importer's 50 MiB cap,
then `validate_policy_document`, whose own cap of 20 MiB and PDF and DOCX
magic-byte checks apply, so 20 MiB is the limit here); on save
`page_marked_text_from_bytes` parses it the way the importer parses a `file`
record, replaces the text, hashes and counts, and sets `import_batch` to
`admin`. Saving an upload onto an existing row deletes that document's match
rows. "Read the text" links to the staff text view. The actions **Activate**
and **Deactivate** set `is_active` and move `updated_at`, so the carrier's
corpus stamp changes and its denials are matched again.

**Plan document matches**
(`/timbit/admin/fighthealthinsurance/plandocumentmatch/`,
`PlanDocumentMatchAdmin`). Read-only: no add and no change. The list shows
id, denial id, document, score, rank, selected, method and created; it
filters on selected, method and the document's carrier, and searches the
denial id, the document title and the document key. The detail page shows the
signals, the passages, the summary, the hash and the relevance key.

**The text view** (`/timbit/help/plan_document_corpus/<pk>/text`, URL name
`staff_plan_document_corpus_text`, `staff_views.PublicPlanDocumentTextView`)
is behind `staff_member_required` and serves the stored text as
`text/plain` with `Content-Security-Policy: default-src 'none'; sandbox`, a
header naming the document, its page and character counts and when it was
updated, and the first 3,000,000 characters.

## Operations

**Re-import a carrier.** Run the new bundle with `--dry-run`, read the
skipped and warning lines, then run it for real (with `--deactivate-missing`
when the bundle is the carrier's complete set). Rows with the same key are
updated in place; `unchanged` rows are not touched. Every updated row moves
the corpus stamp, so the carrier's denials are re-matched at their next
extraction or appeal-time refresh; a document whose text changed also loses
its existing match rows.

**Deactivate a wrong document.** Select it in the admin and run Deactivate,
or send it again with `"is_active": false`, or leave it out of a full bundle
run with `--deactivate-missing`. The reader stops rendering it at once
(`document__is_active=True`), and the stamp change re-matches the carrier's
denials on their next refresh, so a companion document that was riding along
can be re-evaluated. Deleting the row removes its stored file and, through
the cascade, its match rows.

**Inspect a denial's rows.** Search the Plan document matches admin by the
denial id: the rank, score and signals say why each candidate stood where it
did, `selected` and `method` say what was quoted and whether a model chose
it, and `inputs_hash` says what the rows were computed from. In a
`manage.py shell`, `plan_document_matcher.current_hashes(denial)` gives the
hash and relevance key the rows must carry to be current,
`match_documents(denial)` scores the carrier's documents again without
writing anything, and `get_plan_document_context_for_denial(denial)` shows
the section as the prompt will carry it (empty when the person uploaded
documents, the rows are stale, or the passages are not built). The outcome's
`facts.blob` is the letter: do not paste it into a ticket or a log.

**Turn the flag off.** Remove `PLAN_DOCUMENT_MATCHING_ENABLED` from the
environment and restart: no extraction frame, no appeal-time refresh, no
prompt section. The rows stay where they are and are neither refreshed nor
read until the flag comes back; the importer and the admin keep working.

## Not built yet

- **A confirmation step.** Showing the match to the person on the
  plan-documents step ("Is this your plan?") with accept and reject; today
  the guard text in the prompt is the only check on a wrong match.
- **A bundle-upload page.** A superuser page under `/timbit/help/` that takes
  a bundle and runs it through `prepare_document_fields`; bulk loads go
  through the command piped into a pod.
- **Regional Kaiser plan rows.** Region lives on the document, not on
  `InsurancePlan`; new plan regexes would change `match_insurance_plan_from_regex`
  and fax routing for every Kaiser denial.
- **A chat tool over the corpus.** No MCP or chat tool searches the corpus,
  and there is no full-text or trigram search over `text`.
- **Token auth for a push endpoint.** The REST API is session-authenticated
  only, so the pipeline cannot push a bundle; it is piped in.
- **The refresh in the chat research path.** The chat and the speculative
  precompute read the rows the wizard's extraction and appeal steps leave
  behind, and do not run `arefresh_match` themselves.

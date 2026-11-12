# API keys setup

Fight Health Insurance calls several external services for evidence and
logging. This guide covers where to get each key, what it unlocks, and how to
wire it up locally and in CI.

Model backend keys (`ANTHROPIC_API_KEY`, `DEEPINFRA_API`, `PERPLEXITY_API`,
the Azure settings, and the self-hosted `*_HEALTH_BACKEND_*` settings) are
covered in [docs/ml-backends.md](docs/ml-backends.md). Appeal generation needs
at least one model backend. Every key on this page is optional: without one,
the matching feature is skipped (or, for NCBI, rate-limited more tightly) and
the rest of the app keeps working.

## Quick reference

| Variable | Service | Required? | What it unlocks |
| --- | --- | --- | --- |
| none | ClinicalTrials.gov | No key needed | Trial registry evidence for "experimental/investigational" denials |
| `NICE_API_KEY` | NICE syndication | Optional | UK clinical guidance as international evidence in appeals |
| `NCBI_API_KEY` | NCBI / PubMed | Optional | Higher PubMed rate limits (3 to 10 requests per second) |
| none | RxNav / RxNorm | No key needed | Drug-name normalization (brand and generic names, misspellings) |
| `LOG_ANALYTICS_WORKSPACE_ID`, `LOG_ANALYTICS_WORKSPACE_KEY` | Azure Log Analytics | Optional | Ship app logs to a Log Analytics workspace |
| `HUMANJUDGE_API_KEY`, `HUMANJUDGE_TOKEN`, `HUMANJUDGE_MODEL_ID` | HumanJudge | Optional | Outside human review of Appeal Arena drafts, from the admin |
| `RENTAHUMAN_API_KEY`, `RENTAHUMAN_WEBHOOK_SECRET` | RentAHuman | Optional | Paid reviewers judging in the Appeal Arena itself |
| See [docs/ml-backends.md](docs/ml-backends.md) | Model backends | At least one | Appeal generation |

## Setting the variables

Every variable on this page is read from the process environment only, so
export it in the shell that starts the server:

```bash
export NICE_API_KEY="your-key-here"
```

Unlike the model backend keys, which fall back to `.env` on a local run (see
[How the settings are read](docs/ml-backends.md#how-the-settings-are-read)),
these are never read from `.env`. To keep your keys in a file, copy
[.env.example](.env.example) to `.env`, fill it in, and export it before
starting the server:

```bash
set -a; . ./.env; set +a
```

### In CI (GitHub Actions)

The CI workflow passes neither `NICE_API_KEY` nor `NCBI_API_KEY` today. To add
one:

1. Repo → **Settings → Secrets and variables → Actions → New repository
   secret**. Use the variable name as the secret name.
2. Expose it in the workflow step that runs the tests:
   ```yaml
   env:
     NICE_API_KEY: ${{ secrets.NICE_API_KEY }}
   ```

Every tox test environment sets `passenv = *` (`tox.ini`), so anything in the
step's environment reaches the tests.

---

## NICE syndication API (`NICE_API_KEY`)

NICE (the UK National Institute for Health and Care Excellence) publishes
evidence-based clinical recommendations, and its syndication API exposes that
guidance for reuse. Appeals cite it as **international clinical guidance**,
not as U.S. coverage authority. The client is
`fighthealthinsurance/nice_tools.py`.

### Sign up

1. Request access at
   https://www.nice.org.uk/reusing-our-content/nice-syndication-api.
2. Once approved, NICE issues a key through its API gateway (Azure API
   Management). The client sends the key in both the `Api-Key` and
   `Ocp-Apim-Subscription-Key` headers.
3. The client calls `https://api.nice.org.uk`, or `NICE_API_BASE_URL` when
   that is exported (read once, when `nice_tools.py` is imported).

### Behavior

- **Tests never reach the NICE API.** The `Test`, `TestSync` and `TestActor`
  configurations set `NICE_API_BASE_URL` to `http://127.0.0.1:1` in their
  `pre_setup`, which runs only for the configuration in use. `Dev` (including
  `scripts/run_local.sh`) and `Prod` leave it alone.
- **With the key set, under `Dev` or `Prod`:** the NICE lookup runs alongside the
  other evidence sources during appeal generation. Results are stored in
  `NICEGuidance` and `NICEQueryData`, and a cached query is reused for 30
  days.
- **Without the key:** the lookup is skipped, with a debug-level
  `NICE_API_KEY not set; skipping NICE search` log line. A `nice_context`
  already saved on the denial from an earlier run with the key is kept.
- **Live tests:** `TestNICESyndicationLive` in
  `tests/async-unit/test_nice_tools.py` skips when the key is unset. When it
  is set, the tests build their own client pointed at the real API
  (`NICE_API_LIVE_URL`, default `https://api.nice.org.uk`), so they are the
  one test that calls it.

---

## NCBI / PubMed (`NCBI_API_KEY`)

PubMed search works without a key, but NCBI rate-limits unauthenticated
clients more tightly:

| Mode | Requests per second |
| --- | --- |
| No key | 3 |
| With `NCBI_API_KEY` | 10 |

The key goes to both the [metapub](https://pypi.org/project/metapub/)
fetcher (`fighthealthinsurance/utils.py`) and the direct E-utilities calls in
`fighthealthinsurance/pubmed_tools.py`. The `tool` and `email` parameters that
NCBI asks for are fixed in `pubmed_tools.py` (`fighthealthinsurance` and
`support@fighthealthinsurance.com`); there is no variable for them.

Hitting the rate limit doesn't break appeal generation. PubMed timeouts are
caught and logged and the other evidence sources still run, but you'll see
more empty `pubmed_context` results.

### Sign up

1. Create an NCBI account at https://www.ncbi.nlm.nih.gov/account/.
2. Open https://account.ncbi.nlm.nih.gov/settings/, then **API Key
   Management → Create an API Key**.

---

## RxNav / RxNorm (no key required)

RxNorm is a standard drug vocabulary from the U.S. National Library of
Medicine, and RxNav is its public REST API at `https://rxnav.nlm.nih.gov/REST/`.
`fighthealthinsurance/rxnorm_tools.py` uses it to turn whatever the user typed
(for example `Glucophage`, `metformen`, `METFORMIN HCL 500mg`) into a canonical
drug name, an RxCUI, and brand and ingredient synonyms. The PubMed search, the
chat's drug lookup and prior authorization generation use it.

The RxNorm API needs no key or registration (see the
[RxNav API documentation](https://lhncbc.nlm.nih.gov/RxNav/APIs/RxNormAPIs.html)).
Nothing to configure: it works as long as the process can reach
`https://rxnav.nlm.nih.gov`.

- The client sends a `User-Agent` with a contact address
  (`FightHealthInsurance/1.0 (mailto:support@fighthealthinsurance.com)`) so
  NLM can reach us if our traffic looks abnormal.
- Lookups are cached for 30 days in the `RxNormConcept` table, per query
  string.
- A cold lookup makes one to three calls (`/rxcui.json`,
  `/rxcui/{id}/properties.json`, and optionally `/rxcui/{id}/related.json`),
  each with a 5-second timeout.
- Failures are logged at debug level, and the caller carries on with the drug
  name as typed.

---

## ClinicalTrials.gov (no key required)

The integration (`fighthealthinsurance/clinicaltrials_tools.py`) uses the
public [API v2](https://clinicaltrials.gov/data-api/api) at
`https://clinicaltrials.gov/api/v2/studies`. No registration, key or account is
needed.

### What it does

It returns matching studies (NCT ID, phase, status, conditions,
interventions, a brief summary and the study URL). That helps when an insurer
denies a treatment as "experimental or investigational": active or completed
trials show that a therapy is being studied or used clinically.

- When a denial's details are extracted, the app prefetches matching trials
  into the cache. Appeal generation then adds the cached matches to its
  context.
- In the chat, the model can ask for a search with a
  `[clinical trials query: ...]` token in its reply.

Results are cached in `ClinicalTrial` / `ClinicalTrialQueryData` for 30 days,
so a repeat query reads the database instead of calling the API.

### Pointing it elsewhere

Set `CLINICAL_TRIALS_API_BASE` to use a mirror or proxy. Normal use doesn't
need it.

```bash
# Optional. The default is https://clinicaltrials.gov/api/v2
export CLINICAL_TRIALS_API_BASE="https://clinicaltrials.gov/api/v2"
```

---

## Microsoft Azure Log Analytics (`LOG_ANALYTICS_WORKSPACE_ID` / `_KEY`)

Optional: ship application logs to an Azure Log Analytics workspace through
the
[HTTP Data Collector API](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/data-collector-api).
With the variables unset, nothing is shipped and there is no extra runtime
cost.

Microsoft ended support for the HTTP Data Collector API on 14 September 2026.
The app still sends through it (`fighthealthinsurance/log_analytics.py`), and
ingestion may keep working for a while, but it is unsupported: don't set it
up for a new workspace. Microsoft's replacement is the Logs Ingestion API; see
[its migration guide](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/custom-logs-migrate).

### Sign up

1. In the Azure portal, open (or create) a **Log Analytics workspace**.
2. Under **Settings → Agents**, copy the **Workspace ID** and **Primary key**.

### Configure

```bash
export LOG_ANALYTICS_WORKSPACE_ID="00000000-0000-0000-0000-000000000000"
export LOG_ANALYTICS_WORKSPACE_KEY="your-workspace-key"
# Optional: the destination custom-log table. Defaults to FightHealthInsurance.
export LOG_ANALYTICS_LOG_TYPE="FightHealthInsurance"
```

### Behavior

- **With both set:** `fighthealthinsurance/asgi.py` adds a loguru sink, and
  INFO and higher records (including stdlib `logging` calls, which are routed
  into loguru) are sent to the table named by `LOG_ANALYTICS_LOG_TYPE`. A
  background daemon thread does the sending over a shared `requests.Session`,
  so requests never wait on log shipping.
- **Without them:** `is_log_analytics_enabled()` returns False, no sink is
  added, and no network calls are made.
- Shipping errors are swallowed so logging can't break a request. Records are
  dropped if the in-memory send queue is full.

---

## HumanJudge (`HUMANJUDGE_API_KEY` / `_TOKEN` / `_MODEL_ID`)

Optional: lets staff send the Appeal Arena's synthetic appeal drafts to
[HumanJudge](https://humanjudge.com), whose reviewers (people, blind to the
model) pass or flag each one, and download their verdicts. Only synthetic
Arena drafts can be sent; real denials and appeals never are. How it works:
[docs/chooser.md](docs/chooser.md), "HumanJudge, an outside human judge".

### Sign up

1. Sign up as a developer at <https://humanjudge.com/auth?role=builder>
   (free during their beta).
2. In the developer hub, create one model for the Arena, standalone: not
   enrolled in any of their public benchmarks. Copy its API key, which
   starts with `gj_sk_`. That is `HUMANJUDGE_API_KEY`; it sends the drafts
   and can read their verdicts.
3. Optional: on <https://humanjudge.com/profile>, create a personal access
   token (starts with `gj_pat_`). That is `HUMANJUDGE_TOKEN`; when set, it is
   used to read verdicts instead of the model key. Set it if downloading
   verdicts fails with an access error.
4. Only if the account has more than one model: the Arena model's id (a
   UUID, shown in the developer hub) is `HUMANJUDGE_MODEL_ID`. With one
   model, the app finds it.

### Configure

```bash
export HUMANJUDGE_API_KEY="gj_sk_live_your-key"
# Optional, see above.
export HUMANJUDGE_TOKEN="gj_pat_your-token"
export HUMANJUDGE_MODEL_ID="00000000-0000-0000-0000-000000000000"
# Optional: CHOOSER_HUMANJUDGE=false turns it off with the key in place.
```

In production, add the keys to the shared `fight-health-insurance-secret`
Secret (namespace `totallylegitco`), for example:

```bash
kubectl -n totallylegitco patch secret fight-health-insurance-secret \
  --type merge -p '{"stringData":{"HUMANJUDGE_API_KEY":"gj_sk_live_your-key"}}'
```

then restart the web pods (the admin runs there): they read the Secret only
when they start. The Ray pods do not need it.

### Behavior

- **With the key set:** the Chooser Task list's **Upload to HumanJudge**
  action sends the selected cases' drafts, and the HumanJudge Verdict
  list's **Download verdicts from HumanJudge** button fetches the new
  verdicts. Nothing is sent or fetched on its own.
- **Without it:** both say HumanJudge is not configured, and nothing is
  sent anywhere.
- To check the setup, upload one case, then download. A message that votes
  "matched no draft we uploaded" means verdicts are arriving but cannot be
  tied to drafts; report it, as the vote format is not fully documented.

## RentAHuman (`RENTAHUMAN_API_KEY` / `RENTAHUMAN_WEBHOOK_SECRET`)

Optional: hires paid reviewers through [RentAHuman](https://rentahuman.ai)
to judge in the Appeal Arena itself, as a bounty with a join link; their
votes are ordinary Arena votes tagged with the paid run. Only the Arena's
synthetic cases are shown. How it works: [docs/chooser.md](docs/chooser.md),
"Paid reviewers (RentAHuman)".

### Sign up

RentAHuman's website screens out people, so sign up the way it asks agents
to: `POST https://rentahuman.ai/api/agents/signup` with
`{"email": ..., "acceptTerms": true, "country": "US"}` (this accepts their
Terms of Service and Privacy Policy), then
`POST https://rentahuman.ai/api/agents/signup/verify` with the email and the
6-digit code they email. The answer holds the API key (`rah_...`), shown
once. An email that already has an account gets no code; with any working
key, `POST https://rentahuman.ai/api/keys` (`{"name": ...}`) makes another,
and `DELETE /api/keys/{id}` revokes one. Their answers repeat the key in more
than one field, so keep them out of logs.

### Configure

```bash
export RENTAHUMAN_API_KEY="rah_your-key"
# Printed once by `manage.py rentahuman_setup --webhook` (after deploy).
export RENTAHUMAN_WEBHOOK_SECRET="whsec_your-secret"
```

In production, add them to the shared `fight-health-insurance-secret`
Secret (namespace `totallylegitco`) and restart the web pods, which serve
the join link and the webhook:

```bash
kubectl -n totallylegitco patch secret fight-health-insurance-secret \
  --type merge -p '{"stringData":{"RENTAHUMAN_API_KEY":"rah_your-key"}}'
kubectl -n totallylegitco rollout restart deployment/web
```

Then, from a web pod (`kubectl -n totallylegitco exec -it deploy/web -- ...`):
`python manage.py rentahuman_setup` checks the wallet; `--deposit 6` prints
a Stripe link to fund it; `--webhook` registers the webhook and prints its
secret; `--test-webhook <id>` sends a signed test event. Post bounties from
there too (`post_paid_review_bounty`), since the join link only works for a
bounty saved in the production database.

### Behavior

- **With the key set:** `post_paid_review_bounty` previews (and with
  `--post`, posts) bounties, and submissions are checked and approved or
  sent back for a redo. Payment is released by a person on RentAHuman.
- **With the webhook secret set:** submissions are checked as they come in;
  without it, run `sync_paid_reviews`.
- **Without the key:** nothing is sent anywhere; join links of bounties
  already saved still work.

---

## Troubleshooting

- **NICE guidance isn't showing up in appeals.** Under the test
  configurations that is expected; see the NICE [Behavior](#behavior) notes.
  Otherwise the key didn't reach the process: with debug logging on, look for
  `NICE_API_KEY not set; skipping NICE search`. For the live tests in CI,
  check that the step has `NICE_API_KEY: ${{ secrets.NICE_API_KEY }}` in its
  `env`.
- **PubMed timeouts under load.** Add `NCBI_API_KEY`. Without one, NCBI limits
  you to 3 requests per second.
- **A key from this page in `.env` has no effect.** Export it; see
  [Setting the variables](#setting-the-variables).

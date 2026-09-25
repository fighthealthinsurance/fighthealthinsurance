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
| `NICE_API_KEY` | NICE syndication | Optional | UK clinical guidance as international evidence in appeals (Prod only) |
| `NCBI_API_KEY` | NCBI / PubMed | Optional | Higher PubMed rate limits (3 to 10 requests per second) |
| none | RxNav / RxNorm | No key needed | Drug-name normalization (brand and generic names, misspellings) |
| `LOG_ANALYTICS_WORKSPACE_ID`, `LOG_ANALYTICS_WORKSPACE_KEY` | Azure Log Analytics | Optional | Ship app logs to a Log Analytics workspace |
| See [docs/ml-backends.md](docs/ml-backends.md) | Model backends | At least one | Appeal generation |

## Setting the variables

Every variable on this page is read from the process environment, so export
it in the shell that starts the server:

```bash
export NICE_API_KEY="your-key-here"
```

The app does not load `.env` by itself (see
[How the settings are read](docs/ml-backends.md#how-the-settings-are-read)).
To keep your keys in a file, copy [.env.example](.env.example) to `.env`, fill
it in, and export it before starting the server:

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
3. The client calls `https://api.nice.org.uk`. `nice_tools.py` reads
   `NICE_API_BASE_URL` as an override, but the settings overwrite it, so
   setting it has no effect today (see below).

### Behavior

- **Only `Prod` reaches the NICE API.** Python runs every settings class body
  when `fighthealthinsurance/settings.py` is imported, whichever
  configuration is selected. The `Test`, `TestSync` and `TestActor` bodies set
  `NICE_API_BASE_URL` to `http://127.0.0.1:1`, which keeps tests off the real
  API. Only `Prod.pre_setup` clears it again. So under `Dev`, including
  `scripts/run_local.sh`, the lookup goes to that unroutable address and finds
  nothing, even with a key. The same assignments replace any
  `NICE_API_BASE_URL` you export, which is why the override has no effect.
- **With the key set, under `Prod`:** the NICE lookup runs alongside the
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
  one place outside `Prod` that calls it.

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

## Troubleshooting

- **NICE guidance isn't showing up in appeals.** Outside `Prod` that is
  expected; see the NICE [Behavior](#behavior) notes. Under `Prod`, the key
  didn't reach the process: with debug logging on, look for
  `NICE_API_KEY not set; skipping NICE search`. For the live tests in CI,
  check that the step has `NICE_API_KEY: ${{ secrets.NICE_API_KEY }}` in its
  `env`.
- **PubMed timeouts under load.** Add `NCBI_API_KEY`. Without one, NCBI limits
  you to 3 requests per second.
- **A key in `.env` has no effect.** Export it; see
  [Setting the variables](#setting-the-variables).

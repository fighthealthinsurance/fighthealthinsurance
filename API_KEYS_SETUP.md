# API Keys Setup

Fight Health Insurance integrates with several external services. This guide covers
where to register for each key, what it unlocks, and how to wire it up locally
and in CI.

## Quick reference

| Variable                             | Service          | Required?        | What it unlocks |
| ------------------------------------ | ---------------- | ---------------- | --------------- |
| `NICE_API_KEY`                       | NICE syndication | Optional         | UK clinical guidance as international evidence in appeals |
| `NCBI_API_KEY`                       | NCBI / PubMed    | Semi-optional    | Higher PubMed rate limits (3 → 10 req/sec) |
| `OCTOAI_TOKEN`                       | OctoAI           | One ML backend required | Cloud ML inference for appeal generation |
| `HEALTH_BACKEND_HOST` / `_PORT`      | Local LLM        | One ML backend required | Self-hosted ML inference |
| `PERPLEXITY_API`                     | Perplexity       | Optional         | Sonar models for citation discovery |
| `GROQ_API_KEY`                       | Groq             | Optional         | Fallback chat / inference |
| `DEEPINFRA_API`                      | DeepInfra        | Optional         | Additional inference backend |

You need **at least one** ML backend (OctoAI, a local LLM, Perplexity, Groq, or
DeepInfra) for appeal generation to work. Everything else degrades gracefully.

---

## NICE syndication API (`NICE_API_KEY`)

NICE (UK National Institute for Health and Care Excellence) publishes
evidence-based clinical recommendations. The syndication API exposes that
guidance for re-use. We surface it in appeals as **international clinical
guidance**, not as U.S. coverage authority.

### Sign up

1. Visit https://www.nice.org.uk/reusing-our-content/syndication-api and
   request access.
2. Once approved, NICE issues a key through their API gateway (Azure APIM).
   The key works under either of these headers — the client sends both:
   - `Api-Key`
   - `Ocp-Apim-Subscription-Key`
3. The default base URL is `https://api.nice.org.uk`. To override (for staging
   or a future endpoint change), set `NICE_API_BASE_URL`.

### Configure

Local development:

```bash
export NICE_API_KEY="your-key-here"
```

Or add it to your `.env` file.

GitHub Actions (CI):

1. Repo → **Settings → Secrets and variables → Actions → New repository
   secret**.
2. Name: `NICE_API_KEY`. Value: the key from NICE.
3. Expose it in the relevant workflow step:
   ```yaml
   env:
     NICE_API_KEY: ${{ secrets.NICE_API_KEY }}
   ```
   `tox.ini` sets `passenv = *`, so anything in the env reaches the test
   environment automatically.

### Behavior

- **With the key set:** NICE search runs alongside PubMed / RAG / ML citations
  during appeal generation and persists results in `NICEGuidance` /
  `NICEQueryData`. Cached results are reused for 30 days per query.
- **Without the key:** the NICE step is skipped entirely; appeal generation
  proceeds with the other sources. Any `denial.nice_context` already persisted
  from a prior key-enabled run is preserved.
- **Live integration tests** (`TestNICESyndicationLive` in
  `tests/async-unit/test_nice_tools.py`) auto-skip when the key is unset and
  exercise the real API when it is set.

---

## NCBI / PubMed (`NCBI_API_KEY`)

The PubMed integration uses the [metapub](https://pypi.org/project/metapub/)
library, which reads `NCBI_API_KEY` from the environment automatically.

The integration **works without a key**, but NCBI rate-limits unauthenticated
clients more aggressively. With a key you get higher throughput, which matters
under load (concurrent appeal generations) and for batch jobs.

| Mode                | Requests / second |
| ------------------- | ----------------- |
| No key              | 3                 |
| With `NCBI_API_KEY` | 10                |

Hitting the rate limit doesn't break appeal generation — PubMed timeouts are
caught and logged, and other evidence sources still run — but you'll see more
empty `pubmed_context` results.

### Sign up

1. Create an NCBI account at https://www.ncbi.nlm.nih.gov/account/.
2. Open https://account.ncbi.nlm.nih.gov/settings/ → **API Key Management** →
   **Create an API Key**.
3. Optionally also set `NCBI_EMAIL` (a contact email NCBI sees for your
   requests) and `NCBI_TOOL` (an identifier like `fighthealthinsurance`).

### Configure

```bash
export NCBI_API_KEY="your-key-here"
export NCBI_EMAIL="ops@your-domain.example"   # optional but recommended
export NCBI_TOOL="fighthealthinsurance"        # optional
```

Same GitHub Secrets pattern as above.

---

## ML backends (at least one required)

You need one ML backend wired up for appeal generation. Pick whichever fits
your environment:

### OctoAI (`OCTOAI_TOKEN`) — easiest cloud option

Sign up at https://octoai.cloud/, create a token, and:

```bash
export OCTOAI_TOKEN="your-token"
```

### Local LLM — self-hosted

Use [healthinsurance-llm](https://github.com/fighthealthinsurance/healthinsurance-llm)
(GPU required, 3090 or equivalent):

```bash
export HEALTH_BACKEND_HOST="localhost"
export HEALTH_BACKEND_PORT="8000"
```

### Optional supplemental backends

These are picked up automatically by `MLRouter` when their env var is set; the
router falls back through them in cost order.

- **Perplexity** — sign up at https://www.perplexity.ai/settings/api, then
  `export PERPLEXITY_API="..."`. Used for the Sonar citation backends.
- **Groq** — sign up at https://console.groq.com/keys, then
  `export GROQ_API_KEY="..."`.
- **DeepInfra** — sign up at https://deepinfra.com/dash/api_keys, then
  `export DEEPINFRA_API="..."`.

---

## Troubleshooting

- **"Why isn't NICE showing up in my appeals?"** Check `logger` output for
  `NICE_API_KEY not set; skipping NICE search` — that means the env var didn't
  reach the process. In CI, confirm the workflow step has
  `env: NICE_API_KEY: ${{ secrets.NICE_API_KEY }}`.
- **PubMed timeouts under load.** Add `NCBI_API_KEY`. Without one, NCBI starts
  rate-limiting at the 3 req/sec ceiling.
- **`tox` doesn't see my key.** `tox.ini` already has `passenv = *` at the top
  level. If you're running a single environment that overrides `passenv`,
  re-add `passenv = *` to that environment block.

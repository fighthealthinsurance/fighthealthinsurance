# LLM usage metrics

How much model work we do, where it comes from, and what kind of network
asks for it, without keeping anything that identifies a person.

The code is [`fighthealthinsurance/ml/llm_usage.py`](../fighthealthinsurance/ml/llm_usage.py)
(what is counted, and the context that says where it came from),
[`ml/llm_usage_ledger.py`](../fighthealthinsurance/ml/llm_usage_ledger.py)
(the database side and the weekly roll-up),
[`ml/llm_usage_report.py`](../fighthealthinsurance/ml/llm_usage_report.py)
(what the staff pages show) and
[`client_network.py`](../fighthealthinsurance/client_network.py)
(reading, bucketing and keying the client's network).

## What is counted

Every model request that gets an answer, once, with the tokens its provider
reported:

- the OpenAI-compatible transport (our own models, DeepInfra, Perplexity,
  Anthropic, Azure OpenAI), at `RemoteOpenLike.__infer`;
- the Azure Messages transport (`RemoteAzureClaude._messages_request`), where
  cache reads and writes count as prompt tokens;
- TypeSafe (`ml/typesafe.py`), input tokens only.

Timeouts, cancelled losing legs of a race, and HTTP errors are not answers.
They stay in `fhi_ml_calls_total` ([model-backend-health.md](model-backend-health.md)).
An answer with no usage block is counted as a request with `usage="missing"`
and adds no tokens.

Each request carries four labels, all from fixed sets:

| Label | Values |
| --- | --- |
| `surface`: where the work entered | `site` (patient pages and sockets), `pro` (professional chat, prior auth, a professional's case), `assistant` (a case an AI assistant brought in), `staff` (`/timbit` tools), `system` (no person behind it: probes, the chooser, PubMed prefetch), `unknown` |
| `task`: what the model did | `appeal_letter`, `appeal_precompute`, `synthesis`, `prior_auth_letter`, `regulator_letter`, `chat_reply`, `chat_summary`, `chat_analysis`, `entity_extraction`, `questions`, `citations`, `doc_summary`, `policy_analysis`, `pubmed_summary`, `triage`, `letter_scoring`, `reply_gate`, `reply_shadow`, `chooser`, `staff_query`, `intro_email`, `probe`, `other` |
| `tier` | `internal`, `external` |
| `network_class` | `isp`, `hosting`, `ai_platform`, `tor`, `unknown`, `none` |

A case is `assistant` when `Denial.channel` is `"assistant"` (agreed on the
chat path's terms page) or its latest `ConsentRecord` has channel
`"assistant"` (an assistant's link opened the site's form). The MCP server
itself calls no model: its work shows up here when the person opens the link.

`unknown` means no entry point set a surface: a wiring gap to fix, shown on
purpose. The status page flags it.

### How the labels get there

The entry points set an origin (surface and network) and the flows set a
task, in ContextVars, the same way `ML_CALL_PURPOSE` works. The executors in
`exec.py` copy the context, so it reaches the model threads.

- **Sockets:** `websockets.LLMUsageOriginMixin` on the five model sockets.
  Prior auth is `pro`; a chat becomes `pro` once it is known to be a
  professional's.
- **Views:** `@llm_usage.http_entry()` on the next-steps views and the
  prior-auth REST endpoints, and `llm_usage.iterate_from` around the REST
  appeal stream.
- **Cases:** `anote_denial` / `@for_denial(task=...)` next to the spend
  channel calls, so work on a case counts from where the case came from.
- **Tasks:** `llm_task` / `@labelled_task` at each step. A pinned task (the
  chooser, staff tools) wins over everything inside it. Then come a probe, a
  task the transport names (TypeSafe), the innermost step, and finally what
  `ML_CALL_PURPOSE` means (`appeal` gives `appeal_letter`, `chat` gives
  `chat_reply`).
- **Ray and Temporal** lose the context. Work there sets it again from the
  `Denial` or `OngoingChat` it works on: the surface from the case, and the
  network class from the ASN recorded on it. It never gets the address, so
  it has no weekly key.

## The network

The client's address comes from `CF-Connecting-IP` only, the header
Cloudflare overwrites on every request, never `X-Forwarded-For`. A request
without that header (in-cluster, local dev) has network class `unknown` and
no key.

- **Class:** these rules apply in order.
  1. Cloudflare's country `T1` gives `tor`.
  2. An address in an AI platform's published range gives `ai_platform`.
     Anthropic's `160.79.104.0/21` is built in; add others with
     `FHI_AI_PLATFORM_CIDRS`.
  3. Otherwise the ASN name decides:
     - `ANTHROPIC` or `OPENAI` gives `ai_platform`;
     - a cloud or hosting provider (`AMAZON`, `GOOGLE` but not
       `GOOGLE-FIBER`, `MICROSOFT`, `HETZNER`, `OVH`, `CLOUDFLARENET`, …)
       gives `hosting`;
     - any other name gives `isp`;
     - no name gives `unknown`.

  iCloud Private Relay and Cloudflare WARP users land in `hosting`, since they
  egress from those providers. Background work's class comes from
  `Denial.asn_name`, which is derived from a header the client can set, so
  someone could mislabel their own traffic there.
- **ASN name and country:** from the GeoIP database
  ([geoip.md](geoip.md)), read only if it is already loaded, so the event
  loop never waits for it. The country prefers Cloudflare's `CF-IPCountry`.
  Both go to the database's daily network totals, never to Prometheus.
- **Weekly key:** the client's IPv4 /24 or IPv6 /48, as an HMAC under a
  sub-key of `SECRET_KEY` with the ISO week in the message. One network has
  one key all week and a new one the next. It is kept for `site`, `pro` and
  `assistant` requests only (never staff, never system work) and is what the
  "how many networks, and how much did the busiest send" figures count.

## Where the numbers go

**Prometheus**, through the same `/metrics` as the rest
([metrics-endpoint-access.md](metrics-endpoint-access.md)). Per pod, so
aggregate with `sum()`.

| Series | Labels |
| --- | --- |
| `fhi_llm_requests_total` | `model`, `tier`, `surface`, `task`, `usage` (`reported` or `missing`) |
| `fhi_llm_tokens_total` | `model`, `tier`, `surface`, `task`, `kind` (`prompt` or `completion`) |
| `fhi_llm_network_requests_total` | `tier`, `surface`, `task`, `network_class` |
| `fhi_llm_network_tokens_total` | `tier`, `surface`, `network_class`, `kind` |
| `fhi_llm_usage_ledger_dropped_total` | `table`: entries the database ledger dropped |

No label ever carries an address, a key, an ASN or a country. Calls made on
the Ray actors land in registries nothing scrapes, so for them only the
database counts.

**The database**, shared by every pod, the Temporal worker and the Ray
actors. Writes are batched in memory and stored by one background thread
per process every 5 seconds, like the spend ledger.

| Table | Keyed by | Kept |
| --- | --- | --- |
| `LLMUsageDaily` | day, surface, task, model, tier, network class | 400 days (`FHI_LLM_USAGE_DAILY_RETENTION_DAYS`) |
| `LLMUsageNetworkDaily` | day, surface, network class, ASN name, country | 90 days (`FHI_LLM_USAGE_NETWORK_RETENTION_DAYS`) |
| `LLMUsageNetworkWeek` | ISO week, weekly key, surface, network class | Its week only |
| `LLMUsageNetworkWeekSummary` | ISO week, surface or `all`, network class or `all` | Kept: no keys |

The hourly `sweep_assistant_drafts` CronJob (also `manage.py
rollup_llm_usage`) rolls each week's keyed rows up into summaries an hour
after the week ends, then deletes them. A summary records how many networks
there were, the calls and tokens, and what the busiest one and ten sent.
Rows that arrive after their week is summarized are deleted unread.

## What is never stored

- A client's address, or its /24 or /48. The prefix exists only in memory,
  while a request runs, to compute the key.
- A weekly key past its week. It is never in logs, Prometheus, Temporal
  history or Ray arguments.
- User, denial or chat ids, or any text.

Anyone holding `SECRET_KEY` and a week's keyed rows could test every IPv4
/24 against them. That is why the rows go when their week does, and why the
summaries keep no keys.

## Where to look

- **`/timbit/help/status`**, the "LLM usage" panel. It is staff-only, like
  the rest of the page (`staff_member_required`), and shows:
  - today's and the last 7 days' requests and tokens;
  - the external share of tokens;
  - requests with no token count;
  - the last 7 days by surface, top task and network class;
  - this week's network concentration.
- **`/timbit/help/model_usage`**, the "LLM usage" tables for the last 30 days:
  - by surface and task;
  - by model;
  - by surface and network class;
  - the top 25 ASNs;
  - weekly network concentration.

PromQL:

```promql
# Tokens by surface, last day
sum by (surface) (increase(fhi_llm_tokens_total[1d]))

# External tokens by task, last day
sum by (task) (increase(fhi_llm_tokens_total{tier="external"}[1d]))

# Share of site requests from hosting networks
sum(rate(fhi_llm_network_requests_total{surface="site",network_class="hosting"}[1h]))
  / sum(rate(fhi_llm_network_requests_total{surface="site"}[1h]))

# Wiring gaps: requests no entry point labelled, by task
sum by (task) (increase(fhi_llm_requests_total{surface="unknown"}[1d]))

# Models that report no usage
sum by (model) (increase(fhi_llm_requests_total{usage="missing"}[1d]))
  / sum by (model) (increase(fhi_llm_requests_total[1d]))
```

## Switches

| Setting | Effect |
| --- | --- |
| `FHI_LLM_USAGE_DB=false` | Prometheus only: nothing is written to the database. |
| `FHI_LLM_USAGE_NETWORK_KEYS=false` | No weekly keyed rows: the daily totals and Prometheus go on. |
| `FHI_LLM_USAGE_BACKGROUND` | The ledger's writer thread. Off in the test configurations, where tests flush it by hand. |
| `FHI_AI_PLATFORM_CIDRS` | More AI platform ranges (comma-separated CIDRs). |

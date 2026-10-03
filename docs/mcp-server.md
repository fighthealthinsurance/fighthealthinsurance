# MCP server

The read-only server for AI assistants at `/mcp` is described in the
docstring of `fighthealthinsurance/mcp_server.py`. This page covers running
it.

## Alerts for prepare_appeal

`prepare_appeal` is the one tool that keeps anything: it holds the denial
letter an assistant sends until the person opens the link, for at most 2
hours (`fighthealthinsurance/assistant_handoff.py`). Two caps keep that table
small, and both are global, while `/mcp` takes anonymous calls. So one
caller, or a real burst, can fill them. Nothing errors when that happens:
each refused assistant is told to send its person to start on the site and
paste the letter there. The alerts are how we find out.

### What is counted

Counts only. No series carries a letter, a code or who called.

| Series | Type | Moves when |
|---|---|---|
| `fhi_assistant_handoff_links_made_total` | counter | `prepare_appeal` makes a link |
| `fhi_assistant_handoff_forms_opened_total` | counter | a link opens the filled-in form |
| `fhi_assistant_handoff_links_expired_total` | counter | a web pod deletes links nobody opened (see below) |
| `fhi_assistant_handoff_dead_opens_total` | counter | someone opens a used, expired or unknown link |
| `fhi_assistant_handoff_refused_at_cap_total` | counter | `prepare_appeal` is refused at either cap |
| `fhi_assistant_handoff_live_links` | gauge | read from the table at each scrape |

The counters are per web pod, so add them up with `sum()`. The gauge is
counted from the table, so every pod reports the same number: take `max()`.

`links_expired_total` counts what `prepare_appeal`'s own sweep deletes. The
10-minute sweep CronJob is a separate process nothing scrapes, so what it
deletes is in its log line instead. Read the counter as "at least". For
"made and never opened" over a day, `made - opened` is the better number.

### The two alerts

Both are warnings, in `k8s/assistant-handoff-alerts.yaml`, applied by
`scripts/build.sh` when the cluster has the PrometheusRule CRD.

- **FhiAssistantHandoffAtCap**: any refusal at a cap in the last 15 minutes.
  `sum(increase(fhi_assistant_handoff_refused_at_cap_total[15m])) > 0`
- **FhiAssistantHandoffNearCap**: live links above 80% of the live cap for 30
  minutes. `max(fhi_assistant_handoff_live_links) > 240`. The 240 is written
  out because a rule can't read a Django setting. Change it with
  `MCP_PREPARE_APPEAL_MAX_LIVE`; a test fails if the two drift apart.

Where alerts go from there (email, paging) is set in the cluster's
Alertmanager, not in this repo. Check that warnings from the other FHI rule
files reach someone before counting on these.

### When one fires

1. **Burst or real use?** Compare
   `sum(increase(fhi_assistant_handoff_links_made_total[1h]))` with
   `sum(increase(fhi_assistant_handoff_forms_opened_total[1h]))`. Each link
   is made for a person who asked for it, so most should be opened. Many made
   and almost none opened is a script.
2. **Which cap?** `max(fhi_assistant_handoff_live_links)` at or near the live
   cap means it is full. Below it, the per-minute cap is the one refusing.
3. **One source?** The app keeps no record of callers, on purpose. Look at
   the path `/mcp` in Cloudflare's analytics, by source IP. Calls from an
   assistant platform come from that platform's addresses, not the person's:
   Anthropic's are `160.79.104.0/21` (its published outbound range, checked
   2026-10-03). A busy address there is many people, not one.
4. **Then:**
   - One address outside the platforms: block it with a Cloudflare rule.
   - Real use: raise the caps. They are env vars on the web Deployment, read
     at start, so the change needs a rollout. Raise the NearCap threshold
     with them.
   - Either way, the live links clear themselves within 2 hours once the
     burst stops.

### Starting caps

Keep today's: **300 links live** (`MCP_PREPARE_APPEAL_MAX_LIVE`) and **30 per
minute** (`MCP_PREPARE_APPEAL_MAX_PER_MINUTE`). Both are guesses. Look again
after a week of the counts above.

### A Cloudflare backstop (a suggestion, not configured)

A rate limiting rule on `/mcp`, about 60 requests a minute per IP:

- If request matches: `http.request.uri.path in {"/mcp" "/mcp/"} and http.request.method eq "POST"`
- Characteristics: IP
- Rate: 60 requests per 1 minute
- Action: Block for 1 minute. Not a challenge: an MCP client can't solve one.

What the plans allow (Cloudflare's docs, checked 2026-10-03): matching on the
method needs Business or above, so on Free or Pro drop the method clause.
Free counts over 10 seconds only (so 10 per 10 seconds) and blocks for 10
seconds; Pro allows a 1-minute period and up to an hour's block.

What it does and doesn't do:

- It stops one address from flooding `/mcp` as a whole, read-only tools
  included.
- It does not stop one address from filling `prepare_appeal`'s caps. The
  server is stateless, so a script needs one POST per link: 60 a minute from
  one address is more than the 30-a-minute cap, and fills the 300 live links
  in about 10 minutes.
- Per-IP counting puts every user of one assistant platform in the same
  bucket. Watch the rule's hits for the platform ranges above before
  tightening it.

Keeping one caller from filling the caps for everyone needs a per-caller
quota in the app, which is still to decide.

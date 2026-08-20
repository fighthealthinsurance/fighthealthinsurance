# Chat pipeline: architecture, failure modes, and roadmap

A detailed think-through of the chat stack as of the loop-prevention work
(PR #934). Written to be the reference for "why does chat behave this way"
questions: the end-to-end flow, the scoring math with real numbers, every
anti-loop defense and where it sits, how models are chosen, what we
deliberately did not build, and the prioritized list of what should come
next.

## 1. End-to-end flow of one turn

```
Browser (React chat_interface.tsx)
  │  scrub PII client-side ({{FIRST_NAME}}, {{Your Email Address}}, ...)
  │  ws frame: {content, chat_id, use_external_models, debug, ...}
  ▼
OngoingChatConsumer (websockets.py)
  │  auth/session resolution, IP -> state hint (guess_us_state, geoip2fast)
  │  debug gating (settings.DEBUG or staff)
  ▼
ChatInterface.handle_chat_message (chat_interface.py)
  │  crisis check -> early resources reply
  │  EARLY pre-persist of the user message (disconnect safety)
  │  prepare_history_for_llm (context_manager.py): truncate to last 20,
  │    summarize dropped prefix once per 10-message band, bound the
  │    accreted summary to FHI_CHAT_MAX_SUMMARY_CHARS (6000)
  │  prepare_user_message_variants (message_preprocessor.py)
  │  state hint injected into the summary context as an UNCONFIRMED guess
  ▼
_call_llm_with_actions
  │  allow_repeat = user_requested_repeat(RAW message)   <- before wrapping
  │  build_llm_calls[_for_variants] (llm_client.py):
  │    one call per backend x {truncated, full} history x message variant,
  │    base score = quality()^2 // divisor, call -> label map
  │  create_response_scorer: content bonuses/penalties, HARD -inf rejection
  │    of (near-)repeats, per-session repeat-offender decay
  │  best_two_within_timelimit (utils.py): 30s + 30s overtime, returns
  │    best + runner-up + scores + originating calls
  │  [if unusable] retry_llm_with_fallback (retry_handler.py): shortened
  │    history, anti-repeat note + temperature 0.85 when repeats were
  │    rejected, fallback backends, 35s + 40s; repeats get a finite
  │    last-resort penalty here instead of -inf
  │  alternate answer: runner-up kept only when scores are CLOSELY TIED
  │  debug_llm_input / debug_llm_result frames when debug is on
  │  tool handlers (appeal, prior auth, medicaid, pubmed, doc fetcher, ...)
  │    -- recursive tools re-enter _call_llm_with_actions at depth+1
  ▼
persistence (chat_persistence.py): transactional turn persist with
  tail-dedup, summary list capped at 20; panda-summary placeholder swap
  happens in the background when the model omitted its summary
  ▼
ws frames out: status heartbeats, content (+ alternate_content), metrics
```

Everything in the fan-out is concurrent; the serial spine of a turn is
summarization (when it fires) -> fan-out window -> optional retry window ->
tool processing. The whole turn runs under FHI_CHAT_TURN_BUDGET (150s
default), and the fan-out windows were sized (30+30, 35+40) so the ladder
fits inside it.

## 2. The scoring system, with real numbers

Selection is score-based, not ordered fallback. Each call starts from a
base score derived from the model's self-reported quality():

| backend                      | quality | primary base (q^2//5) | full-history (q^2//4) |
|------------------------------|---------|------------------------|------------------------|
| AlphaRemoteInternal (fhi)    | 210     | 8820                   | 11025                  |
| NewRemoteInternal (fhi)      | 200     | 8000                   | 10000                  |
| RemoteHealthInsurance legacy | 101     | 2040                   | 2550                   |
| paid external, premium tier  | 98      | 1920                   | 2401                   |
| DeepInfra DeepSeek-V4-Pro    | 92      | 1692                   | 2116                   |

Content signals then adjust: +100 primary-variant bonus, +100 substantial
response, +10 context, +100 tool-call bonus, +150 mentions an uploaded
document, -75 system-prompt leak, -200 false promise, -200 asks for the
patient's name, repetition penalties (-500 exact / -400 near / -75
bag-of-words vs recent messages), internal-repetition penalty up to -200.

Two consequences worth internalizing:

* **Base scores dominate content signals across tiers.** An internal model
  beats an external on base score by ~6000; no combination of content
  signals (max swing well under 1500) flips that. Cross-tier selection
  therefore only changes hands when the higher tier is *invalid* — which
  is exactly what hard rejection provides.
* **Within a tier, content signals decide.** Two internal calls differ by
  0–2000 base (trunc vs full history), so repeats, leaks, and document
  mentions actually matter there.

The -inf hard rejection (a candidate that nearly repeats one of the last 3
assistant replies, unless the user asked for a repeat or it's a mandated
canned reply) is what turned the scorer from "prefers not to loop" into
"cannot deliver a loop from the primary pass". The old -500 soft penalty
was invisible next to an 8000+ base — that was the production loop.

### Similarity detection: two non-obvious constraints

`response_similarity.py` is small but has two properties that must not be
"cleaned up":

* **`autojunk=False` is required for correctness.** difflib's default
  autojunk heuristic marks any element appearing in >1% of a 200+ element
  sequence as junk and refuses to match on it. On character-level natural
  language that is every space and every common letter, so `ratio()`
  collapses: two 430-char replies differing by one reworded sentence
  measured **0.33 with autojunk vs 0.96 without**, against a 0.9 threshold.
  With the default, every long near-verbatim repeat — precisely what the
  detector exists for — scored as unrelated.
* **The cheap gates in front of `ratio()` are load-bearing, not premature
  optimization.** Scoring runs inline on the event loop inside the fan-out,
  several comparisons per candidate, ~20 candidates per turn. With
  `autojunk=False`, `ratio()` is genuinely O(n·m). Two exact upper bounds run
  first — the length bound `2·min/(la+lb)`, then word-set Jaccard against a
  loose 0.35 floor — so unrelated pairs (the overwhelming majority) cost
  ~0.1ms instead of 15-60ms, and only plausible repeats pay the full
  comparison. `quick_ratio` is *not* a useful gate here: it compares
  character multisets and reads ~0.98 for two unrelated English texts.

## 3. The anti-loop ladder (all layers)

Defenses stack from the inside out, so any single layer failing still
leaves the loop broken:

1. **Prompting** (ml_models.py): the system prompt tells the model to never
   repeat an earlier reply and explains the client-side privacy
   placeholders ({{STATE}} etc.) — the original trigger was the model
   receiving `{{STATE}}` with no explanation, concluding it still didn't
   know the state, and re-asking forever. The client no longer scrubs
   state at all (a state name isn't identifying enough to justify breaking
   the model's ability to use the answer).
2. **Per-backend self-heal** (generate_chat_response): a fresh generation
   that nearly repeats the last assistant reply gets ONE corrective retry
   with feedback text and +0.15 temperature before the caller ever sees
   it. Skipped for canned replies and user-requested repeats
   (allow_repeated_reply, derived from the RAW message upstream).
3. **Hard rejection in scoring** (-inf): a repeat cannot win the primary
   fan-out. rejection_stats counts what was rejected.
4. **Repeat-offender decay** (create_response_scorer): each hard-rejected
   repeat adds a per-session strike to that backend's label; strikes decay
   its BASE score by 0.7^strikes (capped at 4). A persistently looping
   internal backend slides toward external-tier preference within the
   session, so the fan-out stops re-electing it on raw quality. In-memory
   only; resets with the WebSocket session.
5. **Anti-repeat retry** (retry_llm_with_fallback): when everything usable
   was rejected as a repeat, the retry appends an explicit
   system-injected do-not-repeat note (also changes prompt bytes ->
   busts upstream response caches) and samples at 0.85.
6. **Last-resort delivery**: the retry scorer penalizes repeats by -1e6
   (finite) instead of -inf — a repeat beats an error frame, but only when
   literally nothing else came back.
7. **Terse-reply bridge**: a short user reply (<= 60 chars) right after an
   assistant question gets a bridging note telling the model the reply
   answers that question — the "CA" case that models previously ignored.
8. **Metrics** (ml_metrics.py): fhi_chat_repeated_responses_total
   {action=rejected_candidates|delivered_repeat} makes the ladder's
   behavior observable in production.

## 4. Model selection (and why external models are now default-on)

get_chat_backends_with_fallback builds the fan-out:

* the primary fhi backend, doubled (redundancy against a slow pod),
* the strongest 6 *available* internal backends — quality-sorted since
  this change; the old cost-sort quietly picked the cheapest end,
* when external models are enabled: the best <= 3 externals
  (quality-sorted, health-gated, one Groq max) now join the PRIMARY
  fan-out too, plus serve as the retry-pass fallback list.

Why externals in the primary pass: with externals only in the fallback,
an internal-loop turn had to burn the full 30+30s primary window before an
external got a chance. In the primary fan-out, the external answer is
already in hand at rejection time — the quadratic base score still prefers
internals whenever they produce a valid reply, so the privacy/cost
preference is intact; the external answer only surfaces when the internals
loop or fail.

Why default-on: chat messages are PII-scrubbed client-side before they
leave the browser, the consent form's toggle has defaulted to checked for
a while, and the failure mode it prevents (turn fails entirely because the
internal pool is down or looping) is much worse for users than the
marginal exposure of scrubbed text to a vetted external provider. Users
can still opt out — the toggle stores an explicit "false" and the server
respects it; the server also treats an ABSENT key as on, which is what
actually changed (the old code treated absent as off, so anyone who never
went through the consent form silently lost fallback).

What we deliberately did NOT build for selection:

* **Learned/persistent routing weights.** The feedback loop (below) should
  produce data first; hand-tuning quality() numbers against real win/loss
  and preference metrics is cheap and auditable. A learned router is
  premature while the metric volume is small.
* **Latency-aware scoring.** best_two_within_timelimit already gives fast
  models an edge (slow ones miss the window); double-counting latency in
  scores would bias toward terse models.
* **Cross-session offender persistence.** A backend that loops for one
  user is usually a backend+context interaction, not a global property;
  persisting strikes would punish it everywhere for one bad conversation
  and add a writer to a hot path. Session scope + metrics is enough to
  see a globally sick backend.

## 5. Choosing between answers: alternates as a product feature

best_two_within_timelimit returns (best, runner_up, both scores, both
originating calls). The runner-up becomes a side-by-side alternate answer
("🔀 See an alternate answer") ONLY when:

* the two scores are closely tied — runner_up >= 0.8 * best with both
  positive (scores_closely_tied). Given the quadratic tiers this means
  "same tier, comparable content" (e.g. the same model's truncated- vs
  full-history calls at 8000 vs 10000 base, or two same-tier backends);
  a cross-tier runner-up never qualifies, and
* it's presentable (no tool/action tokens, not a near-duplicate of the
  primary, not itself a repeat, no safety flags), and
* tool processing didn't rewrite the primary reply.

The tie requirement is what makes the feature honest: when the scorer has
a clear winner, showing a second answer is noise; when the race was
genuinely close, the user is the right tiebreaker — and their choice is
recorded (fhi_chat_answer_feedback_total{preferred=primary|alternate})
without starting an LLM turn. Only the primary is persisted; replays show
one answer.

**This is also the model-selection feedback loop**: close ties are exactly
the cases where quality() can't separate two backends, and the preference
metric accumulates evidence about which one users actually prefer. When
that data disagrees with the quality map, adjust the map.

## 6. Context management ("context shedding")

* Histories <= 20 messages go to the model verbatim (plus the full history
  variant for large-context models when it fits their window minus 8k).
* Beyond 20, the dropped prefix is summarized once per 10-message band
  (`% SUMMARIZATION_INTERVAL <= 1` — the <= 1 exists because same-role
  merging changes parity; a rare double-fire produces byte-identical
  output because _summarize_history REPLACES the previous summary block
  instead of nesting it).
* The accreted summary context is bounded (bound_summary_context, 6000
  chars, keeps the tail) — an unbounded blob was crowding the actual
  conversation out of attention, which is one of the ways replies degraded
  into replaying earlier turns.
* The per-turn "panda" summary (model-provided context for the next call)
  is stored in summary_for_next_call, capped at MAX_STORED_SUMMARIES=20;
  a missing panda gets a placeholder that a background summarization task
  swaps out transactionally.
* Summarization is hard-bounded at 90s and degrades to "keep existing
  context" — it must never stall the interactive turn.

## 7. Debuggability

Three levels, in increasing detail:

1. **Always-on INFO log line per LLM pass**: picked backend + score,
   runner-up + score + tied?, candidate count, rejected-repeat count,
   retry usage, elapsed ms. This is the production triage record.
2. **Prometheus metrics**: repeats (rejected/delivered), alternates
   offered, answer feedback, turn outcomes.
3. **Debug frames** (localStorage `fhi_chat_debug = "true"`, honored only
   for DEBUG deployments and staff accounts): per turn the server sends
   - `debug_llm_input` — the EXACT wrapped message, context summary,
     history counts, variants, state hint;
   - `debug_llm_result` — picked/runner-up models and scores, per-candidate
     score log, closely_tied, alternate_offered, rejected repeats, current
     repeat-offender strikes, retry path, allow_repeated_reply, elapsed.
   The frontend logs both to the console AND renders them as a collapsed
   "🔧 Debug" panel under the assistant message they produced, so
   debugging no longer requires devtools open before the turn.

## 8. Known gaps and roadmap (prioritized)

1. **Ship the city DB in k8s.** guess_us_state and ASN tracking soft-fail
   without FHI_GEOIP_CITY_DB (startup warns exactly this); the chart
   doesn't yet mount a geoip2fast city database. Low effort, unlocks the
   state hint in prod.
2. **Streaming responses.** The infra streams status heartbeats but final
   answers arrive whole. Token streaming from the winning backend would
   cut perceived latency drastically — but it collides with fan-out
   scoring (you can't score a stream you haven't finished). A pragmatic
   shape: keep the fan-out for the first N seconds, then stream the
   leader's remainder. Biggest UX win, medium-large effort.
3. **Legacy backend prompt shape.** RemoteHealthInsurance
   (supports_system=False) receives the system prompt folded into the
   final user message. It's quality 101 so it rarely wins, but its calls
   burn capacity; consider dropping it from the chat pool entirely.
4. **Retry-button double turns.** The client retry sends the same message
   again; the server merges duplicates at persist time (serial + deduped)
   but the second LLM turn still runs. An in-flight turn-id (client echoes
   it, server drops re-submits of a live turn) would make retry free.
5. **Per-model win/lose metrics.** The debug frame reports the picked
   model; promote that to a bounded-cardinality counter
   (fhi_chat_model_wins_total{model}) so the quality map can be tuned
   from dashboards, not log greps. (Deliberately deferred: needs a label
   allowlist to keep cardinality bounded.)
6. **Summarization model diversity.** summarize_chat_history routes to one
   summarizer; a bad summary quietly poisons every later turn's context.
   Cheap guard: score summaries with the repetition detector before
   storing (a summary that mostly repeats the raw history is fine; one
   that repeats the model's last REPLY is the poison case).
7. **Evaluation harness.** The loop bug shipped because nothing exercised
   multi-turn conversations against scripted "sticky" backends. The test
   suite now covers the ladder with RecordingChatModel; a nightly
   scripted-conversation eval against the real internal backends (no
   users) would catch regressions the unit layer can't.

## 9. Invariants to preserve (change these knowingly or not at all)

* The scorer may only hard-reject (-inf) candidates that are REPEATS or
  invalid — never for style. The last-resort path must stay finite.
* allow_repeated_reply / repeat exemptions are derived from the RAW user
  message, never from the wrapped prompt (wrapper text contains the word
  "repeat").
* The alternate answer is ephemeral: never persisted, never replayed.
* The state hint is transient and UNCONFIRMED: injected per turn, never
  stored on the chat.
* Summarization and geo lookups soft-fail; nothing on the turn path is
  allowed to hard-block the reply.
* Every wait on the turn path has an explicit bound that fits inside
  FHI_CHAT_TURN_BUDGET.
* `user_requested_repeat` is the master switch that disables the whole
  ladder, so it must match an explicit REQUEST ("repeat that", "say that
  again"), never the topic. "repeat MRI", "repeat colonoscopy", "repeat
  prescription" and "repeat denial" are ordinary vocabulary here, and a bare
  `\brepeat\b` turned every one of those conversations into an unprotected
  one. Erring toward not-matching is the safe direction.
* `is_canned_reply` must require the WHOLE mandated block, not a marker
  phrase: the system prompt tells the model to link the Medicaid FAQ on any
  work-requirements answer, so a marker-only test exempted every ordinary
  Medicaid reply — including the looping ones.
* Anything that screens a reply for tool calls must use
  `patterns.contains_tool_call` (the handlers' own flags). Several tool
  patterns are `^...$`-anchored, so a flag-less `re.search` only matches a
  call at the very start of a reply.
* Compare like with like: a raw generation still carries its trailing
  `🐼<summary>` while history stores the split answer. Comparing the two
  shapes put a byte-identical repeat at ~0.76 similarity, under threshold.
* Externals may appear in the primary fan-out OR the retry fallback list,
  never both — `build_retry_calls` iterates both, so a backend in each got
  four identical paid requests per retry.
* Internal (LLM-context-only) history entries are identified by their
  `internal` flag, not by a content prefix a user could type, and every view
  of the history — WebSocket replay, REST listing, chat titles, previews —
  must filter them the same way.

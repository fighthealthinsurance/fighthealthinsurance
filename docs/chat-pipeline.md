# Chat pipeline: architecture, failure modes, and roadmap

A detailed think-through of the chat stack as of the loop-prevention work
(PR #934). Written to be the reference for "why does chat behave this way"
questions: the end-to-end flow, the scoring math with real numbers, every
anti-loop defense and where it sits, how models are chosen, what we
deliberately did not build, and the prioritized list of what should come
next.

## 1. End-to-end flow of one turn

```text
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
  │  alternate answer: the best CLOSELY TIED, presentable candidate from a
  │    different model than the winner, else the runner-up under the same
  │    rules
  │  debug_llm_input / debug_llm_result frames when debug is on
  │  tool handlers (appeal, prior auth, medicaid, pubmed, doc fetcher, ...)
  │    -- recursive tools re-enter _call_llm_with_actions at depth+1
  ▼
persistence (chat_persistence.py): transactional turn persist with
  tail-dedup, summary list capped at 20; panda-summary placeholder swap
  happens in the background when the model omitted its summary
  ▼
ws frames out: status heartbeats, content (+ alternate_content and
  turn_id), metrics
  ▼
turn record (chat/turn_record.py): one ChatTurn row per turn counted in
  fhi_chat_turns_total, with the counted outcome, written after the reply
  frame (or the error frame); a turn cancelled after it was counted gets
  its row from a thread of its own, with a bounded wait
  ▼
shadow scoring (chat/shadow_scoring.py, off by default): a background
  task, started after the row, that scores the delivered reply with
  TypeSafe's Jev for the staff dashboard (see §5)
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

The strongest healthy fhi backend (alpha, at 210) leads the fan-out and
is listed twice; every other backend is listed once (section 4). The
lead's two entries start from the same base score, so content signals
alone decide between them, and alpha's truncated-history calls (8820) sit
above those of the May fine-tune (NewRemoteInternal, 8000).

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

* the lead fhi backend, doubled (redundancy against a slow pod). The lead
  is the strongest fhi backend by quality that follows instructions and
  looks healthy, with equal quality going to the name that sorts first so
  every pod picks the same one. The lead is chosen per backend, not per
  name: when two backends share a name (alpha and the May fine-tune set to
  the same model path), only the stronger leads and the other takes an
  ordinary internal slot. Each step fails open like the other
  filters: with every fhi backend marked down the strongest still leads.
  With alpha and the May fine-tune both registered, alpha leads with two
  calls and the May fine-tune gets one. The lead used to be whichever fhi
  name sorted first, which put the May fine-tune in front of the stronger
  alpha,
* the strongest 6 *available* internal backends other than the lead,
  quality-sorted (an older cost-sort quietly picked the cheapest end). The
  lead is left out here so it gets exactly two calls,
* when external models are enabled: the best <= 3 externals
  (quality-sorted, health-gated) now join the PRIMARY
  fan-out. The separate fallback list then carries only externals NOT
  already there — normally none, since the retry pass fans out over both
  lists and a backend in each would get four identical requests.

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

### The routing policy ("ours first")

Chat's outside models come from a roster, `FHI_CHAT_OUTSIDE_MODELS`, in
order (GPT-5.5 on Azure, then DeepInfra's Mistral-Small 3.2, GLM-5.3 Flash,
DeepSeek V4.1 Flash and Qwen3.8-2.4T). At most three are asked, skipping any
that is down or whose budget is spent (`ml/spend.py` counts spend as the
calls happen). On `FHI_CHAT_EXPLORE_RATE` (20%) of turns the second place
goes to a model further down, so every model keeps being asked often enough
for its place to be learned.

A policy row (`ChatRoutingPolicy`, written once a day by `ml/chat_policy.py`
from a week of ChatTurn metadata) can tune that in three ways:

* **Learn the order.** A model asked on at least 30 turns moves among the
  places such models hold in the roster, by how often its answer was
  delivered. A model with fewer turns keeps its place, so the roster is
  the prior until the data says otherwise.
* **Leave out outside models that do not win.** The top healthy outside
  model is always kept. Any other is left out once enough turns asked it
  and it never won, except while our own models fail often.
* **Give our models a head start.** With a hold above 0
  (`FHI_CHAT_EXTERNAL_HOLD_SECONDS`, 8s, at most 15s) the primary and tool
  passes start our own models' calls first and hold the outside ones back.
  They start when the hold passes with nothing usable, or at once when
  every call of ours has failed or been rejected. If one of ours answers
  usably first they are never sent. The race's windows still run from its
  start, so a turn never takes longer than it would without the hold. The
  retry pass is never staged.

Spending caps are not in the policy: the router skips a provider whose
budget is spent, and the transport refuses to send to one, as it happens.

Guard rails: a policy can only reorder and narrow the roster, never add a
model, and a person's choice to keep chat on our models always wins. When
none of our models is selectable the router sets the whole policy aside.
Chat follows the newest row only while `FHI_CHAT_POLICY_APPLY` is on (off by
default) and the row is newer than `FHI_CHAT_POLICY_MAX_AGE_MINUTES` (36
hours, so one missed daily run does not drop it);
an empty table or an unreadable row gives the default, which is the
behaviour described above. A turn never waits on the database for the
policy: it uses the row cached in its process, and a turn that finds the
cache over a minute old starts a refresh on a thread of its own, with its
own connection and a 0.5s statement timeout on PostgreSQL. Only one
refresh runs at a time per process, and turns meanwhile use the cached
row (the default before any row has been read). A refresh that fails
keeps the cached row, which is still followed only while it is fresh.
Rows come from the `chat-routing-policy` Temporal Schedule, which runs
`ChatRoutingPolicyWorkflow` once a day on its own queue in the
appeal-worker pods while `TEMPORAL_ENABLED` and
`TEMPORAL_CHAT_POLICY_ENABLED` are on (off by default; see
`k8s/temporal/README.md`), or from `manage.py compute_chat_policy` (by hand
or from a CronJob). Both write the same row, and its `source` says which;
a Schedule run's row also carries its run id, so each run writes one row
at most, and the command writes a new row every time. The Schedule's
history holds the window, the run id and the row id only, and Temporal
is never on the turn path: if runs stop, the newest row passes the age
limit above and chat routes by the default. Rows are shown on the staff ML
Model Usage Dashboard whether or not chat follows them. Rows are never
edited; rows older than 30 days are deleted after a new one is written. Each
ChatTurn row records how its primary pass started the outside models
(`external_start`: immediate, after_delay, early, after_check or skipped)
and the hold it used; held-back calls that were never sent have the status
"skipped".

### The reply check (Jev decides whether the outside models are needed)

The cascade TypeSafe documents (docs.typesafe.ai/cookbooks/sde_cascade):
our models answer first, TypeSafe's Jev checks the answer, and the paid
outside models are asked only when the check does not pass.
`chat/reply_gate.py` runs it inside the primary pass's staged fan-out;
`ml/chat_gate.py` holds our own checks, the questions and the decision
rule.

* **When.** Only when all of these hold: `FHI_CHAT_JEV_GATE_ENABLED` is on
  (off by default, forced off in every test configuration), the TypeSafe
  key is set, the person allowed outside models (Jev reads the text), the
  message was typed (not a document upload or a stored long paste), one of
  our models is selectable, TypeSafe's chat budget (`ml/spend.py`) allows a
  request, and the pass has outside calls to hold back and calls of ours to
  start first. With the budget spent the turn routes by our own rules, as
  with the check off. It does not depend on
  `FHI_CHAT_POLICY_APPLY`. Tool passes and the retry pass are never
  checked.
* **The hold.** The outside calls wait until our first usable reply is
  judged, or `FHI_CHAT_JEV_GATE_MAX_WAIT_SECONDS` (8s) passes, whichever
  comes first; when the routing policy's delay is longer, that is the hold.
  They start at once when ours all fail.
* **Our own checks first.** Before anything is sent, our first usable
  reply (as cleaned for delivery) must pass the rule the retry uses
  (`chat/retry_handler.should_retry_response`): at least
  `MIN_RESPONSE_LENGTH` (5) characters, and no promised outcome
  (`safety_filters.detect_false_promises`). A reply that fails them fails
  the check with the scorer `fhi/local-checks-1`, is never sent to
  TypeSafe, and leaves the health row alone. These requirements hold
  whether or not Jev can be reached. An empty reply is not judged at all:
  it is recorded as skipped, like a reply carrying a tool call.
* **The check.** One request per turn: the person's latest message and our
  first usable reply (as cleaned for delivery), redacted as letter scoring
  redacts with the identifiers `chat/redaction.py` collects for the chat's
  accounts and its linked appeals and prior authorization requests (if
  that list cannot be read, nothing is sent), with four yes/no questions
  about the reply: does it respond to what the message asks or says; does
  it state a coverage or eligibility outcome as a settled fact; does it ask
  for something the message already gives; does it promise or guarantee a
  result (our own false-promise rule, asked of Jev too). A fifth asks about
  the message: is it a crucial moment (a deadline, a denial decision, an
  appeal's next step, or whether something is covered)? The request is
  counted against TypeSafe's chat budget. A reply carrying a tool call or
  the data-deletion handoff is not sent; the outside calls start instead.
* **Three tiers.** **Fail**: "responds" below
  `FHI_CHAT_JEV_GATE_MIN_ANSWERS` (0.7), or any problem answer at or above
  `FHI_CHAT_JEV_GATE_MAX_PROBLEM` (0.3). **Pass**: "responds" at least
  `FHI_CHAT_JEV_GATE_CLEAR_ANSWERS` (0.85) and every problem answer below
  `FHI_CHAT_JEV_GATE_CLEAR_PROBLEM` (0.15). **Borderline**: anything
  between.
* **Database work.** The identifier lookup and the health note after the
  turn each run on a thread of their own with their own connection, closed
  afterwards, inside a transaction with a statement timeout on PostgreSQL
  (`chat/isolated_db.py`), never on the chat's database executor. The
  lookup shares the check's 1.5s; a stuck one is a timeout, nothing is
  sent, and the rest of the turn does not wait for it. The turn waits at
  most 1s for the health note. At most 8 of each kind run at once per
  process; past that the check sends nothing, or the note is left out.
* **Pass:** the primary pass never sends the outside calls (their
  coroutines are closed) and the usual scoring picks among our models'
  answers. The retry, which runs only when our own checks reject the
  reply (empty, too short or a false promise), may still ask them; the
  dashboard does not count such a turn as one the check kept them from.
  **Anything else**
  (a fail, an error, an HTTP error, an answer we cannot read, or no answer
  within `FHI_CHAT_JEV_GATE_TIMEOUT_SECONDS`, 1.5s): the outside calls
  start at once and the usual scoring picks among everything. The check
  never holds the reply back, and the race's windows still run from its
  start.
* **Borderline: Jev ranks.** The outside calls start at once (the roster
  asks up to three, so at least two whenever two are up and in budget).
  When the race is over, a second request sends every deliverable
  candidate, ours included, one per reply the person would see and at most
  four, labelled THE REPLY 1, 2 and so on, with the four reply questions
  each. Each candidate's quality is "responds" times one minus its largest
  problem answer, and the best is delivered (ties go to the race's order).
  The ranking has `FHI_CHAT_JEV_RANK_TIMEOUT_SECONDS` (3s); on an error or a
  timeout the race's pick stands. Each ranked call keeps its quality as
  `jev` in `ChatTurn.calls`, and the routing policy orders the outside
  models by those once two of them have 30 each (`_choose_order`).
* **Crucial moments: a side-by-side.** When Jev's crucial answer is at
  least `FHI_CHAT_JEV_CRUCIAL_MIN` (0.5) and the chat still has a
  side-by-side left (`FHI_CHAT_SIDE_BY_SIDES_PER_CHAT`, 2), the pass's
  reserved call starts too: one call (truncated history) to
  `FHI_CHAT_SIDE_BY_SIDE_MODEL` (Kimi-K3), built only for the checked
  primary pass and never asked by the retry. The race starts a reserved
  call only when the check names it (`utils.CheckVerdict`), never after the
  hold runs out, so an error, a timeout or an ordinary turn never sends it.
  The side-by-side is Jev's top two after a ranking, else the reply beside
  the side-by-side model's answer, else beside the best answer from another
  model. While Jev answers the check, side-by-sides are offered for crucial
  moments only; with no answer from Jev, the closely-tied rule (section 5)
  still applies.
* **A failed reply is demoted.** After a fail, from Jev or from our own
  checks (never an error, a timeout or a reply that was not judged), and
  while `FHI_CHAT_JEV_GATE_DEMOTE_FAILED` is on (the default; pinned on in every
  test configuration), the judged reply, and any reply with the same
  text whatever its context summary, ranks one point below the best
  outside answer that has arrived and could be delivered (not empty, too
  short or a false promise), or below the outside calls' base score while
  none has. Our models' base
  score (about 8000 against about 1900 for outside ones) would otherwise
  keep the failed reply in front. Our other replies keep their scores. The
  demoted reply is never the runner-up or the side-by-side alternate, and
  it is still delivered when nothing else usable arrives.
* **Recorded** on the ChatTurn row: `gate_used`, `gate_outcome` (pass,
  borderline, fail, error, timeout, or skipped when nothing was judged),
  Jev's four reply answers and `gate_crucial`, the ranking's
  `rank_outcome`, `rank_ms`, `rank_count` and `rank_changed` (its pick
  replaced the race's), `alternate_reason` (tied or crucial), `gate_scorer` (the model TypeSafe reports plus the rubric
  version, or `fhi/local-checks-1` when our own checks failed the reply
  before Jev was asked), `gate_ms`, `gate_model` (whose reply was judged),
  `gate_demoted` (a fail demoted it) and `gate_demoted_delivered` (the
  primary pass still delivered it). The
  outcome also goes to the `typesafe-chat-gate` ExternalServiceHealth row
  after the reply is sent. The staff usage dashboard shows the counts
  (and how many fails our own checks decided without asking Jev), how
  often the outside models were never sent because the check passed, and,
  after Jev failed the reply, how often an outside model's answer was
  delivered and how often our demoted reply was delivered because nothing
  else usable arrived. Those last two leave out the fails our own checks
  decided, so they stay a check on Jev's questions and thresholds.

What we deliberately did NOT build for selection:

* **A learned router beyond the outside order.** The policy reorders only
  the outside models, from delivered answers, and only once a model has
  enough turns; internal quality() numbers stay hand-tuned, which is cheap
  and auditable while the metric volume is small.
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
originating calls), and the top-level pass also keeps every result that
completed. A side-by-side alternate answer ("🔀 See an alternate answer")
is offered ONLY when a candidate:

* is closely tied with the winner: candidate >= 0.8 * best with both
  positive (scores_closely_tied). Given the quadratic tiers this means
  "same tier, comparable content" (e.g. the same model's truncated- vs
  full-history calls at 8000 vs 10000 base, or two same-tier backends);
  a cross-tier candidate never qualifies, and
* is presentable (no tool/action tokens, not a near-duplicate of the
  primary, not itself a repeat, no safety flags), and
* tool processing didn't rewrite the primary reply, and no retry replaced
  the primary pass's winner, and
* it is not a reply the reply check failed and demoted, and
* Jev did not answer the turn's reply check: when it did, a side-by-side is
  offered only for a crucial moment (section 4, the reply check), and the
  row's `alternate_reason` says which rule offered it.

Among the candidates that qualify, one from a DIFFERENT model than the
winner comes first (pick_side_by_side_alternate): the pick is meant to be
model versus model. Only when no other model's candidate qualifies is the
plain runner-up offered, which is usually the winner's own other call.

The tie requirement is what makes the feature honest: when the scorer has
a clear winner, showing a second answer is noise; when the race was
genuinely close, the user is the right tiebreaker — and their choice is
recorded (fhi_chat_answer_feedback_total{preferred=primary|alternate})
without starting an LLM turn. The answer frame carries the turn's
`turn_id` whenever it carries an alternate; the client echoes it in
`answer_feedback`, and the pick is stored on that turn's ChatTurn row. The
store only takes a pick for a turn of the socket's own chat that offered an
alternate and has no pick yet, so the first pick wins. Only the primary is
persisted; replays show one answer.

**This is also the model-selection feedback loop**: close ties are exactly
the cases where quality() can't separate two backends, and the preference
data accumulates evidence about which one users actually prefer. The staff
ML Model Usage Dashboard shows it per model and per pair of models. When
that data disagrees with the quality map, adjust the map.

### Shadow scores (TypeSafe Jev)

With `TYPESAFE_CHAT_SHADOW_ENABLED` and `TYPESAFE_API_KEY` set, and only in
chats where the person allowed outside models, each delivered turn gets a
background task (chat/shadow_scoring.py) that asks TypeSafe's Jev four
questions about the delivered reply and about the turn's second answer
(the alternate when one was shown, otherwise the runner-up), each read
against the person's message: does it answer what was asked (0 to 2),
does it state a coverage or eligibility outcome as fact, does it ask for
something the message already gave, and does it promise or guarantee a
result (the last three 0 to 1). The last is our own false-promise rule
(chat/safety_filters.detect_false_promises) put as a question, and the
rule itself stays in place. ml/chat_shadow.py holds the rubric, and folds
the four into one composite score for the agreement table.

* It starts after the reply frame has gone out and the ChatTurn row
  exists, and nothing waits for it: the scores land on that row later.
  At most 8 run per process (with up to 16 database threads between
  them, two each), each request under TYPESAFE_TIMEOUT_SECONDS,
  and the whole job has a bound of its own.
* Its database work (the identifier lookup, the health note and the score
  write) runs through chat/isolated_db.py: on a thread with its own
  connection, never on the chat's thread-sensitive executor, with a bounded
  wait and, on PostgreSQL, a statement timeout. A stuck query there cannot
  hold up the chat's next ORM call.
* The texts are redacted the way the letter scorer redacts
  (chat/redaction.py): the identifiers held for the chat's accounts and for
  the appeals and prior authorization requests linked to it, including each
  appeal's denial exactly as letter scoring collects it (patient, claim,
  plan and member identifiers among them), plus emails and phone numbers.
  If that lookup fails, nothing is sent. Only the scores, a scorer string
  (the model that answered and the rubric version) and an outcome (scored,
  failed, timeout) are stored; the texts are never stored or logged.
* Nothing starts for a document upload or a stored long paste, for a reply
  a tool rewrote, or for the canned data-deletion reply.
* It fails closed: any error, timeout or unexpected answer stores no
  scores, and the outcome goes on the `typesafe-chat` ExternalServiceHealth
  row.

Nothing uses the scores to pick a reply. The dashboard shows per-model
means and an agreement table: of the side-by-side picks where both answers
were scored, how often the answer Jev scored higher is the one the person
picked. That is the check to run before the scores inform routing. Both
use one exact scorer string, the newest in the window, named on the page;
turns scored by another Jev version or rubric are counted, never averaged
in, as for draft quality.

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
3. **ChatTurn rows** (one per turn that reached the models and was
   counted in fhi_chat_turns_total, in the admin and on the staff ML Model
   Usage Dashboard): the backends asked, each call's model, pass, history
   kind, status, time and score, the model whose reply was delivered (a
   tool follow-up's pick when one wrote the reply) and the first pass's
   pick and runner-up, retry and tool use, how the outside models were
   started (§4), the alternate offered, the person's pick and any shadow
   scores (§5). Metadata only (see §9). A call that answered was scored
   (scored, repeat or empty) or, when its pass stopped comparing answers
   first, is unscored; either way it keeps its time. Only a call still
   running when its pass stopped waiting is late, with no time, and a
   held-back outside call that was never sent is skipped. An exception
   escaping a turn after the models were asked counts it failed, in the row
   and the metric alike; a turn cancelled before it was counted gets
   neither.
4. **Debug frames** (localStorage `fhi_chat_debug = "true"`, honored only
   for DEBUG deployments and staff accounts): per turn the server sends
   - `debug_llm_input` — the EXACT wrapped message, context summary,
     history counts, variants, state hint;
   - `debug_llm_result` — picked/runner-up models and scores, per-candidate
     score log, closely_tied, alternate_candidate, rejected repeats, current
     repeat-offender strikes, retry path, allow_repeated_reply, elapsed.
   The frontend logs both to the console AND renders them as a collapsed
   "🔧 Debug" panel under the assistant message they produced, so
   debugging no longer requires devtools open before the turn.

## 8. Known gaps and roadmap (prioritized)

1. **Streaming responses.** The infra streams status heartbeats but final
   answers arrive whole. Token streaming from the winning backend would
   cut perceived latency drastically — but it collides with fan-out
   scoring (you can't score a stream you haven't finished). A pragmatic
   shape: keep the fan-out for the first N seconds, then stream the
   leader's remainder. Biggest UX win, medium-large effort.
2. **Legacy backend prompt shape.** RemoteHealthInsurance
   (supports_system=False) receives the system prompt folded into the
   final user message. It's quality 101 so it rarely wins, but its calls
   burn capacity; consider dropping it from the chat pool entirely.
3. **Retry-button double turns.** The client retry sends the same message
   again; the server merges duplicates at persist time (serial + deduped)
   but the second LLM turn still runs. An in-flight turn-id (client echoes
   it, server drops re-submits of a live turn) would make retry free.
4. **Per-model win/lose metrics.** Per-model wins, calls and side-by-side
   picks now live in ChatTurn rows and on the staff usage dashboard. A
   Prometheus counter (fhi_chat_model_wins_total{model}) is still deferred:
   it needs a label allowlist to keep cardinality bounded.
5. **Summarization model diversity.** summarize_chat_history routes to one
   summarizer; a bad summary quietly poisons every later turn's context.
   Cheap guard: score summaries with the repetition detector before
   storing (a summary that mostly repeats the raw history is fine; one
   that repeats the model's last REPLY is the poison case).
6. **Evaluation harness.** The loop bug shipped because nothing exercised
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
* ChatTurn holds metadata only: model labels, backend descriptors,
  statuses, times, scores (including shadow scores and their scorer
  string) and enum values. Never message, reply, summary,
  history, context, state hint or document text, and exceptions by class
  name only. Its chat FK cascades and is non-nullable, so it goes with the
  chat (including delete-my-data).
* The state hint is transient and UNCONFIRMED: injected per turn, never
  stored on the chat.
* Summarization and geo lookups soft-fail; nothing on the turn path is
  allowed to hard-block the reply.
* Shadow scoring stays off the turn path: it starts only after the reply
  was delivered, never delays it, and sends nothing outside unless the
  person allowed outside models for the chat.
* Every wait on the turn path has an explicit bound that fits inside
  FHI_CHAT_TURN_BUDGET.
* The routing policy only narrows: it never adds a model, never asks an
  outside model without the person's consent, and is set aside when none
  of our models is selectable. A turn never waits on its read, which runs
  off the chat's database executor, and a held-back start never makes a
  race run past its windows.
* The reply check sends text to TypeSafe only with the person's consent to
  outside models, and only while its own switch and the key are set. It
  fails open: anything but a pass starts the outside models, and nothing
  it waits on runs past the race's windows. Our own checks run before it
  and send nothing, so a reply they reject fails even when Jev is
  unreachable. Only its numbers, outcome, scorer, time and the judged
  model's label are kept.
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

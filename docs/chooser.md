# The chooser (Appeal Arena)

`/chooser/` is where visitors judge several AI drafts side by side on a
synthetic case, and `/chooser/rankings/` is the public scoreboard built from
those votes. The votes train the selector that picks drafts in the real
appeal flow, and rank the model backends against each other.

## Where they are linked from

The rankings page is staged behind `ARENA_RANKINGS_PAGE_ENABLED` (off by
default; Dev and the test configurations turn it on): while it is off the
page and its JSON serve 404 for every method, every link below to it is
hidden, it is out of the sitemap and `llms.txt`, its markdown twin is gone,
and the Arena's standings panel (which learns of the 404 from the JSON)
shows nothing. The JSON is served with `Cache-Control: public` for
`PUBLIC_RANKINGS_CACHE_SECONDS`, so a browser or shared cache can keep
answering with the last copy for up to that long (15 minutes by default)
after the flag goes off. The Arena itself is not behind the flag. One function reads
it, `utils.arena_rankings_page_enabled`, and the templates get it as
`arena_rankings_enabled` from `context_processors.arena_rankings_context`.

Both pages are reachable without knowing the URL: the Resources menu
(`Appeal Arena`, `Which AI Wins: Rankings`) and the footer on every page, the
`Which AI writes the best appeal?` section on the home page, the
`Which Model Writes the Best Appeal?` section of About our AI, the Help
Patients section of How to Help, and the staff dashboard. Both are in the
static sitemap; the rankings page has a markdown twin and both are listed in
`llms.txt` (the Arena by its own URL, since it is a React app with nothing
to read without scripts: `agent_docs.NO_TWIN_URL_NAMES`). The Arena's own
landing and vote-confirmation screens fetch `/chooser/rankings/?format=json`
and show the standings (each table's leader and its top three), with a link
to the full page; without the JSON they show the link alone.

## What a visitor sees

- **The case.** For an appeal task: the treatment and diagnosis, a fictional
  insurer and denial reason, a short list of *case facts* (age range,
  symptom duration, what was tried, what the clinician documented), *what
  the patient told us* (the app's questions for the case and the made-up
  patient's answers) and the *evidence on file* (PubMed articles, with links
  and, when one is on file, a short summary, and on newer cases up to three
  web pages a search found, marked "web page", with the excerpt the search
  returned). Everything shown is exactly
  what every model-written draft was given; the synthesized draft, which
  combines them, gets the treatment, diagnosis, denial reason and the drafts,
  not the facts, answers or evidence. For a chat task: the conversation and
  the question the drafts answer.
- **Two referee aids**, on top of the case and neither given to the drafts:
  - *The reviewer's verdict* on each study on file: a badge (supports the
    treatment, mixed, argues against, off topic, unclear) from Jev's
    research judgment of the article for the case's treatment and condition
    (`ml/research_judging.py`, the same judgments that order a real appeal's
    articles), or, where Jev gave none, our own model's answer to the same
    questions, with the three probabilities behind it under "What it
    found". The page does not say which of the two judged a study. A study
    nobody has judged has no badge. The page says the drafts
    saw the summaries, not the verdicts, and calls it a hint, not a ruling.
  - *The citation check* under each draft: every PMID the draft cites (as
    "PMID 12345678", "PubMed ID 12345678" or a PubMed link) is matched to
    the evidence on file and shown as a chip that opens PubMed, and marked
    in the draft's text. **Not on file** (red): the drafts were told to cite
    only the evidence listed, and the voter cannot check it here; the chip
    opens PubMed so they can. **Argues against** or **off topic** (orange):
    on file, but the reviewer read the study that way, so the voter should
    check what the draft claims it found. **Mixed** (yellow) and **on file**
    (green) otherwise. An appeal draft is also checked for details the case
    never gives, a calendar date or a member, claim or similar number
    (a fill-in such as "[Date]" is fine), shown as an orange hint. Chat
    replies get neither check: a chat reply may rightly name a date. All of
    this runs in the browser from what is already on the page; nothing is
    sent anywhere, and the vote carries only the voter's flags.
- **The drafts**, blind. The API sends no model name, synthesized flag or
  metadata (`ChooserCandidateSerializer`), and no row id or
  `candidate_index` either: both run in generation order (internal model
  first, synthesized last). Each draft goes out as an opaque token
  (`chooser_tokens.candidate_token`, the id encrypted with a fresh nonce)
  that the vote endpoint resolves back (and takes nothing else: accepting
  plain ids would let a client learn a token's id by guessing), and the
  drafts are served in random order, so a vote is for the letter, not the
  brand. The combined draft is blind like the rest: a chat draft's lookups
  are served under one caption for every draft, and the mark the combined
  draft's lookups are stored with (`from_drafts`: it read the drafts'
  answers, not the lookups) is left off what is served
  (`chooser_tasks.served_tool_calls`).
- **Referee checklist.** Judge in order: truthful, responsive, useful, and
  clear. For appeals, that means answering the insurer's actual reason with
  case-specific reasons and a clear request; for chat, answering the person's
  question with a safe, useful next step. Length and confidence are not quality
  signals. "Makes things up" is only for fabricated facts, sources, or case
  details, not weak writing. Citation chips, automated verdicts, and lookup
  results are aids to verification, not verdicts to follow mechanically.
  Reject all only when confident none is usable (not merely imperfect); skip
  when unable to judge, which records a different outcome.
- **Ties without a separate mode.** Every draft can be selected directly. One
  selection is a winner; two or more are equally good winners. The vote
  carries multiple selections as `tied_candidate_ids` (in display order, none
  flagged) with `chosen_candidate_id` set to the first of them, so a tie vote
  still has a chosen candidate. Counting splits the vote evenly between the
  tied drafts (`ChooserVote.shares`: half a win each for a two-way tie).
- **No usable drafts.** "These are all bad" submits `all_bad=true` with no
  chosen candidate or tie. This is a completed review, not a skip: every
  presented draft counts as shown but receives no winning credit. A database
  constraint keeps no-winner reviews distinct from ordinary votes.
- **Why (optional).** A one-sentence rationale of up to 500 characters can
  accompany any outcome. It is stored on the vote and included only in the
  staff export, never in the public rankings.
- **A flag per draft** ("Makes things up"). Flags ride along with the vote as
  `flagged_candidate_ids`; a flagged draft cannot be the chosen one, and the
  server rejects a vote that tries.
- A per-browser scoreboard (rounds judged, fouls called) in `localStorage`,
  with encouragement at review milestones; nothing about it is sent anywhere.
  Signed-in judges additionally see their account-wide number of completed
  reviews across both Arena types, on the landing, voting and confirmation
  screens. The page seeds that count and the task and vote APIs refresh it.

## Evaluator accounts

Staff can use **Add Evaluators Account** on the staff dashboard
(`/timbit/help/evaluators/add`) to create an ordinary Django user for repeated
judging. The account is active but has `is_staff=False`,
`is_superuser=False`, no individual permissions, and membership only in the
permissionless `evaluators` group. It therefore cannot open the staff
dashboard, other staff views, or Django admin. Anonymous and non-staff users
cannot use the creation action.

The unlinked `/evaluators/login/` page is marked `noindex` and accepts only
active members of that group who are not staff or superusers; other accounts
get the same generic invalid-login response as bad credentials. A successful
login goes straight to `/chooser/`, where the account's review count includes
all of its previous votes. The login grants no extra privileges and does not
change the Arena's existing captcha requirement.

## Paid reviewers (RentAHuman)

Volunteers come and go, so the Arena can also hire judges by the batch
through [RentAHuman](https://rentahuman.ai), a marketplace where software
posts paid tasks for people (`paid_reviews.py`). The cases are synthetic,
so a stranger sees no one's health information. One batch is one
RentAHuman bounty (`PaidReviewBounty`):

1. **Post it.** `python manage.py post_paid_review_bounty --spots 10
   --price 5 --reviews 10` (the defaults) prints the cost (workers' pay
   plus RentAHuman's 18% fee: $59 for 100 reviews, about $0.59 a review,
   or about $10 an hour for the worker at three minutes a review) and
   RentAHuman's dry-run preview. Add `--post` to post it for real; $5 a
   worker is RentAHuman's minimum. The bounty is a remote "computer gig"
   with text evidence and RentAHuman's AI screening on (`autoAccept`:
   qualified applicants are accepted, clear mismatches declined, the rest
   left for you), and its description links the join URL,
   `/chooser/paid/<join_token>/`. If RentAHuman holds it as
   `pending_deposit`, top up the wallet there.
2. **The worker judges.** The join link ties their browser session to a
   `PaidReviewSession` and opens the Arena, which is unchanged except for a
   progress banner. Each vote that session casts is tagged
   (`ChooserVote.paid_review`), so paid votes can be studied or weighted
   apart from volunteers'. A paid vote counts toward the run when it was
   timed by the server at `PAID_REVIEW_MIN_SECONDS` or more, carries a
   reason of `PAID_REVIEW_MIN_RATIONALE_CHARS` or more, and did not come
   from an automated browser; the vote answer says why one did not count.
   Any outcome counts (a winner, a tie, all bad): the floor stops
   click-through, not disagreement. Every vote, counted or not, is recorded
   like any other.
3. **The code.** After `reviews_required` counted votes the banner shows a
   completion code (`FHI-XXXX-XXXX`, single use), which the worker pastes
   as their submission.
4. **Checking it.** RentAHuman's `submission.submitted` event, at
   `/chooser/paid/webhook/rentahuman/` (signed with
   `RENTAHUMAN_WEBHOOK_SECRET`), has the submission fetched from the API
   and checked. A submission RentAHuman already shows as reviewed is left
   alone, so repeated deliveries are harmless; the event id is not used to
   deduplicate, since a resubmission after a redo can carry the same one.
   A code issued for that
   bounty and not yet claimed is claimed and the submission approved;
   anything else gets a redo request saying what was wrong.
   `python manage.py sync_paid_reviews` does the same for every pending
   submission, as a poll behind the webhook or instead of it.
5. **Paying.** Approving moves no money. Release the escrow on RentAHuman
   (its guidance is that agents release payment only when the account
   owner says so). Close a batch's join link with
   `post_paid_review_bounty --close <id>` or in the admin. That only stops
   new workers joining: runs already started keep counting votes, keep
   showing an earned code, and are still answered by `sync_paid_reviews`.

Setting up the account: create an API key at
rentahuman.ai/account/api-keys and set `RENTAHUMAN_API_KEY`. Then
`python manage.py rentahuman_setup` shows the wallet and webhooks;
`--deposit 60` prints a Stripe Checkout link to fund the wallet (nothing is
charged until someone pays there); `--webhook` registers this site's
webhook for `submission.submitted` and prints the `whsec_...` secret once,
for `RENTAHUMAN_WEBHOOK_SECRET` (register it after the route is deployed);
`--test-webhook <endpoint id>` sends RentAHuman's test event. Settings: `RENTAHUMAN_API_KEY`, `RENTAHUMAN_WEBHOOK_SECRET`,
`RENTAHUMAN_API_BASE`, `PAID_REVIEW_SITE_ORIGIN` (where the join link
points), `PAID_REVIEW_MIN_SECONDS` (60) and
`PAID_REVIEW_MIN_RATIONALE_CHARS` (25). Tests blank the key and secret.

## The door: "I'm not a robot"

The Arena starts with the same reCAPTCHA checkbox as the site's other
public forms, on the same terms (`forms.ReCaptchaOptionalMixin`: both
`RECAPTCHA_PUBLIC_KEY` and `RECAPTCHA_PRIVATE_KEY` set and
`RECAPTCHA_TESTING` off; Dev and the tests have it off). The page carries
the site key on `#chooser-root` only then, the app draws the box on its
landing beside the terms checkbox, and the tick's token rides on the first
next-task request in the `X-Recaptcha-Response` header. The server checks
it through `forms.ChooserCaptchaForm` (so Google's answers are read by
`CheckboxReCaptchaField`, like everywhere else), marks the session, and
from then on asks for nothing: votes and skips need the mark, not a token.
A request without the mark gets a 403 `captcha_required` with the field's
`code` (`required`, `captcha_expired`, `captcha_error`, `captcha_invalid`)
and message; the app goes back to its landing, clears the box and shows
the message. Each token sent is a call to Google, so an address gets
`CAPTCHA_MAX_CHECKS` checks per `CAPTCHA_CHECK_WINDOW_SECONDS` (20 per ten
minutes, counted in the shared cache) before it is told to wait
(`captcha_rate_limited`). A session that has passed keeps its mark: the
page carries `data-captcha-passed` on `#chooser-root`, so a reload asks
for no second tick. The Arena is linked from the home hero as "Help Improve Our
Models".

## Review insights

Each vote keeps a raw record of how the review went, so the votes can be
studied and weighted in more than one way later. Nothing here changes how a
vote counts today, and nothing is ever refused over it.

- `served_at`: when the next-task endpoint handed this session the task,
  kept in the session (`chooser_served_at`, the last
  `SESSION_SERVED_LIMIT` tasks) and copied onto the vote.
  `ChooserVote.seconds_to_vote` is `created_at - served_at`, timed on the
  server. Null when the vote came without a recorded serve (a request with
  no session cookie, an older client).
- `active_ms`: the page's count of the time it was visible between the
  case appearing and the vote (time in another tab is left out), clamped to
  between zero and a day (`ACTIVE_MS_CAP`). Null from clients that do not send it,
  or that send something other than a number (then listed under
  `review_signals.dropped`). When it
  and `seconds_to_vote` disagree by more than network time, the server
  figure is the one measured here.
- `expanded_candidate_ids`: the presented drafts opened with "Read it
  all", in display order. Tokens on the way in, like the other candidate
  references; one that names no presented draft is left out, and
  `expanded_candidate_ids` is then listed under `review_signals.dropped`.
- `review_signals`: the page's interaction summary, cleaned by
  `chooser_review_signals.clean_review_signals` (which documents each
  key): pointer moves and presses, key presses, scrolls, pick changes,
  times the page was hidden, visible time to the first interaction
  anywhere on the page while the case is open, the
  pointer types seen (mouse, touch, pen), whether the vote was cast by
  keyboard or pointer, rounds already voted in this page load, the
  viewport width, `navigator.webdriver`, and by draft position (0 for
  Draft A) which drafts overflowed their box and which were scrolled to
  the end. A malformed entry is left out and named under `dropped`.
- `network_class`, `asn_name`, `country`: the judge's network as
  `client_network.classify` classes it (isp, hosting, ai_platform, tor,
  unknown), the ASN's name, and Cloudflare's country. Never the address.
  Blank when no Cloudflare address came with the request.
- `user_agent`: the browser's user agent, cut to 400 characters.

The position of the pick is already in the vote: `presented_candidate_ids`
is in display order and the server shuffles that order every time.

None of these fields leave the server in the exports.

## How a case is made (`chooser_tasks.py`)

1. **Seed pair.** When `CHOOSER_SEED_FROM_RANKINGS` is on (default) the
   scenario is built around a real `(treatment, condition)` pair from
   `condition_rankings.seed_pairs()`: pairs entered by at least k different
   people, both terms vouched for by a medical vocabulary (see below). The pool
   being empty (no refresh has run yet) falls back to the old behaviour,
   where the writer model invents the pair. A `CHOOSER_SIMPLE_CASE_SHARE`
   share of cases (a quarter) are **simple cases**: the writer is asked, in
   so many words, for an everyday procedure or medication, an everyday
   denial reason and two or three facts that settle it, and picks the pair
   itself. Left alone it makes every case elaborate, and a model that pads a
   plain denial out with claims the facts do not support is never caught.
   The case carries `context_json["case_style"]` (`"simple"` or
   `"standard"`), the voter is told when a case is simple, and the export
   carries it in `context`, so votes can be split by it.
2. **Scenario.** A general-purpose backend writes the insurer, the denial
   reason and the case facts (`_scenario_prompt`). Facts are parsed from
   one semicolon-separated line or a bullet list (`_facts_from_response`)
   and stored in `context_json["facts"]`; a scenario without facts is still
   used.
3. **Evidence.** `_evidence_for` attaches up to `CHOOSER_EVIDENCE_LIMIT`
   articles for the pair to `context_json["evidence"]` as `{pmid, title,
   url, summary}`. It reads the cached PubMed searches first
   (`PubMedQueryData` rows written by every search, any age) and only
   searches live when nothing is cached and `CHOOSER_PUBMED_LIVE_SEARCH`
   allows it (off in tests). Seeded pairs are exactly the ones real appeals
   already searched for, so a seeded task usually costs no NCBI call. The
   summary is the one real appeals were given for the article
   (`PubMedArticleSummarized.basic_summary`) when there is one, else the
   start of its abstract; an article with neither has no summary. The
   articles kept are then judged for the pair when `CHOOSER_JUDGE_EVIDENCE`
   is on (`_judge_evidence`, through `pubmed_tools.judge_missing`): a
   judgment already stored for the pair, by this or by a real appeal, is
   not asked for again, and new ones are stored in `PubMedArticleJudgment`,
   where the next case or appeal about the pair reads them. Jev judges first
   when research judging is on too. Whatever it does not judge (research
   judging off or not set up, a failed request, a spent budget, or Jev still
   unanswered at half of `CHOOSER_EVIDENCE_JUDGING_TIMEOUT`, or at all of it
   when none of our own models looks healthy) falls through to our own best
   internal model (`ml_router.best_internal_model`,
   `research_judging.judge_article_with`), with a logged warning naming the
   reason (Jev not set up, `research_judging.skip_reason`, out of time, or
   failed requests, each of which is also logged on its own) and the model. Our own model is asked Jev's three questions, for a
   likelihood of each out of 100, read strictly (a reply it cannot read
   leaves the article unjudged), and its judgments are stored under a scorer
   of their own (`fhi/<model>/research-rubric-N`). Only the arena reads
   those: a real appeal's research and fax page stay on Jev's alone. Once
   Jev is back, an article only our own model judged is asked of Jev again,
   and Jev's judgment is the one shown. Nothing is kept on the case: the
   drafts are told the summaries, never the verdicts. The pair is
   k-anonymous and the case synthetic, so no consent is involved, and what
   is sent is the article's public title and abstract and the pair. Best
   effort, bounded by `CHOOSER_EVIDENCE_JUDGING_TIMEOUT` for both together.

   Beside the PubMed lookup, `_web_evidence_for` runs OpenRouter's web
   search on the pair when `OPENROUTER_API_KEY` is set and `CHOOSER_WEB_SEARCH`
   is on (off in tests), with the same relevance guardrail real appeals use
   (`ml/web_search_context.py`, see [ml-backends.md](ml-backends.md)): each
   page is asked research judging's three questions about its excerpt, by
   Jev when research judging is on, else by our own best model, and only
   pages judged on topic and not against the treatment are kept (at most
   three, strongest support first, after the articles). A page nobody could
   judge is left out, so with `CHOOSER_JUDGE_EVIDENCE` off there are none.
   A kept page is a row `{title, url, summary, source: "web", web_judgment}`:
   the summary is the search's excerpt, and `web_judgment` is the verdict
   that kept it, stored on the case because a web page has no PMID to look
   a judgment up by. The drafts are given the title, link and excerpt, never
   the verdict. Bounded by `CHOOSER_WEB_SEARCH_TIMEOUT`; a failure leaves the
   PubMed rows.
4. **Interview.** `_interview` runs a simulated question-and-answer
   session: the app's own question generators
   (`MLAppealQuestionsHelper.generate_specific_questions`, given the case as
   the denial and the case facts as the patient's history; the generic,
   cached-by-pair questions when those come back empty) ask up to
   `CHOOSER_MAX_QUESTIONS` questions, and a scenario writer answers them in
   character, consistent with the facts. The pairs are stored in
   `context_json["qa_pairs"]` as `{question, answer}`, the shape the
   training loaders already read. Best effort: no questions, or no usable
   answers, leaves the case without them. `CHOOSER_SIMULATED_QA` turns it
   off (it is off in tests).
5. **Drafts.** Each backend is told the facts and the patient's answers are
   the only facts, and the evidence list is the only thing it may cite, for
   no more than its summary says (`_build_appeal_prompt`), so a draft that
   makes things up is doing so against instructions, and the voter can check
   it against the same list. The selected backends are asked at the same
   time (`_fill_candidates`), so a task takes as long as its slowest draft,
   not all of them added up; the slots are still seated in the models'
   order. The retry pass that fills a slot a backend left empty runs one
   backend at a time, as before.
6. **Synthesis.** When `CHOOSER_INCLUDE_SYNTHESIS` is on, one more
   candidate merges the drafts, so synthesis is measured against the single
   models. The model that merges them is **drawn at random for each task**
   (`_combiner_order`), not raced or ranked, so each model's combined
   drafts get votes and the models can be compared at combining. The draw
   is from the arena's own models: a `CHOOSER_FORCE_MODEL` pool big enough
   to fill every slot is the combiners too; otherwise the forced models and
   the router's synthesis backends (`ml_router.synthesis_backends`, with
   hosted models allowed since the drafts came from them too). Never the
   appeal-text fine-tune, which cannot follow "merge these drafts" and
   answers with a stray number, nor a context-only backend. When the model
   drawn writes nothing usable (nothing, too short, or a draft copied
   verbatim) the next one drawn is asked, up to three; with none available,
   or none of the three usable, the task skips synthesis. The candidate
   keeps `model_name="synthesized"` and records the model that wrote it in
   `ChooserCandidate.synthesized_by`, which the voter is never sent. The
   staff model usage dashboard splits the "synthesized" row by it (see
   Exports).
7. **Backend-only judge picks.** Once at least two drafts are on file,
   `chooser_scoring.score_task_candidates` asks Jev and the strongest healthy
   internal model (fhi-local) which candidate each scores highest. Appeals use
   the existing letter-quality rubric; chats use the Jev reply-ranking rubric,
   in balanced batches so a fifth, synthesized candidate is included too.
   fhi-local sees unlabelled candidates and returns bounded JSON scores. Each
   judge's winner, score and scorer/rubric goes into the task's
   `scoring_json`, not its case context or candidate metadata, so voters get
   no hint about authorship or the judges' preferences. Scoring is
   best-effort and never decides whether a task goes READY; either judge may
   independently be absent. `CHOOSER_SCORE_CANDIDATES` controls it (off in
   tests, which mock and enable it per test).

Each step logs one INFO line saying what it found and how long it took
(the scenario and its writer, the evidence and whether it was cached or
searched live, the questions asked and answered, the candidates seated,
the synthesis), and the task's READY line carries the total.

### Waiting for a case

The next-task endpoints (`/ziggy/rest/chooser/next/appeal/`, `.../chat/`)
answer `200` with a case, `202` when there is no case for this session yet
but one is being written (the prefill the request set off, or another
process holding the type's generation claim; the body carries
`retry_after` seconds), and `404` only when nothing is underway. The page
polls on `202`, showing that a case is being written, and gives up after
fifteen minutes. A case is a round of model calls, so a few minutes is
normal; before this the page read the empty pool as "try again in a few
minutes" and sent the visitor away from a case that was one poll from
READY.

### Practice-area preference

Before starting, a judge may optionally name an area of practice they know
from a fixed specialty list. The choice is kept in this browser and sent as
`?practice_area=<slug>` on next-task requests; it is not stored on the vote.
The endpoint serves a ready matching case first, but **never waits for one**:
when no match is ready it serves the usual general case and asks for a
specialty case in the background. A later round prefers the matching case
once it is ready, and the voting screen labels it with the specialty. An
unknown slug is ignored, so it cannot inject prompt text or trigger
generation.

`prefill_practice_area_if_needed` generates only when no fresh, unvoted
matching task exists. It uses the same per-type generation claim as every
other writer and a per-type-and-area throttle, so specialty requests cannot
overlap the general refill or multiply unchecked. Targeted appeal cases ask
the scenario writer to invent a treatment-and-condition pair within the
specialty rather than taking a possibly unrelated general-rankings seed;
targeted chat conversations and their single-question fallback get the same
instruction. The generated case stores the validated slug in `context_json`
for later selection and export. As with every generated case, relevance is
best-effort; the general pool remains available throughout.

### What the voter is served (`annotate_evidence_verdicts`)

The next-task endpoint serves an appeal case with each evidence row carrying
`judgment` (`{verdict, on_topic, supports, undermines}`,
`research_judging.Judgment.as_dict`) when a judgment under the current rubric
is stored for the PMID and the case's treatment and condition: Jev's, else
our own model's. It is read as the case is served, not stored with it: a
case built before its articles were judged shows the verdicts too, and
`context_json` stays exactly what the drafts were given. It reads only
(`pubmed_tools.stored_judgments_for`). Jev's verdicts are shown only while
research judging is switched on, the kill switch for Jev's stored judgments
everywhere, and our own model's only while `CHOOSER_JUDGE_EVIDENCE` is. A web
page's verdict comes from its own row (`web_judgment`, moved to `judgment`)
under the same two switches. A case it cannot be read for is served
without it. The citation check needs nothing from the server.

### Case versions and what voters see first

`ChooserTask.case_version` records which pipeline built the task
(`chooser_tasks.CASE_VERSIONS`): appeal tasks built with summaries and an
interview are version 2, and those whose evidence can also carry judged web
pages are version 3 (so the first deploy with it makes the launch backfill
due: no version 3 task has its votes yet); older appeal tasks and every chat
task are 1. The
version marks the pipeline, not what each step found. The next-task endpoint
serves the newest version first (then fewest votes, then oldest), so new
cases collect votes before old ones, which stay votable; a judge who is
warming up (see below) is served appeal cases with evidence first, then
cases judged fewer than `CHOOSER_WARMUP_SATURATION_VOTES` times, then the
newest version, then by complexity (so an older case still short of that
many votes comes before a newer one that has them). Only tasks at the
current version count as fresh for the refill (`_fresh_tasks`), so unvoted
old tasks never hold off generating new ones.

### Complexity, and starting a judge on the simplest cases

`ChooserTask.complexity` scores how much work a case is to judge, from 1
(the simplest) to 5 (`chooser_complexity.py`). It is the reading and
cross-checking load, not how clinically hard the case is: the words in
every draft and in the case shown above them (facts, answers, the evidence's
titles and summaries, or the chat so far), read at a careful 230 words a
minute, plus a little per citation in the drafts (PMIDs, PubMed links, DOIs,
"et al.", author-year parentheses, bracketed numbers, and an article on
file named by its title), added up as estimated minutes and banded: at most
8 minutes is a 1, 14 a 2, 21 a 3, 30 a 4, and more a 5. The band edges are
provisional, set from what tasks look like rather than measured; the score
is computed from the stored task alone, so it is deterministic and cheap to
recompute, and `score_chooser_complexity --dry-run -v 2` (below) prints
every task's minutes to tune them against the real pool. A task is scored
as it goes READY, once every draft (the synthesized one included) is on
file; a scoring error leaves the score empty rather than holding the task
back.

A task with no score (one from before the score existed, or whose scoring
failed) is **served as a 5 but stored as NULL**: the ordering below
coalesces the empty score to the maximum and never writes it, so a real
score can be filled in later. `python manage.py score_chooser_complexity
[--task-type appeal|chat] [--rescore] [--dry-run]` does that: by default it
scores the READY tasks without a score; `--rescore` redoes every READY task
(after a change to the rubric); `--dry-run` reports without storing;
`-v 2` prints each task's breakdown. Nothing runs it on deploy, so until it
has run an older case counts as a 5. The keys that come before complexity
still decide first (evidence attached, then cases judged fewer than
`CHOOSER_WARMUP_SATURATION_VOTES` times, then the newest case version);
within them, an unscored case is served after every scored case below 5 to
a judge who is warming up or asked for straightforward cases, at any round,
and before them to a judge who asked for complex ones. It ties with the
scored 5s, so fewest votes and then age decide between those.

The score is used to **start a judge on the simplest cases**
(`chooser_complexity.ordered_for_judge`). A session's level is 1 for its
first `CHOOSER_WARMUP_ROUNDS_PER_LEVEL` votes on a task type (skips are not
rounds, and votes on the other type don't count), one more for the next as
many, and so on; at the default of one vote a level a judge is warmed up
after four votes. While the level is below 5 the next-task endpoint serves,
in order: appeal cases with evidence attached before those without (so the
first rounds still practise checking citations); cases judged fewer than
`CHOOSER_WARMUP_SATURATION_VOTES` times before those judged that often (so
the simplest cases don't take every new judge's first votes while the rest,
the unvoted hard ones that hold the refill off included, wait); the newest
case version first, as always; then the cases by how far their complexity
is above the level, so every case within reach ties and the usual keys
decide, and when nothing is within reach the nearest is served; then fewest
votes, then oldest. From level 5 on, and with the rounds setting at 0, the
usual order alone. The staff export (below) carries each task's
`complexity`, so a vote's difficulty can be read alongside it.

**After their first round, the judge is asked what kind of case they want
next**: "Keep them straightforward", "Bring on complex ones" or "Either is
fine". The "Vote counted" screen asks until it is answered, and each answer
also starts the next round; once answered it shows a switcher instead, on
that screen and on the start page, so the choice can change between rounds.
The answer is kept in the browser (`localStorage`), like the scoreboard, and
goes to the next-task endpoints as `?difficulty=straightforward` or
`?difficulty=complex` ("Either" sends nothing, which keeps the warm-up). The
server keeps the warm-up's first keys (evidence first, then cases judged
fewer than `CHOOSER_WARMUP_SATURATION_VOTES` times, then the newest case
version) and then orders by complexity: `straightforward` holds the level
at 1 however many rounds are judged, so the simplest case left comes first;
`complex` serves the most complex case left first, an unscored case
counting as a 5. Both apply with the warm-up off too, since the judge asked;
an unknown value is ignored. The choice is not stored with the vote.

### Retiring a case once its votes say enough (`chooser_settlement.py`)

Every review a case takes is one some other case didn't get. So once a
case's votes show a **clear winner**, or show it is **clearly undecided**,
it is *settled*: its status goes from READY to `EXHAUSTED` and
`ChooserTask.settled_reason` (`clear_winner` or `undecided`, with
`settled_at`) says why. The next-task endpoint serves READY tasks only, so
it stops handing the case out, and the refill's pool counts stop counting
it, so a fresh case is written in its place. Its votes stay and keep
counting in the rankings and exports (`settled_reason` is in the staff
export). A judge it was served to before it settled can still vote on it.

The rules run after every vote and skip, over the case's votes (an all-bad
one included: each is a *review*) and skips:

- Each review's point is shared between its winners, as everywhere else; an
  all-bad review's goes to "no usable draft", which is ranked like a draft,
  so judges agreeing nothing is usable is an answer too.
- Two outcomes are compared **head to head** over the reviews that showed
  both: a review counts for the one it gave more of its point, and for
  neither when it gave them the same (tied winners, or neither won).
- **Clear winner**: at least `CHOOSER_SETTLE_MIN_REVIEWS` reviews, and the
  leading outcome beats every other one head to head by a one-sided sign
  test at p ≤ 0.05 (5-0, 6-0, 7-1, 8-1 and so on against its closest rival;
  6-1, at p ≈ 0.06, is not enough).
- **Clearly undecided**, when there is no clear winner, in any of three
  ways: at least `CHOOSER_SETTLE_MIN_REVIEWS` judges saw it (reviews plus
  skips) and at least `CHOOSER_UNDECIDED_SKIP_SHARE` of them skipped it;
  or `CHOOSER_UNDECIDED_REVIEWS` reviews and the leader beats the
  runner-up in no more than 60% of the reviews that tell them apart (an
  even split at the top); or `CHOOSER_UNDECIDED_MAX_REVIEWS` reviews with
  no clear winner at all.
- A skip counts only when it was of a case the next-task endpoint served
  that session (`ChooserSkip.served_at`), since a skip names nothing but a
  task id and could otherwise retire cases nobody read; and not when the
  same judge later voted on it, so nobody is both a reviewer and a skipper.
  The vote marks the judge's skip `superseded`, matching the judge as the
  vote endpoint does (session, signed-in user, and for a request without a
  session the address); the skip is kept, since the staff participation
  counts read it. Skips from before `served_at` was recorded
  don't count. Any skip still hides the case from that judge.

A vote or skip only ever settles a case, never reopens one.
`python manage.py settle_chooser_tasks [--task-type appeal|chat] [--reopen]
[--dry-run]` applies the rules to the whole READY pool (for cases voted on
before settling existed, or after a change to the settings); `--reopen`
also puts a settled case the rules no longer settle back to READY, and
`-v 2` prints each case's reason. Nothing runs it on deploy.
`CHOOSER_SETTLE_TASKS=false` turns settling off.

### Trying out a model, or choosing the pool (`CHOOSER_FORCE_MODEL`)

To get a new model votes quickly, set `CHOOSER_FORCE_MODEL` to its
registered router name (a key of `ml_router.models_by_name`, as
`FORCE_MODEL` takes; several may be given, comma-separated). With no more
names than `CHOOSER_NUM_CANDIDATES`, every new arena task seats them all
first, whether or not the router would have picked them, and fills the other
slots as usual, so they are compared against the regular rotation rather
than replacing it. An appeal-text fine-tune (one that doesn't follow general
instructions) is forced into appeal tasks only, as chat routing leaves it out
too. The coverage refill treats a forced model like an external backend,
building fresh tasks until enough of them include it. A name the router
doesn't know is logged once and ignored. Unlike `FORCE_MODEL`, it changes
nothing outside the arena.

With more names than slots, the names are the pool: each new task draws its
`CHOOSER_NUM_CANDIDATES` slots from them (`_draw_forced_models`), the models
on the fewest fresh tasks first, ties at random, so every pool model keeps
fresh tasks to be voted on, and no model outside the pool is seated. If
skipped names leave fewer registered pool models than slots, the usual
rotation fills the rest.

A forced name can also be a model kept out of every general pool (one a
backend lists in `chat_models()`), so a provider can be tried on the arena's
synthetic cases without being offered any real traffic. The default is a
pool of five (see [ml-backends.md](ml-backends.md)):

- `lunaroute/deepseek-4.1-flash` (DeepSeek 4.1 Flash, needs `LUNA_ROUTE_KEY`)
- `lunaroute/glm-5.3` (GLM-5.3, needs `LUNA_ROUTE_KEY`)
- `openrouter-arena/anthropic/claude-opus-5.5` (Claude Opus 5.5)
- `openrouter-arena/openai/gpt-6.1-sol` (GPT-6.1 Sol, OpenAI's tier below its
  flagship, as Opus is below Fable)
- `deepseek-ai/DeepSeek-V4-Pro-0813` (the full DeepSeek, DeepInfra, needs
  `DEEPINFRA_API`; DeepInfra serves no full 4.1 yet)

The two `openrouter-arena/` models need `OPENROUTER_API_KEY` and are
synthetic-only, like LunaRoute's. `CHOOSER_FORCE_MODEL=""` turns forcing off.

## Keeping the pool stocked

Four things write tasks, all under one generation claim per type
(`_claim_generation`, a row in `ModelHealthAlertState`), so no two overlap:

- **Page prefill.** Loading the arena, or asking for a case when none is
  left for the session, sets off a prefill in a background thread
  (`trigger_prefill_async`). It writes one task of each type whose READY
  pool is empty, and of the type the session used up (`prefill_if_needed`).
  Throttled per process to one per `CHOOSER_PREFILL_THROTTLE_SECONDS`, with
  page loads and each used-up type throttled separately.
- **Practice-area prefill.** A next-task request with a specialty but no
  available match sets off `trigger_practice_area_prefill_async`. It writes
  one matching task if no fresh one exists, throttled separately by type and
  area, while the request proceeds with a general case.
- **Refill actor.** `ChooserRefillActor` (launched by the deploy's
  actor-launch job, reconciled by a cron) checks every five minutes and
  writes `CHOOSER_GENERATION_BATCH_SIZE` tasks of a type whose READY pool,
  fresh (unvoted, current-version) pool, or coverage of a backend is short
  (`check_and_refill_task_pool`).
- **Launch backfill.** The actor-launch job first runs
  `manage.py launch_chooser_backfill` (before the polling actors: the
  backfill is due exactly when the refill's first tick would want a batch,
  and whichever takes the claim first writes), which starts the one-shot
  `ChooserBackfillActor`. It writes `CHOOSER_LAUNCH_BACKFILL_SIZE` appeal
  tasks (a hundred), one at a time, when every READY appeal task at the
  current case version has at least `CHOOSER_LAUNCH_BACKFILL_MIN_VOTES`
  votes (two): the voters have got through everything the current pipeline
  made, so a deploy of a new pipeline gets a pool of its own cases rather
  than the refill's ten. With a task still short of the bar it does nothing;
  the next deploy asks again. Due, it waits up to
  `CHOOSER_LAUNCH_BACKFILL_CLAIM_WAIT_SECONDS` (an hour) for the claim: the
  claim it finds is usually the one the old cluster's refill actor held
  mid-batch when the deploy deleted it, which lapses within the fifteen-minute
  claim window. Whether it is due is decided once, at launch, and tasks the
  refill or a prefill write while it waits count toward the hundred instead
  of calling it off. A finished actor from an earlier launch is
  retired before a fresh one is created (a detached actor runs the code it
  was created with), one still writing is left to finish and declines a
  second backfill. It stops after three tasks in a row come out unusable.
  `manage.py launch_chooser_backfill --inline` runs the same backfill in the
  current process (a dev machine without Ray); `--count` and `--min-votes`
  override the settings.

## HumanJudge, an outside human judge (`chooser_humanjudge.py`)

[HumanJudge](https://humanjudge.com) has its own volunteer and expert
reviewers give AI outputs a blind pass or flag. The Arena uses it as one
more judge of which appeal draft is best, beside Jev and fhi-local. It is
off unless `HUMANJUDGE_API_KEY` (the HumanJudge model's `gj_sk_` key) is
set; `CHOOSER_HUMANJUDGE=false` turns it off with the key in place.

Staff run it from the admin; nothing goes out or comes back on a timer:

- **Upload.** Three ways, all from the admin, all the same upload:
  - **HumanJudge: [N] unsent cases [Upload]** at the top of the Chooser Task
    list sends the next N cases waiting to go: clearly undecided ones first
    (`settled_reason="undecided"`; the Arena's voters split on them, so a
    second opinion helps most), then the newest
    (`chooser_humanjudge.unsent_task_ids`). N is 1 to 300
    (`MAX_TASKS_PER_UPLOAD`); 25 is filled in.
  - **Upload to HumanJudge** at the top of a case's own page sends that
    case's drafts not yet sent. It shows only on a case that may go, and
    says how many of its drafts are already there.
  - The **Upload to HumanJudge** action sends the cases ticked on the list
    (the **HumanJudge** filter shows which have gone).

  Up to 25 cases (`INLINE_UPLOAD_LIMIT`) are sent while the page waits, and
  it says how it went. More go in a background thread
  (`chooser_humanjudge.start_upload`): the page says so at once, and
  HumanJudge Submissions shows each draft as it goes; a web pod restarting
  mid-upload leaves the rest unsent, ready for the next upload. Drafts
  already sent are skipped, so uploading a case again sends only what
  failed. Failures are listed under HumanJudge Submissions with their status
  code, where **Upload again** retries them.
- **Only the Arena, only synthetic appeals.** HumanJudge's reviewers read
  what they are sent, so only the active drafts of `source="synthetic"`,
  `task_type="appeal"` cases that have reached voters (READY, IN_USE or
  EXHAUSTED) ever go. Any other selected case is skipped, and the message
  says how many. Never a real denial or appeal, and never a chat case.
- **What is sent.** Each draft is one trace (`POST /api/v1/traces/ingest`):
  the input is a brief saying the case is made up and what to pass or flag,
  then the case (treatment, diagnosis, insurer, denial reason, facts, the
  simulated interview and the evidence titles and summaries); the output is
  the draft. The trace's name and model are both `fhi-appeal-arena`, and its
  metadata only our task and candidate ids, so nothing says which model
  wrote it. Each draft's `gj_inference_id` is made once
  (`HumanJudgeSubmission`) and reused on every retry, so a resend is
  idempotent on HumanJudge's side. A refused key stops the upload without
  counting an attempt.
- **Download.** **Download verdicts from HumanJudge**, on the HumanJudge
  Verdict list, reads the votes
  (`GET /api/v1/models/{id}/evaluations?detail=votes`, with
  `HUMANJUDGE_TOKEN`, a `gj_pat_` personal access token, when set, else the
  key) from two days before the newest stored, matches them to drafts by
  inference id, and updates the picks of the cases that got one. Votes that
  match no draft we uploaded are counted in the message and left out.
- **Verdicts are stored apart.** Each lands in `HumanJudgeVerdict`
  (verdict, flag category, feedback, HumanJudge's reviewer id, time, and the
  raw record), never in `ChooserVote`, so they never count toward the
  Arena's tallies, the staff dashboard's win rates or the public rankings.
  Downloading a vote again does not add a row. **Export selected verdicts as
  CSV** on that list gives them with the model behind each draft.
- **The pick.** HumanJudge rates drafts one at a time, not side by side, so
  once every active draft of a case has `CHOOSER_HUMANJUDGE_MIN_VERDICTS`
  verdicts the case's pick is the draft with the best smoothed pass rate,
  (passes + 1) / (verdicts + 2). It goes into the task's `scoring_json` under
  `humanjudge`, in the same shape as Jev's and fhi-local's picks plus
  `passes` and `verdicts`, with scorer `humanjudge/pass-rate-1`. A tie for
  best records no pick.
- **On the public rankings.** Each appeal-letter row carries the model's
  HumanJudge pass rate (`humanjudge` in the JSON: reviews, passes,
  pass rate, warming up) in its own "Outside reviewers passed" column, shown
  as a rate from `PUBLIC_RANKINGS_MIN_SHOWN` reviews; the leader's card
  repeats it. It never changes the order, which stays our voters' alone.
  Only aggregate counts reach the public page, never reviewers' feedback.
  HumanJudge reviews blind, so its verdicts are not held to the
  blind-voting cut-off; the 30-day window counts them by when they were cast
  (`humanjudge_reports.model_verdict_stats`).
- **Staff breakdown.** **Breakdown by model** on the HumanJudge Verdict
  list (`.../humanjudgeverdict/breakdown/`, `?format=json` for the data)
  shows each model's uploads, verdicts, pass rate and flags by category
  beside its Arena record, how often HumanJudge's pick is the same draft as
  the Arena voters', Jev's and fhi-local's, and the latest written feedback.
  It is an admin view: anyone not signed in as staff is sent to the admin
  login, and staff without view permission on HumanJudge verdicts get a 403,
  the JSON included (`humanjudge_reports.breakdown`).
- **The account.** One HumanJudge model, standalone (not enrolled in a
  public benchmark), holds every Arena draft. With more than one model on
  the account, set `HUMANJUDGE_MODEL_ID` to it.

## Rankings and privacy (`condition_rankings.py`, `public_rankings.py`)

The public page shows two things, both safe to publish:

- **Model rankings**: `ChooserVote` rows on tasks whose `source` is
  `synthetic`, cast since blind voting began (`public_rankings.blind_since`:
  when migration 0240 was applied, or `PUBLIC_RANKINGS_SINCE`), and nothing
  else. Win rate is votes won over votes shown in;
  "caught making things up" is flags over votes shown in. A tie gives each
  tied draft an even share of the vote. Models with fewer
  than `PUBLIC_RANKINGS_MIN_SHOWN` votes are listed as warming up. Models
  are named without their provider (`public_model_label`): every
  `provider/model` label serving one model counts as a single row, on the
  broad assumption that providers of the same model are equivalent. Each
  table names its **leader**, the first ranked model (best win rate, ties to
  the one flagged less), or none while every model is warming up; the page
  opens with the all-time leaders (`Who is winning`), marks the leading row
  in each table, and draws every win rate as a bar. The JSON repeats the
  all-time leaders under `leaders`, keyed by kind. Real
  patients' picks, call logs, chat turns and session counts stay on the
  staff dashboard (`/timbit/...`, `ModelUsageDashboardView`).
- **Most-appealed conditions and treatments**: aggregates over
  `Denial.diagnosis` / `Denial.procedure`. A term is shown only if it

  1. comes from a denial that is not flagged for exclusion, not from a test
     account (`TEST_ACCOUNT_EMAILS`), and not filed through a professional
     account (`condition_rankings.eligible_denials`, the same base as the
     de-identified export);
  2. is shaped like a medical term (`looks_like_medical_term`: no digit
     runs, no pronouns, no addresses, no non-answers like "see attached");
  3. was entered by at least `CONDITION_RANKING_MIN_PEOPLE` (default 5)
     different people, counted by distinct hashed email after
     normalization, so one person filing a case five times, or spelling a
     term five ways, counts once. A term below that threshold is never
     shown, never seeded into a case, and never sent to an outside service;
  4. is vouched for by a vocabulary: the microsite list in the repo, RxNorm
     for drug-shaped treatments, then NCBI (a MeSH lookup, and for compound
     phrases a quoted title/abstract search with at least
     `CONDITION_RANKING_MIN_PUBMED_HITS` hits). Verdicts persist in
     `MedicalTermCheck`, so each term costs outside lookups once; a refresh
     spends at most `CONDITION_RANKING_LOOKUP_BUDGET` of them.

  Counts are rounded down to a multiple of the threshold ("25+"), and the
  list is ordered by the rounded count (ties by name), so neither the order
  nor the top-N cut tells two "5+" terms apart.

The counts and verdicts are refreshed in the background: the chooser refill
actor calls `condition_rankings.refresh_if_due()` after each tick (once an
hour fleet-wide, via the `ModelHealthAlertState` claim, and bounded by
`CONDITION_RANKING_REFRESH_TIMEOUT`), and
`python manage.py refresh_condition_rankings [--budget N]` does it by hand.
One refresh runs at a time; a second caller is told to come back. Each
refresh stores every k-anonymous term's people count on its
`MedicalTermCheck` row, so a page load is a read of that table: it never
scans the denials and never calls NCBI. Page data is cached for
`PUBLIC_RANKINGS_CACHE_SECONDS`; `?format=json` returns the same data for
embedding. All of these settings are defined on `settings.Base` from
environment variables of the same name.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `CHOOSER_SEED_FROM_RANKINGS` | `True` | Build appeal scenarios around a real, k-anonymous pair |
| `CHOOSER_SIMPLE_CASE_SHARE` | `0.25` (`0` in tests) | Share of new appeal cases written as explicitly simple ones |
| `CHOOSER_EVIDENCE_LIMIT` | `5` | Articles attached to a scenario |
| `CHOOSER_PUBMED_LIVE_SEARCH` | `True` (`False` in tests) | Search PubMed when nothing is cached for the pair |
| `CHOOSER_PUBMED_SEARCH_TIMEOUT` | `20.0` | Seconds a live search may take |
| `CHOOSER_WEB_SEARCH` | `True` (`False` in tests) | Also search the web (OpenRouter, when `OPENROUTER_API_KEY` is set) for a case's evidence, keeping only pages judged relevant |
| `CHOOSER_WEB_SEARCH_TIMEOUT` | `25.0` | Seconds that search and its judging may take (25 at most) |
| `CHOOSER_SIMULATED_QA` | `True` (`False` in tests) | Give appeal cases a simulated interview |
| `CHOOSER_JUDGE_EVIDENCE` | `True` (`False` in tests) | Judge a case's evidence as it is built: Jev when `TYPESAFE_RESEARCH_JUDGING_ENABLED` is on, our own model for whatever Jev does not judge. Off also hides our own model's stored verdicts |
| `CHOOSER_SCORE_CANDIDATES` | `True` (`False` in tests) | Ask Jev and fhi-local for their highest-scoring candidate during generation and store the picks in backend-only task metadata |
| `CHOOSER_HUMANJUDGE` | `True` when `HUMANJUDGE_API_KEY` is set (`False` in tests) | Allow the admin's HumanJudge upload and download (see above) |
| `HUMANJUDGE_API_KEY` / `HUMANJUDGE_TOKEN` / `HUMANJUDGE_MODEL_ID` | unset | HumanJudge's model key, an optional personal access token for reading votes, and the model id when the account has several |
| `CHOOSER_HUMANJUDGE_MIN_VERDICTS` | `1` | HumanJudge verdicts every draft of a case needs before its pick is recorded |
| `CHOOSER_EVIDENCE_JUDGING_TIMEOUT` | `60` | Seconds a case waits for those verdicts, Jev's and our own model's together |
| `CHOOSER_MAX_QUESTIONS` | `4` | Questions the simulated patient is asked |
| `CHOOSER_QA_TIMEOUT` | `60` | Seconds each question generator may take |
| `CHOOSER_PENDING_RETRY_SECONDS` | `15` | What the next-task endpoint tells the page to wait while a case is being written |
| `CHOOSER_LAUNCH_BACKFILL_SIZE` | `100` | Appeal tasks the launch backfill writes (`0` turns it off) |
| `CHOOSER_LAUNCH_BACKFILL_MIN_VOTES` | `2` | Votes every current appeal task needs before the launch backfill is due |
| `CHOOSER_LAUNCH_BACKFILL_CLAIM_WAIT_SECONDS` | `3600` | How long a due launch backfill waits for a generation claim another process holds |
| `CHOOSER_FORCE_MODEL` | DeepSeek 4.1 Flash, GLM-5.3, Claude Opus 5.5, GPT-6.1 Sol, DeepSeek V4 Pro (`""` in tests) | Registered model names seated in every new arena task; with more than `CHOOSER_NUM_CANDIDATES`, the pool every slot is drawn from |
| `CHOOSER_WARMUP_ROUNDS_PER_LEVEL` | `1` | Votes a judge casts before the complexity of the cases served to them may rise a level; `0` turns the warm-up off |
| `CHOOSER_WARMUP_SATURATION_VOTES` | `5` | Votes after which a case yields its place, in the warm-up or in the order a judge chose, to a less-judged one |
| `CHOOSER_SETTLE_TASKS` | `True` | Stop serving a case once its votes show a clear winner or that it is clearly undecided |
| `CHOOSER_SETTLE_MIN_REVIEWS` | `5` | Reviews before a clear winner is called, and judges (reviews plus skips) before a skip-heavy case is called undecided |
| `CHOOSER_UNDECIDED_REVIEWS` | `10` | Reviews after which an even split between the top two counts as undecided |
| `CHOOSER_UNDECIDED_MAX_REVIEWS` | `20` | Reviews after which a case with no clear winner counts as undecided |
| `CHOOSER_UNDECIDED_SKIP_SHARE` | `0.5` | Share of the judges who saw a case skipping it that makes it undecided |
| `CONDITION_RANKING_MIN_PEOPLE` | `5` | The k in k-anonymity: different people per term |
| `CONDITION_RANKING_LOOKUP_BUDGET` | `30` | Outside lookups per refresh |
| `CONDITION_RANKING_RECHECK_DAYS` | `365` | When a stored verdict is asked again |
| `CONDITION_RANKING_MIN_PUBMED_HITS` | `20` | Hits a quoted phrase needs in PubMed |
| `CONDITION_RANKING_REFRESH_SECONDS` | `3600` | How often `refresh_if_due` refreshes |
| `CONDITION_RANKING_REFRESH_TIMEOUT` | `180` | Seconds the refill actor waits on one refresh |
| `CONDITION_RANKING_LIMIT` | `5` | Rows per list on the public page (kept short as defense in depth against PII typed into a field) |
| `ARENA_RANKINGS_PAGE_ENABLED` | `False` (`True` in Dev and tests) | Serve the public rankings page and link to it |
| `PUBLIC_RANKINGS_CACHE_SECONDS` | `900` | How long the page's numbers are cached |
| `PUBLIC_RANKINGS_MIN_SHOWN` | `10` | Votes before a model is ranked rather than warming up |
| `PUBLIC_RANKINGS_SINCE` | migration 0240's applied time | Earliest vote the public page counts; `""` counts all |

## Exports

The staff model usage dashboard (`ModelUsageDashboardView`) has, under each
chooser table, a **combiner table**: the votes on the synthesized drafts
alone, by the model that combined them (`synthesized_by`), with the same
columns as the model table (votes won, votes shown in, win rate, flags).
Synthesized drafts from before the combiner was recorded are under
"(combiner not recorded)". It is not on the public rankings page.

`charts:chooser_ranked_export` (staff only) carries each candidate's
`votes` (ties split), `tie_count` and `flag_count`, so the preference data,
the ties and the fabrication flags travel together. A synthesized
candidate also carries `synthesized_by`, the model that combined the drafts. `vote_count` is the
winning candidate's `votes` and `total_votes` the number of reviews cast.
`all_bad_count` records no-winner reviews; when they are a task's only
reviews, `chosen_candidate` is null and `vote_count` is zero rather than the
task disappearing from the export. Non-empty rationales are in
`review_feedback` with their outcome and candidate references, but no user,
session or address identifier. Generation-time Jev and fhi-local picks are in
`scorer_picks`, keyed `jev` and `fhi_local` (and `humanjudge` once
HumanJudge's reviewers have picked one), each with the winning candidate's
id, index and model name, its score, and the scorer/rubric. These stay on the
task's backend-only `scoring_json`, never in the case context or voter API.
The case (summaries and `qa_pairs` included) rides along in `context`, and its
`complexity` (1 to 5, or null when unscored) beside it. `settled_reason`
(`clear_winner`, `undecided`, or blank while the case is still served) says
why the case stopped being served, not who won: `chosen_candidate` stays the
draft with the most credit, even on a case whose clear answer was "no usable
draft" (compare it with `all_bad_count`). Both are counted at export time,
over every vote, including any cast after the case settled, so they can
differ from what the votes showed when it settled.

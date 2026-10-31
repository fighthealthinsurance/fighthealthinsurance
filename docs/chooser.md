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
shows nothing. The Arena itself is not behind the flag. One function reads
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
  and, when one is on file, a short summary). Everything shown is exactly
  what every model-written draft was given; the synthesized draft, which
  combines them, gets the treatment, diagnosis, denial reason and the drafts,
  not the facts, answers or evidence. For a chat task: the conversation and
  the question the drafts answer.
- **Two referee aids**, on top of the case and neither given to the drafts:
  - *The reviewer's verdict* on each study on file: a badge (supports the
    treatment, mixed, argues against, off topic, unclear) from Jev's
    research judgment of the article for the case's treatment and condition
    (`ml/research_judging.py`, the same judgments that order a real appeal's
    articles), with the three probabilities behind it under "What it
    found". A study nobody has judged has no badge. The page says the drafts
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
  brand.
- **House rules.** Pick the draft you would send; a draft that invents a
  study, a law, a test result, a date or a history that is not in the case is
  a foul and should never win; call a tie when two or more are equally good;
  skip if unsure.
- **Ties.** "It's a tie" switches the page to marking two or more drafts as
  tied for best. The vote carries them as `tied_candidate_ids` (in display
  order, none flagged) with `chosen_candidate_id` set to the first of them,
  so a tie vote still has a chosen candidate. Counting splits the vote
  evenly between the tied drafts (`ChooserVote.shares`: half a win each for
  a two-way tie), so every vote still adds up to one.
- **A flag per draft** ("Makes things up"). Flags ride along with the vote as
  `flagged_candidate_ids`; a flagged draft cannot be the chosen one, and the
  server rejects a vote that tries.
- A per-browser scoreboard (rounds judged, fouls called) in `localStorage`;
  nothing about it is sent anywhere.

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
the message. The Arena is linked from the home hero as "Help Improve Our
Models".

## How a case is made (`chooser_tasks.py`)

1. **Seed pair.** When `CHOOSER_SEED_FROM_RANKINGS` is on (default) the
   scenario is built around a real `(treatment, condition)` pair from
   `condition_rankings.seed_pairs()`: pairs entered by at least k different
   people, both terms vouched for by a medical vocabulary (see below). The pool
   being empty (no refresh has run yet) falls back to the old behaviour,
   where the writer model invents the pair.
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
   articles kept are then judged for the pair by Jev when
   `CHOOSER_JUDGE_EVIDENCE` and research judging are both on
   (`_judge_evidence`, through `pubmed_tools.judge_missing`): a judgment
   already stored for the pair, by this or by a real appeal, is not asked
   for again, and new ones are stored in `PubMedArticleJudgment`, where the
   next case or appeal about the pair reads them. Nothing is kept on the
   case: the drafts are told the summaries, never the verdicts. The pair is
   k-anonymous and the case synthetic, so no consent is involved, and what
   is sent is the article's public title and abstract and the pair. Best
   effort, bounded by `CHOOSER_EVIDENCE_JUDGING_TIMEOUT`.
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
   it against the same list.

### What the voter is served (`annotate_evidence_verdicts`)

The next-task endpoint serves an appeal case with each evidence row carrying
`judgment` (`{verdict, on_topic, supports, undermines}`,
`research_judging.Judgment.as_dict`) when a judgment under the current rubric
is stored for the PMID and the case's treatment and condition. It is read as
the case is served, not stored with it: a case built before its articles
were judged shows the verdicts too, and `context_json` stays exactly what
the drafts were given. It reads only (`pubmed_tools.stored_judgments_for`)
and shows nothing while research judging is switched off, the kill switch
for stored judgments everywhere. A case it cannot be read for is served
without it. The citation check needs nothing from the server.

### Case versions and what voters see first

`ChooserTask.case_version` records which pipeline built the task
(`chooser_tasks.CASE_VERSIONS`): appeal tasks built with summaries and an
interview are version 2; older appeal tasks and every chat task are 1. The
version marks the pipeline, not what each step found. The next-task endpoint
serves the newest version first (then fewest votes, then oldest), so new
cases collect votes before old ones, which stay votable. Only tasks at the
current version count as fresh for the refill (`_fresh_tasks`), so unvoted
old tasks never hold off generating new ones.

### Trying out a model (`CHOOSER_FORCE_MODEL`)

To get a new model votes quickly, set `CHOOSER_FORCE_MODEL` to its
registered router name (a key of `ml_router.models_by_name`, as
`FORCE_MODEL` takes; several may be given, comma-separated, up to
`CHOOSER_NUM_CANDIDATES`). Every new arena task then seats it first, whether
or not the router would have picked it, and fills the other slots as usual,
so it is compared against the regular rotation rather than replacing it. An
appeal-text fine-tune (one that doesn't follow general instructions) is
forced into appeal tasks only, as chat routing leaves it out too. The coverage refill treats it like an
external backend, building fresh tasks until enough of them include it. A
name the router doesn't know is logged once and ignored. Unlike
`FORCE_MODEL`, it changes nothing outside the arena.

A forced name can also be a model kept out of every general pool (one a
backend lists in `chat_models()`), so a provider can be tried on the arena's
synthetic cases without being offered any real traffic. For now the default is
LunaRoute's two models, `lunaroute/deepseek-4.1-flash,lunaroute/glm-5.3`
(see [ml-backends.md](ml-backends.md)); they are seated only where
`LUNA_ROUTE_KEY` is set, and `CHOOSER_FORCE_MODEL=""` turns forcing off.

## Rankings and privacy (`condition_rankings.py`, `public_rankings.py`)

The public page shows two things, both safe to publish:

- **Model rankings**: `ChooserVote` rows on tasks whose `source` is
  `synthetic`, cast since blind voting began (`public_rankings.blind_since`:
  when migration 0240 was applied, or `PUBLIC_RANKINGS_SINCE`), and nothing
  else. Win rate is votes won over votes shown in;
  "caught making things up" is flags over votes shown in. A tie gives each
  tied draft an even share of the vote. Models with fewer
  than `PUBLIC_RANKINGS_MIN_SHOWN` votes are listed as warming up. Each
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
| `CHOOSER_EVIDENCE_LIMIT` | `5` | Articles attached to a scenario |
| `CHOOSER_PUBMED_LIVE_SEARCH` | `True` (`False` in tests) | Search PubMed when nothing is cached for the pair |
| `CHOOSER_PUBMED_SEARCH_TIMEOUT` | `20.0` | Seconds a live search may take |
| `CHOOSER_SIMULATED_QA` | `True` (`False` in tests) | Give appeal cases a simulated interview |
| `CHOOSER_JUDGE_EVIDENCE` | `True` (`False` in tests) | Have Jev judge a case's evidence as it is built (needs `TYPESAFE_RESEARCH_JUDGING_ENABLED`) |
| `CHOOSER_EVIDENCE_JUDGING_TIMEOUT` | `60` | Seconds a case waits for those verdicts |
| `CHOOSER_MAX_QUESTIONS` | `4` | Questions the simulated patient is asked |
| `CHOOSER_QA_TIMEOUT` | `60` | Seconds each question generator may take |
| `CHOOSER_FORCE_MODEL` | LunaRoute's two models, for now (`""` in tests) | Registered model names seated in every new arena task |
| `CONDITION_RANKING_MIN_PEOPLE` | `5` | The k in k-anonymity: different people per term |
| `CONDITION_RANKING_LOOKUP_BUDGET` | `30` | Outside lookups per refresh |
| `CONDITION_RANKING_RECHECK_DAYS` | `365` | When a stored verdict is asked again |
| `CONDITION_RANKING_MIN_PUBMED_HITS` | `20` | Hits a quoted phrase needs in PubMed |
| `CONDITION_RANKING_REFRESH_SECONDS` | `3600` | How often `refresh_if_due` refreshes |
| `CONDITION_RANKING_REFRESH_TIMEOUT` | `180` | Seconds the refill actor waits on one refresh |
| `CONDITION_RANKING_LIMIT` | `25` | Rows per list on the public page |
| `ARENA_RANKINGS_PAGE_ENABLED` | `False` (`True` in Dev and tests) | Serve the public rankings page and link to it |
| `PUBLIC_RANKINGS_CACHE_SECONDS` | `900` | How long the page's numbers are cached |
| `PUBLIC_RANKINGS_MIN_SHOWN` | `10` | Votes before a model is ranked rather than warming up |
| `PUBLIC_RANKINGS_SINCE` | migration 0240's applied time | Earliest vote the public page counts; `""` counts all |

## Exports

`charts:chooser_ranked_export` (staff only) carries each candidate's
`votes` (ties split), `tie_count` and `flag_count`, so the preference data,
the ties and the fabrication flags travel together. `vote_count` is the
winning candidate's `votes` and `total_votes` the number of votes cast; the
case (summaries and `qa_pairs` included) rides along in `context`.

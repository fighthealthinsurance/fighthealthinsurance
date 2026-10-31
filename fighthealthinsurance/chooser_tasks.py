"""
Background task generation for the Chooser (Best-Of Selection) system.

This module provides utilities for:
- Checking and refilling the pool of READY ChooserTasks
- Generating synthetic candidates using existing ML pipelines

Note on external models: chooser tasks are built ONLY from synthetic,
model-generated scenarios (no patient data), so candidate generation opts
into external backends (``use_external=True``). The chooser exists precisely
to compare backends against each other — leaving external providers
(Anthropic, Azure, DeepInfra, ...) out of it meant they never appeared in the
selection UI or the model-usage dashboard, which is what happened when these
calls relied on the router's internal-only default.
"""

import asyncio
import datetime
import json
import random
import re
import threading
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, List, Optional, Tuple, cast

from django.conf import settings
from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from channels.db import database_sync_to_async
from loguru import logger

from fighthealthinsurance.context_utils import truncate_at_boundary
from fighthealthinsurance.ml import llm_usage, research_judging
from fighthealthinsurance.ml.ml_appeal_questions_helper import MLAppealQuestionsHelper
from fighthealthinsurance.ml.ml_metrics import ml_call_purpose_override
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.ml.model_identity import (
    SYNTHESIZED_MODEL_NAME,
    canonical_model_name,
)
from fighthealthinsurance.ml.question_parsing import (
    MAX_QUESTION_CHARS,
    is_junk_question,
)
from fighthealthinsurance import condition_rankings
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    ModelHealthAlertState,
    PubMedArticleSummarized,
    PubMedMiniArticle,
    PubMedQueryData,
)
from fighthealthinsurance.utils import fire_and_forget_in_new_threadpool

# Configuration for auto-refill thresholds
CHOOSER_MIN_READY_TASKS = getattr(settings, "CHOOSER_MIN_READY_TASKS", 50)
# Minimum number of READY synthetic tasks of each type that still have zero
# votes ("unscored"). The READY pool can stay large while every task has
# already been voted on, so we back-fill whenever fresh, unscored tasks run
# low to keep collecting preference data.
CHOOSER_MIN_UNSCORED_TASKS = getattr(settings, "CHOOSER_MIN_UNSCORED_TASKS", 5)
CHOOSER_GENERATION_BATCH_SIZE = getattr(settings, "CHOOSER_GENERATION_BATCH_SIZE", 10)
CHOOSER_NUM_CANDIDATES = getattr(settings, "CHOOSER_NUM_CANDIDATES", 4)
# Whether to add a synthesized candidate (combining the per-model outputs)
# to each task so the chooser can measure synthesis vs. single models.
CHOOSER_INCLUDE_SYNTHESIS = getattr(settings, "CHOOSER_INCLUDE_SYNTHESIS", True)
# Ceiling for the coverage trigger (see check_and_refill_task_pool): once this
# many fresh tasks exist, a backend that still has too few is not worth
# another batch. It sits above CHOOSER_MIN_READY_TASKS on purpose: a pool
# bootstrapped with no votes yet is all fresh, and a lower ceiling would turn
# the trigger off in exactly the pool it exists for, one built before a
# provider was configured.
CHOOSER_MAX_UNSCORED_TASKS = getattr(
    settings, "CHOOSER_MAX_UNSCORED_TASKS", 2 * CHOOSER_MIN_READY_TASKS
)
# How often the chat fallback may re-ask for a single question before the task
# is given up (see _ask_for_single_question).
CHOOSER_FALLBACK_ATTEMPTS = getattr(settings, "CHOOSER_FALLBACK_ATTEMPTS", 3)
# How many backends may write one task's scenario or conversation: a reply
# that can't be used goes to the next (see _scenario_writers).
CHOOSER_SCENARIO_WRITERS = getattr(settings, "CHOOSER_SCENARIO_WRITERS", 3)
# At most one background prefill per process per this many seconds (see
# trigger_prefill_async): a page load and an empty next-task fetch both ask.
CHOOSER_PREFILL_THROTTLE_SECONDS = getattr(
    settings, "CHOOSER_PREFILL_THROTTLE_SECONDS", 60
)
# How long a generation claim holds (see _claim_generation) before it lapses,
# so one left behind by a process that died mid-generation stops holding the
# type off. A batch renews its claim before each task, so this has to outlast
# one task's round of model calls, not a whole batch.
CHOOSER_GENERATION_CLAIM_SECONDS = getattr(
    settings, "CHOOSER_GENERATION_CLAIM_SECONDS", 15 * 60
)
# The fewest candidates a task needs to go READY.
_MIN_READY_CANDIDATES = 2
# Whether an appeal scenario is built around a real (treatment, condition)
# pair from condition_rankings (k-anonymous and vocabulary-checked, so
# nothing about any one person) rather than one the writer invents. Real
# pairs make the drafts argue about what people actually fight, and the
# PubMed searches already run for the pair are reused as the case's
# evidence, so a seeded task usually costs no NCBI call at all.
CHOOSER_SEED_FROM_RANKINGS = getattr(settings, "CHOOSER_SEED_FROM_RANKINGS", True)
# Articles attached to a scenario as the evidence the drafts may cite.
CHOOSER_EVIDENCE_LIMIT = getattr(settings, "CHOOSER_EVIDENCE_LIMIT", 5)
# Seconds a live PubMed search for a scenario's evidence may take.
CHOOSER_PUBMED_SEARCH_TIMEOUT = getattr(settings, "CHOOSER_PUBMED_SEARCH_TIMEOUT", 20.0)
# Seconds a case waits for the verdicts on its evidence (see
# _judge_evidence), Jev's and then our own model's, before going on without
# them; verdicts that landed in time are stored either way.
CHOOSER_EVIDENCE_JUDGING_TIMEOUT = getattr(
    settings, "CHOOSER_EVIDENCE_JUDGING_TIMEOUT", 60.0
)
# Of that, the most Jev is waited for when our own model could take over:
# Jev normally answers within seconds, so one that has not answered by then
# is unlikely to, and our own model, which writes its answers, needs the
# rest.
_JEV_SHARE_OF_JUDGING = 0.5
# Case facts kept per scenario, and the longest one.
CHOOSER_MAX_FACTS = 6
_MAX_FACT_CHARS = 240
# Evidence rows carry a PMID, a title, (when known) a link, and (when one is
# on file) a short summary: the summary real appeals were given for the
# article, else the start of its abstract. The voter is shown exactly what
# the drafts were given, summaries included.
_EVIDENCE_KEYS = ("pmid", "title", "url", "summary")
_EVIDENCE_SUMMARY_CHARS = 600
# The case-building pipeline that writes a new task of each type (stored as
# ChooserTask.case_version). Appeal cases at 2 also carry article summaries
# and a simulated question-and-answer session with the patient. Voters get
# the newest version first, and only tasks at the current version count as
# fresh (see _fresh_tasks), so the refill keeps the new kind coming while
# older tasks stay votable. Bump a type's version when its cases change in
# a way votes should be told apart by.
CASE_VERSIONS = {"appeal": 2, "chat": 1}
# The simulated interview (see _interview): how many of the app's questions
# the fictional patient is asked, how long the question generators may take,
# and the longest answer kept.
CHOOSER_MAX_QUESTIONS = getattr(settings, "CHOOSER_MAX_QUESTIONS", 4)
CHOOSER_QA_TIMEOUT = getattr(settings, "CHOOSER_QA_TIMEOUT", 60)
_MAX_ANSWER_CHARS = 400


def _case_version(task_type: str) -> int:
    """The version a new ``task_type`` task is built at (see CASE_VERSIONS)."""
    return CASE_VERSIONS.get(task_type, 1)


def _simulated_qa_allowed() -> bool:
    """Whether appeal cases get a simulated question-and-answer session.
    Read per call, like the live-search switch: off in tests, which would
    otherwise reach the question generators' real backends."""
    return bool(getattr(settings, "CHOOSER_SIMULATED_QA", True))


def _pubmed_live_search_allowed() -> bool:
    """Whether a scenario with no cached evidence may search PubMed live.
    Read per call so tests, and an operator short on NCBI quota, can turn
    it off; the seeded path rarely needs it."""
    return bool(getattr(settings, "CHOOSER_PUBMED_LIVE_SEARCH", True))


def _evidence_judging_allowed() -> bool:
    """Whether a case's evidence is judged as the case is built (see
    _judge_evidence), and our own model's verdicts shown to voters
    (annotate_evidence_verdicts). Read per call, like the live-search
    switch: off in tests. Jev judges only while research judging is on too
    (research_judging.enabled: the key and TYPESAFE_RESEARCH_JUDGING_ENABLED);
    our own model judges what Jev does not."""
    return bool(getattr(settings, "CHOOSER_JUDGE_EVIDENCE", True))


def _model_display_name(model) -> str:
    """The friendly registry name stamped by MLRouter, with a str() fallback
    (RemoteModelLike.__str__ never yields an opaque object repr)."""
    return getattr(model, "name", None) or str(model)


def _split_by_kind(models: List, seen: set) -> Tuple[List, List]:
    """``(internal, external)``: ``models`` deduped by friendly name, in
    router order, skipping the names already in ``seen``."""
    internal: List = []
    external: List = []
    for m in models:
        name = _model_display_name(m)
        if name in seen:
            continue
        seen.add(name)
        if getattr(m, "external", False):
            external.append(m)
        else:
            internal.append(m)
    return internal, external


# CHOOSER_FORCE_MODEL names already reported as unregistered, so a name for
# a provider this deployment has no key for is logged once per process, not
# on every task and refill tick.
_UNREGISTERED_FORCED_NAMES: set = set()


def _forced_arena_models(task_type: str) -> List:
    """The backends ``CHOOSER_FORCE_MODEL`` names that can answer a
    ``task_type`` task, one instance each, in the order named: a model being
    tried out, seated in every new arena task (see _select_candidate_models)
    so it collects votes against the usual rotation quickly. Names are the
    router's registered names, comma-separated: the keys of
    ``models_by_name`` (as FORCE_MODEL takes), or of
    ``chat_outside_models_by_name`` for a model kept out of every general
    pool (LunaRoute's, say), which the arena can try on its synthetic cases
    without it ever seeing a patient's. A name the router doesn't know is
    logged once and ignored. Read per call, so it takes effect at the next
    task. Unlike FORCE_MODEL, it touches nothing outside the arena and
    replaces no backend inside it.

    A chat task skips an appeal-text fine-tune (one that says it doesn't
    follow general instructions), as the router's chat list does: it would
    only waste the slot, and the coverage refill would keep building chat
    tasks waiting for a candidate it never writes. Past CHOOSER_NUM_CANDIDATES
    names the rest are dropped, with a warning, for the same reason: they
    would never be seated."""
    raw = getattr(settings, "CHOOSER_FORCE_MODEL", "") or ""
    names = raw.split(",") if isinstance(raw, str) else list(raw)
    forced: List = []
    for name in dict.fromkeys(n.strip() for n in names if n and n.strip()):
        # A healthy instance first, as a call by name gets: a forced model
        # whose first instance is down would write nothing.
        instances = ml_router.healthy_first(ml_router.models_by_name.get(name) or [])
        model = (
            instances[0]
            if instances
            else ml_router.chat_outside_models_by_name.get(name)
        )
        if model is None:
            if name not in _UNREGISTERED_FORCED_NAMES:
                _UNREGISTERED_FORCED_NAMES.add(name)
                logger.warning(
                    f"CHOOSER_FORCE_MODEL names {name!r}, which is not a "
                    "registered backend; ignoring it"
                )
            continue
        if getattr(model, "context_only", False) is True:
            # Builds context (Perplexity's citations); never writes a draft.
            logger.debug(
                f"CHOOSER_FORCE_MODEL {name!r} is context-only; not forcing it "
                "into arena tasks"
            )
            continue
        supports = getattr(model, "supports_general_instructions", None)
        if task_type == "chat" and supports is not None and not supports():
            logger.debug(
                f"CHOOSER_FORCE_MODEL {name!r} answers appeals only; not "
                "forcing it into chat tasks"
            )
            continue
        forced.append(model)
    if len(forced) > CHOOSER_NUM_CANDIDATES:
        logger.warning(
            f"CHOOSER_FORCE_MODEL names {len(forced)} backends for "
            f"{CHOOSER_NUM_CANDIDATES} candidate slots; forcing only "
            f"{[_model_display_name(m) for m in forced[:CHOOSER_NUM_CANDIDATES]]}"
        )
        forced = forced[:CHOOSER_NUM_CANDIDATES]
    return forced


def _select_candidate_models(
    models: List, limit: int, forced: Optional[List] = None
) -> List:
    """Pick up to ``limit`` distinct-by-name backends for candidate generation.

    The router's backend lists intentionally repeat models (e.g. the primary
    fhi model twice for chat) and lead with internal models; taking a plain
    prefix slice would burn candidate slots on duplicates and leave external
    backends unmeasured. Instead: dedupe by friendly name, then alternate
    internal/external so both pools get compared, shuffling the external pool
    so different external providers get coverage across tasks.

    ``forced`` backends (CHOOSER_FORCE_MODEL) take the first slots, whatever
    their kind, and the rest are filled as above.
    """
    selected: List = []
    seen: set = set()
    for model in forced or []:
        name = _model_display_name(model)
        if name not in seen and len(selected) < limit:
            seen.add(name)
            selected.append(model)
    internal, external = _split_by_kind(models, seen)
    # Randomize external order for coverage across tasks; keep the internal
    # ordering (cost/quality ranked by the router).
    random.shuffle(external)
    i = e = 0
    while len(selected) < limit and (i < len(internal) or e < len(external)):
        if i < len(internal):
            selected.append(internal[i])
            i += 1
        if len(selected) < limit and e < len(external):
            selected.append(external[e])
            e += 1
    return selected


def _spare_candidate_models(models: List, seated: List) -> List:
    """The backends of ``models`` that ``seated`` left out, deduped by name,
    externals first: the ones the retry pass gives a failed backend's slot
    to (see _next_retry_model). Externals lead so that a dead external's
    slot stays external and the internal/external balance holds."""
    internal, external = _split_by_kind(
        models, {_model_display_name(m) for m in seated}
    )
    random.shuffle(external)
    return external + internal


def _next_retry_model(
    spares: List, produced: dict[int, Any], failed: set[int], have: int
) -> Optional[Any]:
    """The backend the retry pass asks next, or None when none should be.

    A backend that gave nothing usable or raised in this task is never asked
    again: it is usually down, and each re-ask was another paid call and
    warning, on every task. An unused backend fills the slot first
    (``spares`` is consumed in order). One that already answered is asked
    again only while the task's ``have`` candidates are short of the ones it
    needs to go READY: two of its drafts in one task make the usage
    dashboard charge it two presentations per vote it can win at most once.
    ``produced`` maps id to model, in the order they answered.
    """
    if spares:
        return spares.pop(0)
    if have < _MIN_READY_CANDIDATES:
        return next(
            (m for key, m in produced.items() if key not in failed),
            None,
        )
    return None


_LABEL_MARKUP_RE = re.compile(r"^[\s\-\*\u2022\d\.\)\(#>]+")
_OPENING_EMPHASIS_RE = re.compile(r"^[*_`]+")
_TRAILING_EMPHASIS_RE = re.compile(r"\s*([*_`]+)$")


def _strip_label_emphasis(value: str) -> str:
    """``value`` without the markdown emphasis its label left on it.

    That is the closing half of an emphasised label ("**Procedure:** MRI"),
    the closing half of a line emphasised whole ("**Procedure: MRI**"), and
    a pair wrapping the whole value ("Procedure: **MRI**"). Emphasis inside
    the value is the model's text and stays: stripping markers from both
    ends stored "**MRI** of lumbar spine" as "MRI** of lumbar spine" and
    "What is *prior auth*" as "What is *prior auth".
    """
    value = value.strip()
    opening = _OPENING_EMPHASIS_RE.match(value)
    if opening and value[opening.end() :][:1].isspace():
        value = value[opening.end() :].strip()
    opening = _OPENING_EMPHASIS_RE.match(value)
    if opening:
        marker, closing = opening.group(0), opening.group(0)[::-1]
        inner = value[len(marker) : len(value) - len(closing)]
        if (
            inner.strip()
            and value.endswith(closing)
            and marker not in inner
            and closing not in inner
        ):
            return inner.strip()
    trailing = _TRAILING_EMPHASIS_RE.search(value)
    if trailing and trailing.group(1) not in value[: trailing.start()]:
        value = value[: trailing.start()]
    return value.strip()


def _split_label(raw: str) -> Optional[Tuple[str, str]]:
    """``(label, value)`` for a "Label: value" line, else None.

    List bullets, numbering and markdown emphasis around the label are
    dropped, so "**Procedure:** MRI", "**Procedure**: MRI", "1. Procedure:
    MRI" and "- Procedure: MRI" all read as ("Procedure", "MRI"). Models
    decorate labels freely; the old parser required the bare form and
    treated every other spelling as a missing field.
    """
    line = _LABEL_MARKUP_RE.sub("", raw.strip())
    if ":" not in line:
        return None
    label, value = line.split(":", 1)
    return label.strip().strip("*_` ").strip(), _strip_label_emphasis(value)


def _labeled_fields(text: str) -> dict:
    """``{label: value}`` for every "Label: value" line of ``text``. Labels
    are lower-cased with spaces as underscores; see _split_label for the
    markdown that is dropped."""
    fields: dict = {}
    for raw in text.split("\n"):
        split = _split_label(raw)
        if split is None:
            continue
        key = split[0].lower().replace(" ", "_")
        if key and split[1]:
            fields[key] = split[1]
    return fields


def _parse_conversation(text: str):
    """``(history, final_user_prompt)`` from a USER:/ASSISTANT: transcript.

    Every turn goes into the history in order, and only a trailing user turn
    comes back out, as the question the candidates answer (None when the
    transcript ends with the assistant). A user turn used to be held back
    whenever it followed an assistant turn, so in a transcript with two
    answered follow-ups the middle question was dropped and two assistant
    turns sat side by side. Labels may carry list or markdown decoration
    ("**USER:**", "**User**:", see _split_label); continuation lines are kept
    as written.
    """
    history: list = []
    current_role = None
    current_content: list = []

    def flush():
        content = " ".join(current_content).strip()
        if current_role and content:
            history.append({"role": current_role, "content": content})

    for raw in text.strip().split("\n"):
        split = _split_label(raw)
        role = split[0].lower() if split else None
        if split and role in ("user", "assistant"):
            flush()
            current_role = role
            current_content = [split[1]]
        elif current_role and raw.strip():
            current_content.append(raw.strip())
    flush()
    if not history or history[-1]["role"] != "user":
        return history, None
    final_user_prompt = history.pop()["content"]
    return history, final_user_prompt


_FACT_SPLIT_RE = re.compile(r"\s*;\s*|\s*\n\s*")
# The list markup a fact may start with: bullets, and a list number followed
# by a period or bracket and a space. Not every leading digit, as the label
# regex strips: "45-55 years old" and "6 months of symptoms" are facts whose
# first word is a number, and the old strip left "years old".
_FACT_MARKUP_RE = re.compile(r"^(?:[\s\-\*\u2022>#]+|\d{1,2}[.)]\s+)+")
_FACT_BULLET_RE = re.compile(r"^\s*(?:[\-\*\u2022]|\d{1,2}[.)])\s+")


def _clean_fact(raw: str) -> str:
    """One case fact without list markup, emphasis or a trailing period,
    cut to _MAX_FACT_CHARS."""
    fact = _strip_label_emphasis(_FACT_MARKUP_RE.sub("", raw.strip()))
    fact = fact.replace("**", "").replace("__", "").strip().rstrip(".").strip()
    return fact[:_MAX_FACT_CHARS]


def _facts_from_response(text: str) -> List[str]:
    """The case facts a scenario writer gave, as a list.

    Asked for on one line separated by semicolons, but models also answer
    with a bullet list under the label; both are read. Up to
    CHOOSER_MAX_FACTS distinct facts, in order; none when the label is
    missing, so an older-style scenario still parses.
    """
    inline = _labeled_fields(text).get("case_facts", "")
    pieces: List[str] = [p for p in _FACT_SPLIT_RE.split(inline) if p.strip()]
    if not pieces:
        collecting = False
        for raw in text.split("\n"):
            split = _split_label(raw)
            if split is not None and split[0].lower().replace(" ", "_") == "case_facts":
                collecting = True
                if split[1]:
                    pieces.extend(p for p in _FACT_SPLIT_RE.split(split[1]) if p)
                continue
            if not collecting:
                continue
            if _FACT_BULLET_RE.match(raw):
                # A bullet is a fact even when it reads "Age range: 45-55".
                pieces.append(raw)
                continue
            if split is not None and split[1]:
                # The next labelled field ends the list.
                break
            if raw.strip():
                pieces.append(raw)
    facts: List[str] = []
    for piece in pieces:
        fact = _clean_fact(piece)
        if len(fact) >= 3 and fact.lower() not in {f.lower() for f in facts}:
            facts.append(fact)
        if len(facts) >= CHOOSER_MAX_FACTS:
            break
    return facts


async def _pick_seed_pair() -> Optional[condition_rankings.SeedPair]:
    """A real (treatment, condition) pair to build the scenario around, or
    None: seeding is off, the pool is empty (no refresh has run, or nothing
    has cleared k-anonymity and the vocabularies yet), or it could not be
    read. The writer then invents the pair as before."""
    if not CHOOSER_SEED_FROM_RANKINGS:
        return None
    try:
        pairs = await condition_rankings.seed_pairs()
    except Exception as e:
        logger.warning(f"Chooser: could not read the seed pairs: {e}")
        return None
    if not pairs:
        return None
    return random.choice(pairs)


async def _cached_pmids(query: str) -> List[str]:
    """PMIDs earlier PubMed searches found for ``query``, newest search
    first, from the denial-less cache rows every search writes (any age:
    for a synthetic case a two-year-old search is as good as today's).
    Nothing is sent to NCBI."""
    pmids: List[str] = []
    rows = (
        PubMedQueryData.objects.filter(query__iexact=query, denial_id__isnull=True)
        .exclude(articles__isnull=True)
        .exclude(articles="")
        # Newest first; the id breaks the tie two searches in one
        # millisecond would otherwise leave to row order.
        .order_by("-created", "-internal_id")[:6]
    )
    async for row in rows:
        try:
            found = json.loads((row.articles or "").replace("\x00", ""))
        except json.JSONDecodeError:
            continue
        for pmid in found or []:
            if pmid and str(pmid) not in pmids:
                pmids.append(str(pmid))
    return pmids


async def _store_titles(pmids: List[str]) -> None:
    """Fetch and store a title for each PMID that has none, so the articles
    a live search found can be listed. Bounded by
    CHOOSER_PUBMED_SEARCH_TIMEOUT; whatever isn't fetched in time is left
    out of the evidence, as before."""
    have = {
        pmid
        async for pmid in PubMedMiniArticle.objects.filter(pmid__in=pmids)
        .exclude(title__isnull=True)
        .exclude(title="")
        .values_list("pmid", flat=True)
    }
    missing = [pmid for pmid in pmids if pmid not in have]
    if not missing:
        return
    from asgiref.sync import sync_to_async

    from fighthealthinsurance.utils import pubmed_fetcher

    async def fetch_all() -> None:
        for pmid in missing:
            try:
                # NCBI call only, no ORM: plain asgiref bridge.
                fetched = await sync_to_async(pubmed_fetcher.article_by_pmid)(pmid)
            except Exception as e:
                logger.debug(f"Chooser: fetching PubMed article {pmid} failed: {e}")
                continue
            title = (getattr(fetched, "title", None) or "") if fetched else ""
            title = title.replace("\x00", "").strip()
            if not title:
                continue
            abstract = (getattr(fetched, "abstract", None) or "").replace("\x00", "")
            updated = await PubMedMiniArticle.objects.filter(pmid=pmid).aupdate(
                title=title
            )
            if not updated:
                await PubMedMiniArticle.objects.acreate(
                    pmid=pmid, title=title, abstract=abstract
                )
            elif abstract:
                # The abstract is what the case summarizes the article from
                # when no appeal has summarized it yet.
                await PubMedMiniArticle.objects.filter(
                    Q(abstract__isnull=True) | Q(abstract=""), pmid=pmid
                ).aupdate(abstract=abstract)

    try:
        await asyncio.wait_for(fetch_all(), timeout=CHOOSER_PUBMED_SEARCH_TIMEOUT)
    except asyncio.TimeoutError:
        logger.debug("Chooser: ran out of time fetching PubMed titles")


async def _summaries_for(pmids: List[str]) -> dict:
    """``{pmid: summary}`` for the articles an appeal already summarized
    (PubMedArticleSummarized.basic_summary, what real appeals are given as
    their PubMed context). Nothing is fetched or summarized here."""
    summaries: dict = {}
    async for pmid, summary in (
        PubMedArticleSummarized.objects.filter(pmid__in=pmids)
        .exclude(basic_summary__isnull=True)
        .exclude(basic_summary="")
        .values_list("pmid", "basic_summary")
    ):
        summaries.setdefault(pmid, summary)
    return summaries


def _evidence_summary(summary: Optional[str], abstract: Optional[str]) -> str:
    """What the case says an article found: its stored summary, else the
    start of its abstract, cut at a sentence or word boundary."""
    text = " ".join((summary or abstract or "").replace("\x00", "").split())
    return truncate_at_boundary(text, _EVIDENCE_SUMMARY_CHARS)


async def _judge_evidence(
    procedure: str, diagnosis: str, articles: List[PubMedMiniArticle]
) -> None:
    """Have a case's articles judged for its pair (is each about this
    treatment for this condition, does it support it, does it argue against
    it), storing the answers in PubMedArticleJudgment the way a real
    appeal's research context does (pubmed_tools.judge_missing; a judgment
    already stored for the pair is not asked for again). The voter's page
    reads the stored judgments back (annotate_evidence_verdicts). Nothing is
    kept on the case itself: the drafts are given the summaries and never
    the verdicts, so the stored case stays exactly what they were shown, and
    a draft that leans on a study judged against the treatment is doing so
    on its own reading of the summary.

    Jev judges first, while research judging is on. Whatever it does not
    judge (it is off or not set up, a request failed, its budget is spent,
    or it ran out of its share of the time) falls through to our own best
    internal model, with a warning: the arena's verdicts are a referee aid,
    worth having from a rougher reader than none. A judgment only our own
    model made is asked of Jev again once Jev is back, and Jev's wins.

    The pair is k-anonymous and the case synthetic, so there is no consent
    to ask; what is sent is the article's public title and abstract and the
    pair, as for a real appeal. Best effort, bounded by
    CHOOSER_EVIDENCE_JUDGING_TIMEOUT: a case whose evidence could not be
    judged is still worth a vote, without verdicts."""
    if not articles or not _evidence_judging_allowed():
        return
    from fighthealthinsurance import pubmed_tools

    loop = asyncio.get_running_loop()
    deadline = loop.time() + CHOOSER_EVIDENCE_JUDGING_TIMEOUT
    jev_on = research_judging.enabled()
    key = research_judging.treatment_key(procedure, diagnosis)
    pmids = [a.pmid for a in articles if a.pmid]
    try:
        by_jev: dict = {}
        by_ours: dict = {}
        async for row in pubmed_tools.judgment_rows(
            pmids, key, jev=jev_on, own_models=True
        ):
            judged = (
                by_jev
                if row.scorer.startswith(research_judging.JEV_SCORER_PREFIX)
                else by_ours
            )
            judged[row.pmid] = pubmed_tools.judgment_of(row)
        if all(pmid in (by_jev if jev_on else by_ours) for pmid in pmids):
            return
        # The article is what is read. One stored without an abstract (an
        # older row, or a fetch that brought only the title) may have it on
        # the row an appeal summarized.
        without = [a.pmid for a in articles if not (a.abstract or "").strip()]
        abstracts = {
            pmid: abstract
            async for pmid, abstract in PubMedArticleSummarized.objects.filter(
                pmid__in=without
            )
            .exclude(abstract__isnull=True)
            .exclude(abstract="")
            .values_list("pmid", "abstract")
        }
        to_judge = [
            SimpleNamespace(
                pmid=a.pmid,
                title=a.title,
                abstract=(a.abstract or "").strip() or abstracts.get(a.pmid, ""),
            )
            for a in articles
        ]
        ours = ml_router.best_internal_model()
        if jev_on:
            jev_budget = CHOOSER_EVIDENCE_JUDGING_TIMEOUT * (
                _JEV_SHARE_OF_JUDGING if ours is not None else 1.0
            )
            try:
                # judge_missing adds to by_jev as each answer lands, so
                # what Jev answered in time is kept past a timeout.
                await asyncio.wait_for(
                    pubmed_tools.judge_missing(to_judge, procedure, diagnosis, by_jev),
                    timeout=jev_budget,
                )
            except asyncio.TimeoutError:
                logger.info("Chooser: Jev ran out of time judging a case's evidence")
        known = {**by_ours, **by_jev}
        left = [
            a.pmid for a in to_judge if a.pmid and a.pmid not in known and a.abstract
        ][: research_judging.MAX_ARTICLES]
        if not left:
            return
        why = (
            f"Jev did not judge {len(left)} of a case's articles"
            if jev_on
            else "Jev is not set up for research judging (TYPESAFE_API_KEY "
            "and TYPESAFE_RESEARCH_JUDGING_ENABLED)"
        )
        if ours is None:
            logger.warning(
                f"Chooser: {why}, and there is no internal model to judge "
                f"them instead; {len(left)} article(s) go without a verdict"
            )
            return
        logger.warning(
            f"Chooser: {why}; falling back to our own model "
            f"{_model_display_name(ours)} for {len(left)} article(s)"
        )
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.info("Chooser: no time left to judge a case's evidence")
            return
        await asyncio.wait_for(
            pubmed_tools.judge_missing(
                to_judge, procedure, diagnosis, known, own_model=ours
            ),
            timeout=remaining,
        )
    except asyncio.TimeoutError:
        logger.info("Chooser: ran out of time judging a case's evidence")
    except Exception as e:
        logger.opt(exception=True).warning(
            f"Chooser: could not judge a case's evidence: {e}"
        )


def annotate_evidence_verdicts(context: dict) -> dict:
    """The case as the voter is shown it: ``context`` with the stored
    verdict on each article of its evidence, as ``judgment``
    (research_judging.Judgment.as_dict: the verdict in a word and its three
    probabilities), for every article judged for the case's treatment and
    condition under the current rubric: Jev's, else our own model's
    (_judge_evidence).

    Read when the case is served rather than stored with it, so a case
    built before an article was judged (by this module or by a real appeal
    about the pair) shows the verdict too, and the stored case stays exactly
    what the drafts were given: they are told the summaries, never the
    verdicts. Reads only and sends nothing. Jev's verdicts are shown only
    while research judging is switched on, and our own model's only while
    CHOOSER_JUDGE_EVIDENCE is (pubmed_tools.stored_judgments_for: each
    switch is the kill switch for its stored judgments). Returns a new dict;
    ``context`` is left as it is."""
    evidence = context.get("evidence") or []
    pmids = [
        str(item["pmid"])
        for item in evidence
        if isinstance(item, dict) and item.get("pmid")
    ]
    if not pmids:
        return context
    from fighthealthinsurance.pubmed_tools import stored_judgments_for

    judgments = stored_judgments_for(
        context.get("procedure"),
        context.get("diagnosis"),
        pmids,
        own_models=_evidence_judging_allowed(),
    )
    if not judgments:
        return context
    annotated = []
    for item in evidence:
        judgment = (
            judgments.get(str(item.get("pmid"))) if isinstance(item, dict) else None
        )
        annotated.append(
            {**item, "judgment": judgment.as_dict()} if judgment is not None else item
        )
    return {**context, "evidence": annotated}


async def _evidence_for(procedure: str, diagnosis: str) -> List[dict]:
    """The evidence a scenario's drafts may cite: up to CHOOSER_EVIDENCE_LIMIT
    articles for the procedure and diagnosis, each a PMID, a title, a link
    and, when one is on file, a summary (see _evidence_summary). Cached
    searches first; a live search only when nothing is cached and
    CHOOSER_PUBMED_LIVE_SEARCH allows it. With live search allowed, articles
    with no stored title have it fetched; otherwise they are skipped: the
    list is for the voter to check citations against, so every row must be
    readable. The articles kept are then judged for the pair when judging
    is on (_judge_evidence), which stores the verdicts but adds nothing
    here."""
    query = f"{procedure} {diagnosis}".strip()
    if not query:
        return []
    pmids = await _cached_pmids(query)
    live = _pubmed_live_search_allowed()
    if not pmids and live:
        from fighthealthinsurance.pubmed_tools import PubMedTools

        try:
            pmids = [
                str(p)
                for p in await PubMedTools().find_pubmed_article_ids_for_query(
                    query, since=None, timeout=CHOOSER_PUBMED_SEARCH_TIMEOUT
                )
                if p
            ]
        except Exception as e:
            logger.warning(f"Chooser: live PubMed search failed for a scenario: {e}")
            pmids = []
    if live:
        # A search stores only the PMIDs; without their titles every one
        # would be skipped below. Titles a fetch missed (it timed out or
        # failed) are tried again the next time the pair is used.
        await _store_titles(pmids[: CHOOSER_EVIDENCE_LIMIT * 3])
    if not pmids:
        return []
    titles = {
        article.pmid: article
        async for article in PubMedMiniArticle.objects.filter(
            pmid__in=pmids[: CHOOSER_EVIDENCE_LIMIT * 3]
        )
        .exclude(title__isnull=True)
        .exclude(title="")
    }
    summaries = await _summaries_for(list(titles))
    evidence: List[dict] = []
    for pmid in pmids:
        article = titles.get(pmid)
        if article is None:
            continue
        item = {
            "pmid": pmid,
            "title": (article.title or "").strip()[:300],
            "url": article.article_url or f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        }
        summary = _evidence_summary(summaries.get(pmid), article.abstract)
        if summary:
            item["summary"] = summary
        evidence.append(item)
        if len(evidence) >= CHOOSER_EVIDENCE_LIMIT:
            break
    # Jev's verdict on each article, stored for the voter's page; the
    # drafts are not told it (see _judge_evidence).
    await _judge_evidence(
        procedure, diagnosis, [titles[item["pmid"]] for item in evidence]
    )
    return evidence


def _scenario_prompt(seed: Optional[condition_rankings.SeedPair]) -> str:
    """What the scenario writer is asked. With a seed, the procedure and
    diagnosis are given and the writer fills in the rest of the case."""
    if seed is None:
        pair_lines = (
            "Procedure: [a specific real medical procedure name, e.g., MRI of lumbar spine, arthroscopic knee surgery]\n"
            "Diagnosis: [a specific real medical diagnosis, e.g., chronic lower back pain, torn ACL]\n"
        )
        intro = "Please generate a varied, realistic but completely fictional health insurance denial scenario.\n\n"
    else:
        pair_lines = f"Procedure: {seed.treatment}\n" f"Diagnosis: {seed.condition}\n"
        intro = (
            "Please generate a realistic but completely fictional health insurance denial scenario "
            f"about this exact procedure and diagnosis (copy them verbatim): {seed.treatment} for {seed.condition}.\n\n"
        )
    return (
        "We are building a system to help patients appeal health insurance denials. "
        "To improve our model selection techniques, we need realistic synthetic denial scenarios. "
        + intro
        + "Provide the following in EXACTLY this structured format (one field per line):\n"
        + pair_lines
        + "Insurance Company: [a fictional but realistic-sounding insurance company name]\n"
        "Denial Reason: [a 1-2 sentence realistic denial reason, such as 'not medically necessary', 'experimental treatment', 'out of network', etc.]\n"
        "Case Facts: [3 to 5 short facts about the fictional patient and case on this one line, separated by semicolons: "
        "an age range, how long the symptoms have lasted, what was tried already and what happened, relevant history, "
        "and what the treating clinician documented. No names, no dates, no member or claim numbers.]\n\n"
        "Be creative and varied. Use real medical terminology for procedures and diagnoses. "
        "The case facts are the only facts an appeal may rely on, so make them concrete."
    )


def _scenario_writers(models: List) -> List:
    """The backends that write a task's synthetic scenario or conversation,
    in the order they are asked.

    The router's generation list starts with the cheapest internal backend,
    which in production is the fhi-legacy appeal fine-tune. It does not
    follow instructions (its own docstring: blank lines and stray digits), so
    every scenario request either came back empty, which raised in the parser
    and disabled the task before a single candidate existed, or came back as
    text no field could be read from. These are the backends that say they
    follow general instructions, up to ``CHOOSER_SCENARIO_WRITERS`` of them,
    and a reply that can't be used goes to the next: with one writer, a
    backend answering junk disabled every task while the backends behind it
    were never asked. When none says it follows instructions, the first
    backend writes, rather than none.
    """
    general = []
    for model in models:
        supports = getattr(model, "supports_general_instructions", None)
        if supports is None or supports():
            general.append(model)
    if general:
        return general[: max(1, CHOOSER_SCENARIO_WRITERS)]
    logger.warning(
        "Chooser: no general-purpose backend is registered; writing the "
        f"scenario with {_model_display_name(models[0])}"
    )
    return models[:1]


async def _ask_writer(writer, system_prompt: str, prompt: str) -> Optional[str]:
    """``writer``'s reply, or None when it gave none. A writer that raised
    gave none too, so the next writer is asked rather than the task failing."""
    try:
        reply = await writer._infer_no_context(
            system_prompts=[system_prompt], prompt=prompt
        )
    except Exception as e:
        logger.opt(exception=True).warning(
            f"Chooser: {_model_display_name(writer)} raised instead of "
            f"answering: {e}"
        )
        return None
    return str(reply) if reply else None


def _denial_letter(context: dict) -> str:
    """The synthetic denial in the few lines the question generators read as
    the letter: they want a denial text, and a case has only its parts."""
    procedure = context.get("procedure") or "the requested care"
    diagnosis = context.get("diagnosis")
    insurer = context.get("insurance_company") or "The plan"
    letter = f"{insurer} denied coverage of {procedure}"
    if diagnosis:
        letter += f" for {diagnosis}"
    reason = context.get("denial_text_preview")
    return f"{letter}. Reason given: {reason}" if reason else f"{letter}."


async def _case_questions(context: dict) -> List[str]:
    """The questions the app would ask this patient, from the generators the
    appeal flow itself uses (MLAppealQuestionsHelper): the case-specific
    ones, given the denial and the case facts as the patient's history, and
    the generic ones for the pair when those come back empty. Up to
    CHOOSER_MAX_QUESTIONS, junk and repeats dropped; the generators' own
    suggested answers are not kept, since the patient answers them all.
    Labelled "other" in the call metrics, like the rest of the chooser's
    synthetic traffic."""
    procedure = context.get("procedure") or ""
    diagnosis = context.get("diagnosis") or ""
    facts = "; ".join(str(f) for f in (context.get("facts") or []) if f)
    with ml_call_purpose_override("other"):
        found = await MLAppealQuestionsHelper.generate_specific_questions(
            denial_text=_denial_letter(context),
            patient_context=facts or None,
            procedure=procedure,
            diagnosis=diagnosis,
            timeout=CHOOSER_QA_TIMEOUT,
            # Synthetic case: external backends allowed (module docstring).
            use_external=True,
        )
        if not found:
            found = await MLAppealQuestionsHelper.generate_generic_questions(
                procedure=procedure,
                diagnosis=diagnosis,
                timeout=CHOOSER_QA_TIMEOUT,
                use_external=True,
            )
    questions: List[str] = []
    for row in found or []:
        question = str(row[0] if isinstance(row, (list, tuple)) else row).strip()
        if (
            not question
            or len(question) > MAX_QUESTION_CHARS
            or is_junk_question(question)
            or question.lower() in {q.lower() for q in questions}
        ):
            continue
        questions.append(question)
        if len(questions) >= CHOOSER_MAX_QUESTIONS:
            break
    return questions


_PATIENT_SYSTEM_PROMPT = (
    "You role-play a fictional patient answering their appeal assistant's "
    "questions, for a synthetic case used to compare appeal-writing models. "
    "Answer plainly, in the first person, and never contradict the case."
)


def _interview_prompt(context: dict, questions: List[str]) -> str:
    """What the fictional patient is asked: the case, then the questions."""
    lines = [
        "Here is a fictional patient's health insurance denial:",
        f"Procedure: {context.get('procedure') or ''}",
        f"Diagnosis: {context.get('diagnosis') or ''}",
        f"Insurance Company: {context.get('insurance_company') or ''}",
        f"Denial Reason: {context.get('denial_text_preview') or ''}",
    ]
    facts = [str(f) for f in (context.get("facts") or []) if f]
    if facts:
        lines.append("Case facts:")
        lines.extend(f"- {fact}" for fact in facts)
    lines.append("\nThe patient's appeal assistant asked them:")
    lines.extend(f"Q{i}: {q}" for i, q in enumerate(questions, start=1))
    lines.append(
        "\nAnswer each question as this patient would, in one or two plain "
        "sentences. Stay consistent with the case facts; where they do not "
        "settle a question, give a realistic answer that fits the case, or say "
        "the patient does not know. No names, no dates, no member, claim or "
        "phone numbers.\n"
        "Reply in EXACTLY this format, one line per question:"
    )
    lines.extend(
        f"A{i}: [the patient's answer to Q{i}]" for i in range(1, len(questions) + 1)
    )
    return "\n".join(lines)


def _clean_answer(raw: str) -> str:
    """One answer without markdown or wrapping quotes, cut to
    _MAX_ANSWER_CHARS; "" for an echo of the format's placeholder."""
    answer = _strip_label_emphasis(raw).replace("**", "").strip().strip('"').strip()
    if answer.startswith("["):
        return ""
    return truncate_at_boundary(answer, _MAX_ANSWER_CHARS)


def _answers_from_response(text: str, questions: List[str]) -> List[dict]:
    """The ``{"question", "answer"}`` pairs in the patient's reply, in the
    questions' order; a question left unanswered is left out."""
    fields = _labeled_fields(text)
    pairs: List[dict] = []
    for i, question in enumerate(questions, start=1):
        answer = _clean_answer(fields.get(f"a{i}", ""))
        if len(answer) >= 2:
            pairs.append({"question": question, "answer": answer})
    return pairs


async def _interview(task: ChooserTask, context: dict, writers: List) -> List[dict]:
    """A simulated question-and-answer session with the case's patient: the
    app's own questions for the case (_case_questions), answered in
    character by the first scenario writer that answers usably. The drafts
    get the answers as facts about the patient, and the voter sees them.
    Best effort: nothing comes back when the switch is off
    (CHOOSER_SIMULATED_QA), no question came back, or no writer answered,
    and the case goes on without one."""
    if not _simulated_qa_allowed():
        return []
    try:
        questions = await _case_questions(context)
    except Exception as e:
        logger.opt(exception=True).warning(
            f"ChooserTask {task.id}: could not generate questions: {e}"
        )
        return []
    if not questions:
        logger.info(f"ChooserTask {task.id}: no questions for the interview")
        return []
    prompt = _interview_prompt(context, questions)
    for writer in writers:
        pairs = _answers_from_response(
            await _ask_writer(writer, _PATIENT_SYSTEM_PROMPT, prompt) or "", questions
        )
        if pairs:
            return pairs
        logger.info(
            f"ChooserTask {task.id}: {_model_display_name(writer)} gave no "
            "usable interview answers"
        )
    return []


def _usable_question(question: Optional[str]) -> bool:
    """Long enough to be a question, and short enough to be one message."""
    return question is not None and 10 <= len(question) <= 1000


async def _ask_for_single_question(model, prompt: str) -> Optional[str]:
    """Up to ``CHOOSER_FALLBACK_ATTEMPTS`` calls; None when none answered.

    The model call returns None on every way of not answering (transport
    failure, cooldown, 429 back-off) rather than raising, so the retry has to
    be bounded here: unbounded, it spun the refill thread forever against a
    backend that was down.
    """
    for attempt in range(1, CHOOSER_FALLBACK_ATTEMPTS + 1):
        answer = await _ask_writer(
            model, "Generate a natural user question about health insurance.", prompt
        )
        if answer and answer.strip():
            return answer
        logger.info(
            f"Chooser: {_model_display_name(model)} returned no question "
            f"(attempt {attempt}/{CHOOSER_FALLBACK_ATTEMPTS})"
        )
    return None


def _note_unusable_candidate(task: ChooserTask, model, kind: str, response) -> None:
    """A backend answered with nothing usable. Logged, because the retry
    pass gives the slot to another backend: without this a task whose
    external backends both failed went READY with internal candidates only
    and no trace of why."""
    size = len(response.strip()) if isinstance(response, str) else 0
    logger.info(
        f"Chooser task {task.id}: {_model_display_name(model)} returned no "
        f"usable {kind} candidate ({size} chars)"
    )


async def _fill_candidates(
    task: ChooserTask,
    task_type: str,
    kind: str,
    min_len: int,
    ask: Callable[[Any, bool], Awaitable[Optional[str]]],
    base_metadata: dict,
) -> None:
    """Seat up to ``CHOOSER_NUM_CANDIDATES`` ``kind`` candidates on ``task``,
    then the synthesized one.

    The appeal and chat generators differ only in how a draft is asked for,
    so the bookkeeping that decides who is asked (and so which models the
    votes can measure) lives here once. ``ask(model, resample)`` returns the
    model's reply, or None; ``resample`` is set when the model already seated
    a candidate in this task. Each selected backend is asked once, then the
    open slots go to the backends _next_retry_model picks. A reply longer
    than ``min_len`` is stored with ``base_metadata``, plus ``retry: True``
    on a re-sample only: a spare backend's first draft is not a retry.
    """
    # Drawn from the same list the coverage refill checks, so the two agree.
    comparable = _comparable_backends(task_type)
    models = _select_candidate_models(
        comparable, CHOOSER_NUM_CANDIDATES, _forced_arena_models(task_type)
    )
    if not models:
        logger.warning(f"No models available for {task_type} candidate generation")
        return
    logger.debug(
        f"Chooser {task_type} candidates for task {task.id} using models: "
        f"{[_model_display_name(m) for m in models]}"
    )

    candidate_index = 0
    # By id: the models that seated a candidate (in the order they did) and
    # the ones that gave nothing usable or raised; see _next_retry_model.
    produced: dict[int, Any] = {}
    failed: set[int] = set()

    async def attempt(model, resample: bool) -> None:
        nonlocal candidate_index
        try:
            response = await ask(model, resample)
            if response and len(response.strip()) > min_len:
                metadata = dict(base_metadata)
                if resample:
                    metadata["retry"] = True
                await ChooserCandidate.objects.acreate(
                    task=task,
                    candidate_index=candidate_index,
                    kind=kind,
                    # None only for a None model, which is never asked.
                    model_name=canonical_model_name(model) or "",
                    content=response.strip(),
                    metadata=metadata,
                )
                produced[id(model)] = model
                candidate_index += 1
                task.num_candidates_generated = candidate_index
            else:
                _note_unusable_candidate(task, model, task_type, response)
                failed.add(id(model))
        except Exception as e:
            retry = " (retry)" if resample else ""
            logger.warning(
                f"Error generating {task_type} candidate{retry} with model {model}: {e}"
            )
            failed.add(id(model))

    # First pass: try each model once
    for model in models:
        await attempt(model, resample=False)

    # If we don't have enough candidates, give the open slots to backends
    # not asked yet, then (only to reach the READY minimum) re-sample one
    # that answered.
    spares = _spare_candidate_models(comparable, models)
    for _ in range(CHOOSER_NUM_CANDIDATES * 2):  # Limit total retries
        if candidate_index >= CHOOSER_NUM_CANDIDATES:
            break
        model = _next_retry_model(spares, produced, failed, candidate_index)
        if model is None:
            break
        await attempt(model, resample=id(model) in produced)

    # One save covers every candidate generated above; candidates are
    # created durably as they complete, only the counter waits.
    await task.asave()

    # Add a synthesized candidate combining the per-model drafts.
    await _maybe_add_synthesized_candidate(task, kind)


# What a released generation claim's timestamp is set to: older than any
# window, so the next claim wins at once.
_RELEASED_CLAIM = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def _generation_claim_key(task_type: str) -> str:
    return f"chooser_generation:{task_type}"


class _GenerationClaim:
    """One process's hold on generating ``task_type`` tasks (see
    _claim_generation). Used as ``async with claim:``, which releases it
    however the generation ends."""

    def __init__(self, task_type: str, token: Optional[datetime.datetime]):
        self.task_type = task_type
        # The time the claim row was set to, and so the owner's proof: a
        # renewal or release touches the row only while it still holds this.
        # None for a refill generating without a claim (see
        # _claim_for_refill): there is nothing to renew or release.
        self.token = token

    def _held(self, token: datetime.datetime) -> "QuerySet[ModelHealthAlertState]":
        return ModelHealthAlertState.objects.filter(
            key=_generation_claim_key(self.task_type), last_alert_sent=token
        )

    async def renew(self) -> bool:
        """Restart the claim's window. False when it lapsed and another
        process has taken it since, so this one must stop generating."""
        if self.token is None:
            return True
        now = timezone.now()
        try:
            renewed = await self._held(self.token).aupdate(last_alert_sent=now)
        except Exception as e:
            # A database that can't answer is no sign another process took
            # the claim, and the claim holds until its window ends anyway.
            logger.warning(
                f"Chooser: could not renew the {self.task_type} generation "
                f"claim; carrying on: {e}"
            )
            return True
        if renewed:
            self.token = now
        return bool(renewed)

    async def release(self) -> None:
        """Free the claim now rather than when it lapses. A claim that lapsed
        and was taken by another process is that process's: the row no
        longer holds our token, so this leaves it alone."""
        if self.token is None:
            return
        try:
            await self._held(self.token).aupdate(last_alert_sent=_RELEASED_CLAIM)
        except Exception as e:
            # Not raised: this runs on the way out of a generation, and the
            # claim lapses on its own, so a failed release only holds the
            # next generation of this type off until then.
            logger.warning(
                f"Chooser: could not release the {self.task_type} generation "
                f"claim; it lapses within {CHOOSER_GENERATION_CLAIM_SECONDS}s: {e}"
            )
        self.token = None

    async def __aenter__(self) -> "_GenerationClaim":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.release()


async def _claim_generation(task_type: str) -> Optional[_GenerationClaim]:
    """Claim the generation of ``task_type`` tasks across every process, or
    None while another process holds the claim.

    Every generation path takes this first: the refill actor's batches, page
    prefills and trigger_task_generation_sync. Each used to guard itself, the
    actor with a process-local lock and a prefill by looking for a recent
    QUEUED task. Neither saw the other, and two web workers could both look
    before either had created its task, so overlapping generations each paid
    for a round of outside model calls.

    The claim is ModelHealthAlertState.try_claim's one-winner pattern on the
    same table, under its own key: a conditional UPDATE that only moves a
    timestamp older than the window, then get_or_create for a row not there
    yet, each atomic on its own. try_claim itself is sync and hands back no
    token to release with. The claim lapses CHOOSER_GENERATION_CLAIM_SECONDS
    after it was taken or last renewed, so one left behind by a process that
    died mid-generation holds nothing off for longer than that.

    Raises on a database error: each caller decides whether to fail open.
    """
    key = _generation_claim_key(task_type)
    now = timezone.now()
    cutoff = now - datetime.timedelta(seconds=CHOOSER_GENERATION_CLAIM_SECONDS)
    if await ModelHealthAlertState.objects.filter(
        key=key, last_alert_sent__lte=cutoff
    ).aupdate(last_alert_sent=now):
        return _GenerationClaim(task_type, now)
    _, created = await ModelHealthAlertState.objects.aget_or_create(
        key=key, defaults={"last_alert_sent": now}
    )
    return _GenerationClaim(task_type, now) if created else None


async def _claim_for_refill(task_type: str) -> Optional[_GenerationClaim]:
    """The refill's claim on ``task_type``, or None while another process
    holds it.

    Fails open, unlike every other path: when the claim can't be checked the
    refill generates without one. The refill actor is the pool's main
    supplier, and one named actor whose loop awaits each tick, so it never
    overlaps itself; prefills fail closed on the same error, so while claims
    can't be checked the actor is the only process generating. Failing
    closed here would let the pool drain for as long as the claim table is
    unreachable, though the tasks table the refill just counted answers.
    """
    try:
        return await _claim_generation(task_type)
    except Exception as e:
        logger.warning(
            f"Chooser: could not check the {task_type} generation claim; "
            f"refilling without it: {e}"
        )
        return _GenerationClaim(task_type, None)


async def check_and_refill_task_pool() -> bool:
    """Generate a batch of tasks for every type whose pool needs one.

    Three triggers, checked in order (see ``_refill_reason``): the READY pool
    is short, fresh (unvoted) tasks are short, or an external backend the
    chooser would compare today has no fresh tasks comparing it. The third is
    what lets a newly configured provider into the pool: READY is monotonic
    (nothing moves a task out of it), so without it a pool bootstrapped before
    the provider existed never built a task for it.

    Each type's batch runs under that type's generation claim (see
    _claim_generation), so it never overlaps a batch or prefill of the same
    type in any process. A type another process is generating is skipped
    until the next tick, and is not a failed refill: it is being refilled.

    Returns False when types needed a batch and none of those batches
    produced a READY task, else True. Generation errors are caught per task
    and leave the task DISABLED, so a refill whose every model failed still
    returns normally; the refill actor needs this to tell that from a pool
    that is being refilled. One type failing while another refills is not a
    failed refill: it used to count as one, so the actor was reported
    unhealthy and replaced, which cannot fix a type whose backends are
    failing, and could kill it mid-batch, leaving its claim to lapse before
    anything else could generate that type.
    """
    needed = produced = 0
    for task_type in ["appeal", "chat"]:
        reason = await _refill_reason(task_type)
        if reason is None:
            continue
        claim = await _claim_for_refill(task_type)
        if claim is None:
            logger.info(
                f"Chooser {task_type} tasks need generating ({reason}), but "
                "another process is generating them; skipping this tick"
            )
            continue
        needed += 1
        logger.info(
            f"Chooser {task_type} tasks need generating ({reason}). "
            f"Generating {CHOOSER_GENERATION_BATCH_SIZE} tasks."
        )
        async with claim:
            ready = await _generate_batch_tasks(
                task_type, CHOOSER_GENERATION_BATCH_SIZE, claim
            )
        if ready:
            produced += 1
        else:
            logger.warning(
                f"Chooser {task_type} refill ({reason}) produced no usable task"
            )
    return needed == 0 or produced > 0


async def _refill_reason(task_type: str) -> Optional[str]:
    """Why ``task_type`` needs a batch, or None when the pool is healthy."""
    ready_count = await _count_ready_tasks(task_type)
    if ready_count < CHOOSER_MIN_READY_TASKS:
        return f"ready={ready_count}/{CHOOSER_MIN_READY_TASKS}"
    unscored_count = await _count_unscored_tasks(task_type)
    if unscored_count < CHOOSER_MIN_UNSCORED_TASKS:
        return f"unscored={unscored_count}/{CHOOSER_MIN_UNSCORED_TASKS}"
    # Coverage refills are capped: a backend that never returns a usable
    # candidate would otherwise justify a batch every tick, forever.
    if unscored_count >= CHOOSER_MAX_UNSCORED_TASKS:
        return None
    uncovered = await _external_backends_without_fresh_tasks(task_type)
    if uncovered:
        return f"no fresh tasks compare {sorted(uncovered)}"
    return None


def _comparable_backends(task_type: str) -> List:
    """The backends candidate generation draws from for ``task_type``, and
    so the ones the coverage refill checks: the router's list, plus any
    CHOOSER_FORCE_MODEL backend it left out (one still being tried out may
    not have made the router's cut yet). (``ml_router`` is a proxy, so its
    methods read as Any to mypy; hence the casts.)"""
    if task_type == "appeal":
        backends = cast(List, ml_router.generate_text_backends(use_external=True))
    else:
        # explore=False: every roster model that can be asked, so each gets
        # compared, and the same list each time: which models need fresh
        # tasks must not depend on a chat turn's random draw.
        backends = cast(
            List, ml_router.get_chat_backends(use_external=True, explore=False)
        )
    listed = {_model_display_name(m) for m in backends}
    return backends + [
        m
        for m in _forced_arena_models(task_type)
        if _model_display_name(m) not in listed
    ]


def _fresh_tasks(task_type: str) -> "QuerySet[ChooserTask]":
    """READY synthetic tasks of ``task_type`` at the current case version
    (CASE_VERSIONS) with no vote yet: the fresh ("unscored") tasks, the ones
    still able to gather new preference data. An older-version task stays
    votable but is served after the current ones, so an unvoted one would
    otherwise hold the refill off while voters see the new kind run out."""
    return (
        ChooserTask.objects.filter(
            task_type=task_type,
            status="READY",
            source="synthetic",
            case_version__gte=_case_version(task_type),
        )
        .annotate(vote_count=Count("votes"))
        .filter(vote_count=0)
    )


async def _fresh_task_coverage(task_type: str) -> dict:
    """How many fresh tasks of ``task_type`` hold a candidate from each
    backend, keyed by the persisted model name."""
    fresh_ids = _fresh_tasks(task_type).values("id")
    coverage: dict = {}
    # .order_by() clears the Meta ordering, which would otherwise leak into
    # the GROUP BY.
    async for row in (
        ChooserCandidate.objects.filter(task_id__in=fresh_ids, is_active=True)
        .order_by()
        .values("model_name")
        .annotate(tasks=Count("task", distinct=True))
    ):
        coverage[row["model_name"]] = row["tasks"]
    return coverage


async def _external_backends_without_fresh_tasks(task_type: str) -> set:
    """External backends the chooser would compare today, and any
    CHOOSER_FORCE_MODEL backend, that fewer than ``CHOOSER_MIN_UNSCORED_TASKS``
    fresh tasks actually compare.

    Externals only otherwise: the internal backends fill their candidate
    slots on every task, while the externals rotate through theirs, and
    comparing providers is what the chooser is for. A forced backend is
    checked whatever its kind: the pool it was just forced into has no fresh
    task with it yet, and getting it votes is why it was forced.
    """
    try:
        backends = _comparable_backends(task_type)
        forced = _forced_arena_models(task_type)
    except Exception as e:
        logger.warning(f"Chooser: could not list backends for {task_type}: {e}")
        return set()
    # Named the way candidates are stored, or the lookup below could miss.
    external = {canonical_model_name(m) for m in forced}
    # Forced models that take every slot leave none for the rest, so only
    # theirs can gain coverage; counting the others would build a batch every
    # tick (up to the unscored ceiling) that compares none of them.
    if len(forced) < CHOOSER_NUM_CANDIDATES:
        external |= {
            canonical_model_name(m) for m in backends if getattr(m, "external", False)
        }
    if not external:
        return set()
    coverage = await _fresh_task_coverage(task_type)
    return {
        name for name in external if coverage.get(name, 0) < CHOOSER_MIN_UNSCORED_TASKS
    }


async def _count_ready_tasks(task_type: str) -> int:
    """Count the number of READY tasks for a given type."""

    return cast(
        int,
        await database_sync_to_async(
            ChooserTask.objects.filter(task_type=task_type, status="READY").count
        )(),
    )


async def _count_unscored_tasks(task_type: str) -> int:
    """Count READY synthetic tasks of a type, at its current case version,
    that have not yet received a vote (see _fresh_tasks).

    These are the tasks still able to gather fresh preference data; once a
    task has any vote it is "scored" and contributes diminishing value, so we
    track unscored tasks separately from the overall READY pool.
    """

    return await _fresh_tasks(task_type).acount()


async def _generate_batch_tasks(
    task_type: str, batch_size: int, claim: Optional[_GenerationClaim] = None
) -> int:
    """Generate a batch of chooser tasks with candidates; returns how many
    came out READY.

    ``claim`` is renewed before each task after the first: a batch is a round
    of model calls per task and can outlast one window, and a claim that
    lapsed mid-batch would let another process generate this type alongside
    it. When another process has taken the claim anyway, the batch stops and
    leaves the type to it.
    """
    ready = 0
    for index in range(batch_size):
        if index and claim is not None and not await claim.renew():
            logger.warning(
                f"Chooser {task_type} generation claim lapsed and another "
                f"process took it; stopping the batch after {index} tasks"
            )
            break
        try:
            if await _generate_single_task(task_type):
                ready += 1
        except Exception as e:
            logger.opt(exception=True).warning(
                f"Error generating chooser task of type {task_type}: {e}"
            )
    return ready


# Every chooser generation path (the refill actor, the prefill thread, the
# REST next-task fallback) comes through here: synthetic data, no person.
@llm_usage.system_entry("chooser")
async def _generate_single_task(task_type: str) -> bool:
    """
    Generate a single ChooserTask with candidates; returns whether it came
    out READY.

    For appeals: generates multiple synthetic appeal letters for a sample context
    For chat: generates multiple synthetic chat responses for a sample prompt
    """

    # Create the task in QUEUED state
    task = await database_sync_to_async(ChooserTask.objects.create)(
        task_type=task_type,
        status="QUEUED",
        source="synthetic",
        case_version=_case_version(task_type),
        num_candidates_expected=CHOOSER_NUM_CANDIDATES,
        num_candidates_generated=0,
    )

    try:
        if task_type == "appeal":
            await _generate_appeal_candidates(task)
        else:
            await _generate_chat_candidates(task)

        # Mark task as READY if we generated enough candidates. The counter
        # was persisted by the generator's end-of-run save (not once per
        # candidate); this save records the final status.
        if task.num_candidates_generated >= _MIN_READY_CANDIDATES:
            task.status = "READY"
            await database_sync_to_async(task.save)()
            logger.info(
                f"ChooserTask {task.id} is now READY with {task.num_candidates_generated} candidates"
            )
            return True
        else:
            task.status = "DISABLED"
            await database_sync_to_async(task.save)()
            logger.warning(
                f"ChooserTask {task.id} disabled - insufficient candidates generated"
            )

    except Exception as e:
        logger.opt(exception=True).error(
            f"Error generating candidates for task {task.id}: {e}"
        )
        task.status = "DISABLED"
        await database_sync_to_async(task.save)()
    return False


async def _generate_appeal_candidates(task: ChooserTask):
    """Generate appeal letter candidates for a task using ONLY synthetic data from ML.

    The case is built the way a real appeal's inputs are: a scenario (the
    pair, the insurer, the denial reason, the case facts), the PubMed
    evidence for the pair with a summary of each article (_evidence_for),
    and the patient's answers to the app's own questions (_interview). Every
    draft is given all of it, and the voter is shown all of it.
    """

    # Use ML to generate a synthetic denial scenario. Synthetic data only, so
    # external backends are allowed (and wanted — see module docstring).
    generation_models = ml_router.generate_text_backends(use_external=True)
    if not generation_models:
        logger.warning("No models available for generating synthetic denial scenarios")
        # Cannot proceed without models - mark task as disabled
        task.status = "DISABLED"
        await database_sync_to_async(task.save)()
        return

    # The scenario writers must follow instructions; the cheapest internal
    # backend (fhi-legacy) does not. See _scenario_writers.
    writers = _scenario_writers(generation_models)
    try:
        # A real pair from the rankings when there is one (see
        # _pick_seed_pair); the writer invents one otherwise.
        seed = await _pick_seed_pair()
        scenario_prompt = _scenario_prompt(seed)
        scenario_system_prompt = (
            "You are a system that generates realistic but fictional health insurance scenarios. "
            "These are used to improve model selection for a health insurance appeal assistance service. "
            "Generate varied, realistic scenarios using real medical terminology."
        )

        context = None
        for writer in writers:
            # A backend that did not answer returns None rather than raising;
            # the parser reads it as "no fields" and the next writer is asked.
            scenario_response = await _ask_writer(
                writer, scenario_system_prompt, scenario_prompt
            )
            fields = _labeled_fields(scenario_response or "")
            written = {
                "procedure": fields.get("procedure"),
                "diagnosis": fields.get("diagnosis"),
                "insurance_company": fields.get("insurance_company"),
                "denial_text_preview": fields.get("denial_reason"),
            }
            if seed is not None:
                # The seed is the case, whatever the writer echoed back.
                written["procedure"] = seed.treatment
                written["diagnosis"] = seed.condition
            missing = [k for k, v in written.items() if not v]
            if not missing:
                context = written
                # Facts are wanted, not required: a scenario without them
                # is the old kind of task, still worth a vote.
                facts = _facts_from_response(scenario_response or "")
                if facts:
                    context["facts"] = facts
                else:
                    logger.info(
                        f"ChooserTask {task.id}: scenario from "
                        f"{_model_display_name(writer)} gave no case facts"
                    )
                break
            preview = (scenario_response or "")[:200]
            logger.warning(
                f"ChooserTask {task.id}: scenario from "
                f"{_model_display_name(writer)} is missing {missing}. "
                f"Response started: {preview!r}"
            )
        if context is None:
            # No placeholders: a task about "Medical procedure" for "Medical
            # condition" measures nothing, and it used to go READY and reach
            # voters. Disable it; the next batch tries again.
            logger.warning(
                f"ChooserTask {task.id}: no complete scenario from "
                f"{len(writers)} writer(s); disabling the task"
            )
            task.status = "DISABLED"
            await task.asave()
            return

        context["seeded"] = seed is not None
        # The evidence the drafts may cite, and the voter checks them
        # against. Best effort: a scenario with none still measures the
        # models, and the voter is told the list is empty.
        try:
            context["evidence"] = await _evidence_for(
                context["procedure"] or "", context["diagnosis"] or ""
            )
        except Exception as e:
            logger.opt(exception=True).warning(
                f"ChooserTask {task.id}: could not attach evidence: {e}"
            )
            context["evidence"] = []
        # What the patient said when the app asked its questions. Best
        # effort too: a case without answers is still worth a vote.
        qa_pairs = await _interview(task, context, writers)
        if qa_pairs:
            context["qa_pairs"] = qa_pairs
        task.context_json = context
        await database_sync_to_async(task.save)()

    except Exception as e:
        logger.opt(exception=True).warning(
            f"Error generating synthetic denial scenario: {e}"
        )
        # Cannot proceed without a valid scenario - mark task as disabled
        task.status = "DISABLED"
        await database_sync_to_async(task.save)()
        return

    # Generate candidates using different models. Synthetic context only, so
    # external backends (Anthropic/Azure/DeepInfra/...) participate — the chooser
    # is how they get compared and how they show up in usage reporting.
    prompt = _build_appeal_prompt(task.context_json)
    system_prompt = (
        "You are an expert at writing health insurance appeal letters. "
        "Write a professional, compelling appeal letter based on the given context. "
        "Stay inside the facts and evidence you are given: a letter that makes "
        "things up loses the appeal and this comparison."
    )

    async def ask(model, resample: bool) -> Optional[str]:
        # A second draft from the same model is asked to differ from its first.
        creative = (
            " Be creative and write a unique response different from previous attempts."
            if resample
            else ""
        )
        return cast(
            Optional[str],
            await model._infer_no_context(
                system_prompts=[system_prompt + creative], prompt=prompt
            ),
        )

    await _fill_candidates(
        task, "appeal", "appeal_letter", 100, ask, {"source": "synthetic"}
    )


async def _generate_chat_candidates(task: ChooserTask):
    """Generate chat response candidates for a task using ONLY synthetic data from ML."""

    # Generate synthetic chat prompt using ML
    # Use ML to generate a synthetic conversation with some back-and-forth.
    # Synthetic data only, so external backends are allowed (see module docstring).
    generation_models = ml_router.generate_text_backends(use_external=True)
    if not generation_models:
        logger.warning("No models available for generating synthetic chat prompts")
        # Cannot proceed without models - mark task as disabled
        task.status = "DISABLED"
        await database_sync_to_async(task.save)()
        return

    # The conversation writers must follow instructions; the cheapest
    # internal backend (fhi-legacy) does not. See _scenario_writers.
    writers = _scenario_writers(generation_models)
    try:
        # Generate a multi-turn conversation scenario
        conversation_prompt = (
            "We are building a health insurance assistance chatbot. "
            "To improve our model selection techniques, we need realistic conversation scenarios.\n\n"
            "Please generate a short realistic conversation between a user and an assistant about ONE of these topics:\n"
            "- Health insurance claim denials and appeals\n"
            "- Prior authorization requests and denials\n"
            "- Medicare eligibility, coverage, or appeals\n"
            "- Medicaid eligibility, enrollment, or coverage issues\n\n"
            "The conversation should have 2-4 exchanges (user message, then assistant response, then user follow-up, etc.).\n"
            "End with a user question that the assistant has NOT yet answered.\n\n"
            "Format EXACTLY like this (use these exact labels):\n"
            "USER: [first user message]\n"
            "ASSISTANT: [assistant response]\n"
            "USER: [follow-up question - this is the one we want candidate responses for]\n\n"
            "Make the conversation natural and include specific details (procedures, conditions, timeframes, etc.)."
        )

        conversation_system_prompt = (
            "You are a system that generates realistic chat conversations about health insurance, "
            "Medicare, Medicaid, and prior authorizations for improving model selection. "
            "Generate varied, natural-sounding conversations."
        )

        # Every writer is asked for a conversation before any is asked for a
        # single question: a conversation from the next writer beats a
        # question from one that could not write one.
        history: list = []
        final_user_prompt: Optional[str] = None
        for writer in writers:
            # A backend that did not answer returns None rather than raising.
            conversation_response = await _ask_writer(
                writer, conversation_system_prompt, conversation_prompt
            )
            history, final_user_prompt = _parse_conversation(
                conversation_response or ""
            )
            if _usable_question(final_user_prompt):
                break
            logger.warning(
                f"ChooserTask {task.id}: could not parse a conversation from "
                f"{_model_display_name(writer)}"
            )
            final_user_prompt = None

        if final_user_prompt is None:
            # Fallback: try generating a simple single question
            history = []
            simple_prompt = (
                "Generate a realistic 1-2 sentence question someone might ask about one of:\n"
                "- Appealing a health insurance denial\n"
                "- Prior authorization problems\n"
                "- Medicare or Medicaid eligibility\n"
                "Just the question, nothing else."
            )
            for writer in writers:
                question = await _ask_for_single_question(writer, simple_prompt)
                if question:
                    question = question.strip().strip('"').strip("'").strip()
                if _usable_question(question):
                    final_user_prompt = question
                    break
            if final_user_prompt is None:
                logger.warning(
                    f"ChooserTask {task.id}: no usable conversation or question "
                    f"from {len(writers)} writer(s); disabling the task"
                )
                task.status = "DISABLED"
                await task.asave()
                return

        task.context_json = {
            "prompt": final_user_prompt,
            "history": history,
        }
        await database_sync_to_async(task.save)()

    except Exception as e:
        logger.opt(exception=True).warning(
            f"Error generating synthetic chat prompt: {e}"
        )
        # Cannot proceed without a valid prompt - mark task as disabled
        task.status = "DISABLED"
        await database_sync_to_async(task.save)()
        return

    # Generate candidates using different models. Synthetic context only, so
    # external backends participate (they are what the chooser compares).
    chat_history = task.context_json.get("history", [])
    user_prompt = task.context_json.get("prompt", "")

    async def ask(model, resample: bool) -> Optional[str]:
        # Asked the same way on a re-sample. NOTE: the parameter is
        # current_message_for_llm — passing current_message= raised TypeError
        # for EVERY backend and silently produced zero chat candidates.
        # Labelled "other" in the call metrics, like the appeal synthesis:
        # this is not a user's chat turn, though generate_chat_response
        # labels its calls "chat" (whose budget still pays for, and caps,
        # them).
        with ml_call_purpose_override("other"):
            response, _ = await model.generate_chat_response(
                current_message_for_llm=user_prompt,
                previous_context_summary=None,
                history=chat_history,
                is_professional=True,
                is_logged_in=True,
            )
        return cast(Optional[str], response)

    await _fill_candidates(
        task,
        "chat",
        "chat_response",
        50,
        ask,
        {"source": "synthetic", "has_history": len(chat_history) > 0},
    )


def _build_appeal_prompt(context: dict) -> str:
    """Build a prompt for generating appeal letters."""
    parts = ["Write an appeal letter for the following health insurance denial:"]

    if context.get("procedure"):
        parts.append(f"Procedure: {context['procedure']}")
    if context.get("diagnosis"):
        parts.append(f"Diagnosis: {context['diagnosis']}")
    if context.get("insurance_company"):
        parts.append(f"Insurance Company: {context['insurance_company']}")
    if context.get("denial_text_preview"):
        parts.append(f"Denial Summary: {context['denial_text_preview']}")
    facts = [str(f) for f in (context.get("facts") or []) if f]
    if facts:
        parts.append("\nCase facts (the only facts known about this patient):")
        parts.extend(f"- {fact}" for fact in facts)
    qa_pairs = [
        p
        for p in (context.get("qa_pairs") or [])
        if isinstance(p, dict) and p.get("question") and p.get("answer")
    ]
    if qa_pairs:
        parts.append(
            "\nThe patient's answers to the appeal assistant's questions "
            "(as reliable as the case facts):"
        )
        for pair in qa_pairs:
            parts.append(f"Q: {pair['question']}")
            parts.append(f"A: {pair['answer']}")
    evidence = [e for e in (context.get("evidence") or []) if e.get("title")]
    if evidence:
        parts.append("\nEvidence on file (the only studies you may cite):")
        for e in evidence:
            parts.append(
                f"- {e['title']} (PMID {e['pmid']})"
                if e.get("pmid")
                else f"- {e['title']}"
            )
            if e.get("summary"):
                parts.append(f"  Summary: {e['summary']}")

    parts.append(
        "\nPlease write a professional appeal letter that argues why this treatment "
        "should be covered. Include relevant medical reasoning and cite any applicable "
        "guidelines or regulations."
    )
    parts.append(
        "Use only the case facts"
        + (" and the patient's answers" if qa_pairs else "")
        + " above; where a detail is not given, leave it out or "
        "say the records will show it, rather than inventing it. Cite only the "
        "evidence on file, by title or PMID"
        + (
            ", and claim no more for a study than its summary says"
            if any(e.get("summary") for e in evidence)
            else ""
        )
        + (
            "; if nothing on file fits, cite nothing"
            if evidence
            else " (there is none on file, so cite no studies)"
        )
        + ". Never invent a study, a statute, a test result, a date or a clinical history."
    )

    return "\n".join(parts)


async def _maybe_add_synthesized_candidate(task: ChooserTask, kind: str) -> None:
    """Add one synthesized candidate combining the task's existing candidates.

    Synthesis needs >=2 base candidates to be meaningful (with a single input
    models tend to echo it verbatim), mirroring the real appeal flow. The
    synthesized candidate is stored with ``synthesized=True`` and
    ``model_name="synthesized"`` so it is tracked and bucketed distinctly when
    measuring synthesis against single models.
    """
    if not CHOOSER_INCLUDE_SYNTHESIS:
        return

    # Best-effort: synthesis runs AFTER the base candidates are already
    # persisted, so nothing here may propagate. Otherwise a transient error
    # (e.g. a DB read/count hiccup) would bubble into _generate_single_task's
    # except and flip an otherwise-valid task to DISABLED, orphaning its good
    # base candidates. Contain everything and simply skip on error.
    try:
        existing: List[ChooserCandidate] = [
            candidate
            async for candidate in ChooserCandidate.objects.filter(
                task=task, is_active=True, synthesized=False
            ).order_by("candidate_index")
        ]
        # Dedupe identical drafts so two copies of the same text don't count as
        # two distinct inputs (synthesizing from identical drafts is pointless).
        contents = list(
            dict.fromkeys(
                c.content.strip() for c in existing if c.content and c.content.strip()
            )
        )
        if len(contents) < 2:
            # Not enough distinct drafts to synthesize from.
            return

        context = task.context_json or {}
        if kind == "appeal_letter":
            synthesized_text = await _synthesize_appeal_candidate(context, contents)
        else:
            synthesized_text = await _synthesize_chat_candidate(context, contents)

        if not synthesized_text:
            return
        normalized = synthesized_text.strip()
        # Hold the synthesized candidate to the same minimum-length bar the base
        # candidates must clear (>100 chars for appeals, >50 for chat).
        min_length = 100 if kind == "appeal_letter" else 50
        if len(normalized) <= min_length:
            logger.info(
                f"Chooser synthesis for task {task.id} returned too-short output; skipping"
            )
            return
        # Skip a synthesized output that is just a verbatim copy of a draft.
        if normalized in set(contents):
            logger.info(
                f"Chooser synthesis for task {task.id} returned a verbatim draft; skipping"
            )
            return

        # Next free index: base candidates occupy 0..N-1 contiguously.
        next_index = await database_sync_to_async(
            ChooserCandidate.objects.filter(task=task).count
        )()
        await database_sync_to_async(ChooserCandidate.objects.create)(
            task=task,
            candidate_index=next_index,
            kind=kind,
            model_name=SYNTHESIZED_MODEL_NAME,
            synthesized=True,
            content=normalized,
            metadata={"source": "synthetic", "synthesized": True},
        )
        task.num_candidates_generated = next_index + 1
        # The synthesized draft is one more than the base slots: raise the
        # expectation with it, or every synthesized task reads as one over.
        task.num_candidates_expected = max(task.num_candidates_expected, next_index + 1)
        await database_sync_to_async(task.save)()
        logger.info(
            f"Added synthesized {kind} candidate to task {task.id} "
            f"from {len(contents)} drafts"
        )
    except Exception as e:
        logger.opt(exception=True).warning(
            f"Failed to add synthesized candidate to task {task.id}: {e}"
        )


async def _synthesize_appeal_candidate(
    context: dict, contents: List[str]
) -> Optional[str]:
    """Synthesize multiple appeal drafts into one, reusing AppealGenerator."""
    from fighthealthinsurance.generate_appeal import AppealGenerator

    try:
        generator = AppealGenerator()
        return await generator.synthesize_appeals(
            appeal_texts=contents,
            denial_text=context.get("denial_text_preview"),
            procedure=context.get("procedure"),
            diagnosis=context.get("diagnosis"),
            # Synthetic chooser traffic, not a user's appeal: kept out of the
            # appeal series of the fhi_ml_call* metrics.
            purpose="other",
        )
    except Exception as e:
        logger.opt(exception=True).warning(
            f"Error synthesizing appeal chooser candidate: {e}"
        )
        return None


async def _synthesize_chat_candidate(
    context: dict, contents: List[str]
) -> Optional[str]:
    """Synthesize multiple chat responses into one using the best internal model."""
    best_model = ml_router.best_internal_model()
    if best_model is None:
        logger.warning("No internal model available for chat synthesis")
        return None

    history = context.get("history", []) or []
    user_prompt = context.get("prompt", "") or ""

    convo_lines = []
    for msg in history:
        role = str(msg.get("role", "user")).capitalize()
        convo_lines.append(f"{role}: {msg.get('content', '')}")
    convo_lines.append(f"User: {user_prompt}")
    conversation = "\n".join(convo_lines)

    numbered = "\n\n".join(
        f"--- RESPONSE {i + 1} ---\n{text}" for i, text in enumerate(contents)
    )
    prompt = (
        f"CONVERSATION SO FAR:\n{conversation}\n\n"
        f"Below are {len(contents)} candidate assistant responses to the final "
        "user message. Synthesize them into the single best response, combining "
        "the most accurate, helpful, and clear elements from each. Reply with "
        "only the synthesized response.\n\n"
        f"{numbered}"
    )
    try:
        result = await best_model._infer_no_context(
            system_prompts=[
                "You combine multiple assistant responses into one best response "
                "for a health insurance assistance chatbot. Keep it accurate, "
                "helpful, and concise."
            ],
            prompt=prompt,
            temperature=0.3,
        )
    except Exception as e:
        logger.opt(exception=True).warning(
            f"Error synthesizing chat chooser candidate: {e}"
        )
        return None
    if result and len(result.strip()) > 50:
        return str(result).strip()
    return None


async def prefill_if_needed(min_ready: int = 1, exhausted: Optional[str] = None):
    """
    Check if there are enough READY tasks and trigger generation if not.
    This is a lightweight check intended to be called on page load.

    Args:
        min_ready: Minimum number of ready tasks required for each type.
        exhausted: A task type some session has already answered every READY
            task of. The pool is not short, so the count alone would never
            generate one, and that session would see "No tasks available"
            until the refill actor next ran.

    The prefill throttle lives in the cache, which is per process
    (LocMemCache), so every web worker could start a generation while the
    pool was empty, each paying for a round of outside model calls. A
    prefill generates only under the type's generation claim (see
    _claim_generation), which one process holds at a time, the refill
    actor included; the claim is released when the task is done.
    """

    for task_type in ["appeal", "chat"]:
        ready_count = await _count_ready_tasks(task_type)
        if ready_count < min_ready:
            reason = f"below minimum ({ready_count} < {min_ready})"
        elif task_type == exhausted:
            reason = f"all {ready_count} used up by a session"
        else:
            continue
        try:
            claim = await _claim_generation(task_type)
        except Exception as e:
            # Fail closed: the refill actor still supplies the pool, while a
            # prefill that can't tell whether another process is generating
            # could pay for the same round twice. One warning per pass: the
            # next type's claim would fail the same way.
            logger.warning(
                f"Chooser prefill skipped: could not check the {task_type} "
                f"generation claim: {e}"
            )
            return
        if claim is None:
            logger.debug(
                f"Chooser {task_type} tasks {reason}, but another process is "
                "generating them; not starting another"
            )
            continue
        logger.info(
            f"Chooser {task_type} tasks {reason}. Triggering generation of 1 task."
        )
        # Fire and forget - don't wait for completion
        work = _generate_under(claim)
        try:
            await fire_and_forget_in_new_threadpool(work)
        except Exception:
            # No thread runs ``work``, so nothing else would release the
            # claim before it lapsed.
            work.close()
            await claim.release()
            raise


async def _generate_under(claim: _GenerationClaim) -> bool:
    """One task of the claim's type, then the claim released, however the
    generation ended."""
    async with claim:
        return await _generate_single_task(claim.task_type)


def trigger_prefill_async(exhausted: Optional[str] = None) -> bool:
    """
    Trigger async pre-fill of chooser tasks.
    Safe to call from sync context - fires and forgets in a background thread.

    Throttled to one prefill per process per CHOOSER_PREFILL_THROTTLE_SECONDS:
    every chooser page load and every empty next-task fetch asks, and each
    prefill is a full task generation. A prefill for a type a session has
    used up (``exhausted``, see prefill_if_needed) has its own window, so a
    page load just before cannot hold it off. Across processes, a prefill
    skips a type that is already being generated (see _claim_generation).
    Returns whether a prefill was started.
    """
    from django.core.cache import cache

    throttle_key = "chooser_prefill_recently_triggered"
    if exhausted:
        throttle_key += f":{exhausted}"
    try:
        if not cache.add(throttle_key, 1, CHOOSER_PREFILL_THROTTLE_SECONDS):
            logger.debug("Chooser prefill requested again within the throttle window")
            return False
    except Exception as e:
        # A cache that cannot answer must not stop the prefill.
        logger.debug(f"Chooser prefill throttle unavailable: {e}")

    def run_prefill():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(prefill_if_needed(min_ready=1, exhausted=exhausted))
        except Exception as e:
            logger.opt(exception=True).warning(f"Error in prefill task: {e}")
        finally:
            loop.close()

    thread = threading.Thread(target=run_prefill)
    thread.daemon = True
    thread.start()
    return True


# Utility function to manually trigger task generation (for testing/admin purposes)
def trigger_task_generation_sync(task_type: str, count: int = 1):
    """
    Synchronously trigger generation of chooser tasks.
    Useful for testing or admin commands. Generates nothing while another
    process holds the type's generation claim (see _claim_generation).
    """
    from asgiref.sync import async_to_sync

    async def run_generation():
        claim = await _claim_generation(task_type)
        if claim is None:
            logger.warning(
                f"Chooser {task_type} tasks are being generated by another "
                "process; not generating more"
            )
            return
        async with claim:
            await _generate_batch_tasks(task_type, count, claim)

    async_to_sync(run_generation)()

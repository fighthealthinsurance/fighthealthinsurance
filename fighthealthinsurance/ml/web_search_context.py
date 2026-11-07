"""Web search results as extra citations for an appeal, from OpenRouter's
web search (``OpenRouterWebSearch`` in ml_models.py).

Why: the citation backends (Perplexity) write their citations themselves,
and the other sources (CMS, ECRI, the microsites) cover what they cover.
A web search finds current coverage policies, guidelines and evidence for
the treatment, each with a real URL.

How it stays out of the way: it runs beside the citation backends, inside
the citation step's own time box (``ml_citations_helper``), and anything
that goes wrong (no key, the model down or out of credit, a timeout, an
odd response) gives no web citations rather than an error. It never makes
the citation step longer than it already was allowed to take.

The guardrail: a web page is not quoted because a search found it. Each
result is asked the same three questions a PubMed article is
(ml/research_judging.py: is it about this treatment for this condition,
does it support it, does it argue against it), by Jev when research
judging is on and otherwise by our own best general-purpose model. A
result judged off topic or against the treatment is dropped, and so is one
nobody could judge (no excerpt to read, the judges down or out of time):
unlike a PubMed article, an unjudged web page does not keep its place.
What is kept goes in order of how strongly it supports the treatment. With
at least two plainly on topic, the ones only probably on topic are left out
too; with fewer, they are kept and labelled probably off topic, for the
letter's model to weigh (research_judging.focus).

Data protection: the search is sent the denial's procedure and diagnosis
only, never the letter, the history or anything about the person: the same
terms a PubMed search sends, so like the PubMed search it runs whether or
not the person allowed outside models. The judges are sent the page's
public title and excerpt and the same procedure and diagnosis, as research
judging is, and as there Jev (an outside service) is asked only with the
person's external-models consent (``Denial.use_external``, read again from
the database first); without it our own model judges. Nothing here logs the
text.
"""

import asyncio
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from loguru import logger

from fighthealthinsurance.ml import llm_usage, research_judging
from fighthealthinsurance.ml.ml_models import WebCitation
from fighthealthinsurance.ml.ml_router import ml_router
from fighthealthinsurance.models import Denial

# The most a web search and its judging take, at most; the caller passes
# less when its own time box is shorter.
DEFAULT_BUDGET_SECONDS = 25.0
# The share of the budget the search gets; the judging gets the rest.
SEARCH_SHARE = 0.6
# Results judged per search, judged this many at once (the same backends
# write real appeals), and kept at most.
MAX_JUDGED = 5
JUDGE_CONCURRENCY = 3
MAX_KEPT = 3
# How much of a page's excerpt goes beside its citation in the letter's
# context.
CITED_EXCERPT_CHARS = 300
# Below this much time left, judging is not started.
_MIN_JUDGE_SECONDS = 2.0
# Of a result's judging time, the most Jev is waited for when our own model
# could take over (as in the Appeal Arena's _judge_evidence): Jev normally
# answers within seconds, and our own model, which writes its answer,
# needs the rest.
JEV_SHARE_OF_JUDGING = 0.5

# Counts, for tests and the shell: searches, results found, kept, dropped
# (judged off topic or against), set aside (probably off topic, beside
# enough on topic: research_judging.focus) and unjudged (dropped too).
outcomes: Dict[str, int] = {
    "searched": 0,
    "skipped": 0,
    "failed": 0,
    "found": 0,
    "kept": 0,
    "dropped": 0,
    "set_aside": 0,
    "unjudged": 0,
}


def _count(outcome: str, n: int = 1) -> None:
    outcomes[outcome] = outcomes.get(outcome, 0) + n


def _term(value: Optional[str]) -> str:
    return " ".join(str(value or "").split())[: research_judging.TERM_CHAR_CAP]


def build_question(procedure: Optional[str], diagnosis: Optional[str]) -> Optional[str]:
    """What the web search is asked, from the procedure and diagnosis
    alone, or None without a procedure (there would be nothing to judge the
    results against)."""
    treatment = _term(procedure)
    if not treatment:
        return None
    condition = _term(diagnosis)
    for_condition = f" for {condition}" if condition else ""
    return (
        f"Find clinical guidelines, insurer coverage policies and published "
        f"evidence on whether {treatment}{for_condition} is medically "
        "necessary or the standard of care."
    )


async def _consent_allows(denial: Denial) -> bool:
    """The person allowed outside models, on the row in memory and on the
    row as it is now. A missing row or a failed read is a no. Decides
    whether Jev may judge the results, not whether the search runs."""
    if getattr(denial, "use_external", False) is not True:
        return False
    denial_id = getattr(denial, "denial_id", None)
    if denial_id is None:
        return False
    try:
        row = (
            await Denial.objects.filter(denial_id=denial_id)
            .values("use_external")
            .afirst()
        )
    except Exception as e:
        logger.debug(
            f"Could not read consent for denial {denial_id} before judging "
            f"web results ({type(e).__name__}); not asking Jev"
        )
        return False
    return bool(row and row["use_external"])


async def judge_result(
    result: WebCitation,
    procedure: Optional[str],
    diagnosis: Optional[str],
    *,
    judge_model: Any = None,
    may_ask_jev: bool = False,
    timeout_seconds: float = research_judging.OWN_MODEL_TIMEOUT_SECONDS,
) -> Optional[research_judging.Judgment]:
    """Jev's judgment of one result when ``may_ask_jev`` (the person's
    external-models consent, the caller's to check) and research judging
    is on and may send now, else (or when Jev gives none) ``judge_model``'s,
    else None, all within ``timeout_seconds``: with ``judge_model`` there to
    take over, Jev gets JEV_SHARE_OF_JUDGING of it and our own model the
    rest. Never raises (cancellation aside)."""
    started = time.monotonic()
    title, excerpt = result.page_title, result.excerpt
    if (
        may_ask_jev
        and research_judging.enabled()
        and research_judging.skip_reason() is None
    ):
        jev_seconds = (
            timeout_seconds * JEV_SHARE_OF_JUDGING
            if judge_model is not None
            else timeout_seconds
        )
        judgment = await research_judging.judge_article(
            title, excerpt, procedure, diagnosis, timeout_seconds=jev_seconds
        )
        if judgment is not None:
            return judgment
    if judge_model is None:
        return None
    left = timeout_seconds - (time.monotonic() - started)
    if left <= 0:
        return None
    return await research_judging.judge_article_with(
        judge_model,
        title,
        excerpt,
        procedure,
        diagnosis,
        timeout_seconds=left,
    )


async def _judge_all(
    results: List[WebCitation],
    procedure: Optional[str],
    diagnosis: Optional[str],
    seconds: float,
    may_ask_jev: bool,
) -> Dict[str, research_judging.Judgment]:
    """Judgments by URL, for the results judged within ``seconds``; the
    rest are cancelled and stay unjudged."""
    judge_model = ml_router.best_internal_model()
    gate = asyncio.Semaphore(JUDGE_CONCURRENCY)
    deadline = time.monotonic() + seconds

    async def one(result: WebCitation) -> Optional[research_judging.Judgment]:
        async with gate:
            # What is left once a slot frees, not the whole window, so the
            # split between Jev and our own model fits in it.
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            return await judge_result(
                result,
                procedure,
                diagnosis,
                judge_model=judge_model,
                may_ask_jev=may_ask_jev,
                timeout_seconds=left,
            )

    tasks = {asyncio.ensure_future(one(r)): r for r in results}
    try:
        done, _ = await asyncio.wait(tasks, timeout=seconds)
    finally:
        # Also when this call is cancelled (its time box ran out, or the
        # citation run was): asyncio.wait leaves its tasks running, and a
        # judge left behind keeps calling the backends that write appeals.
        unfinished = [task for task in tasks if not task.done()]
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)
    judgments: Dict[str, research_judging.Judgment] = {}
    for task in done:
        if task.cancelled() or task.exception() is not None:
            continue
        judgment = task.result()
        if judgment is not None:
            judgments[tasks[task].url] = judgment
    return judgments


# Every citation ``cited`` writes carries this right after its title and URL,
# so a stored citation list (Denial.ml_citation_context, plain strings) can
# still tell the web search's citations from the rest: when a prompt is too
# long, they go before the PubMed references do (generate_appeal's
# proactive shed).
WEB_CITATION_MARKER = " (web page"
_WEB_CITATION_RE = re.compile(re.escape(WEB_CITATION_MARKER) + r"(?: excerpt: |\)|; )")


def is_web_citation(citation: Any) -> bool:
    """Whether ``citation`` is one ``cited`` wrote."""
    return isinstance(citation, str) and bool(_WEB_CITATION_RE.search(citation))


def cited(
    result: WebCitation, judgment: Optional[research_judging.Judgment] = None
) -> str:
    """The citation a kept result goes into the letter's context as: its
    title and URL, then the start of the page's excerpt, labelled as what
    it is, and, when ``judgment`` finds it probably off topic, a note
    saying so (research_judging.PROBABLY_OFF_TOPIC_NOTE)."""
    excerpt = result.excerpt[:CITED_EXCERPT_CHARS].strip()
    label = f"web page excerpt: {excerpt}" if excerpt else "web page"
    if judgment is not None and judgment.probably_off_topic:
        label = f"{label}; relevance note: judged {research_judging.PROBABLY_OFF_TOPIC_NOTE}"
    return f"{result} ({label})"


# One kept result and the judgment that kept it.
JudgedResult = Tuple[WebCitation, research_judging.Judgment]


async def _search_and_judge(
    procedure: Optional[str],
    diagnosis: Optional[str],
    budget_seconds: float,
    may_ask_jev: Callable[[], Awaitable[bool]],
    label: str,
) -> List[JudgedResult]:
    started = time.monotonic()
    question = build_question(procedure, diagnosis)
    if question is None:
        _count("skipped")
        return []
    backend = ml_router.web_search_backend()
    if backend is None:
        _count("skipped")
        return []
    with llm_usage.llm_task("web_search"):
        results = await backend.web_search(
            question, timeout=budget_seconds * SEARCH_SHARE
        )
    _count("searched")
    results = results[:MAX_JUDGED]
    _count("found", len(results))
    if not results:
        return []
    left = budget_seconds - (time.monotonic() - started)
    if left < _MIN_JUDGE_SECONDS:
        _count("unjudged", len(results))
        return []
    jev_allowed = await may_ask_jev()
    left = budget_seconds - (time.monotonic() - started)
    judgments = await _judge_all(results, procedure, diagnosis, left - 0.5, jev_allowed)
    kept, dropped = research_judging.order(
        [r for r in results if r.url in judgments],
        judgments,
        pmid_of=lambda r: r.url,
    )
    # With enough plainly on topic, the probably off-topic ones are left out
    # rather than offered with a note (research_judging.focus).
    kept, set_aside = research_judging.focus(kept, judgments, pmid_of=lambda r: r.url)
    unjudged = len(results) - len(judgments)
    _count("unjudged", unjudged)
    _count("dropped", len(dropped))
    _count("set_aside", len(set_aside))
    _count("kept", min(len(kept), MAX_KEPT))
    logger.debug(
        f"Web search for {label}: {len(results)} found, {len(kept)} kept, "
        f"{len(dropped)} judged off topic or against, {len(set_aside)} "
        f"probably off topic beside enough on topic, {unjudged} unjudged"
    )
    return [(r, judgments[r.url]) for r in kept[:MAX_KEPT]]


async def _never() -> bool:
    return False


async def judged_web_results(
    procedure: Optional[str],
    diagnosis: Optional[str],
    *,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    may_ask_jev: Callable[[], Awaitable[bool]] = _never,
    label: str = "a case",
) -> List[JudgedResult]:
    """The pages a web search on the procedure and diagnosis found, each
    with the judgment that kept it (on topic, not against the treatment),
    strongest support first, at most MAX_KEPT, within ``budget_seconds``;
    [] when there is no web search, no procedure, nothing relevant, or no
    time. ``may_ask_jev`` is awaited once there is something to judge, and
    says whether Jev may judge it. Never raises (cancellation aside).
    ``label`` names the work in the log lines, never the terms."""
    budget = min(float(budget_seconds), DEFAULT_BUDGET_SECONDS)
    if budget <= 0:
        return []
    try:
        return await asyncio.wait_for(
            _search_and_judge(procedure, diagnosis, budget, may_ask_jev, label),
            budget,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        _count("failed")
        logger.debug(f"Web search for {label} ran out of its {budget:.0f}s")
        return []
    except Exception as e:
        _count("failed")
        logger.warning(f"Web search for {label} failed ({type(e).__name__})")
        return []


async def web_search_citations(
    denial: Denial, budget_seconds: float = DEFAULT_BUDGET_SECONDS
) -> List[str]:
    """Citations from a web search on the denial's treatment, each judged
    relevant and not against it, within ``budget_seconds`` (see
    judged_web_results); Jev may judge them only with the person's
    external-models consent. Never raises (cancellation aside)."""
    kept = await judged_web_results(
        denial.procedure,
        denial.diagnosis,
        budget_seconds=budget_seconds,
        may_ask_jev=lambda: _consent_allows(denial),
        label=f"denial {getattr(denial, 'denial_id', None)}",
    )
    return [cited(result, judgment) for result, judgment in kept]

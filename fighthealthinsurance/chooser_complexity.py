"""How much work an Appeal Arena (chooser) case is to judge, as a score from
COMPLEXITY_MIN (1, the simplest) to COMPLEXITY_MAX (5), stored on
``ChooserTask.complexity``; and the warm-up that starts a new judge on the
simplest cases.

The score estimates what a voter does to judge a case well: read every draft
and the case they were written from (facts, answers, the evidence's titles
and summaries), then check each citation in the drafts against the evidence
on file. It is computed from the stored task alone (its ``context_json`` and
its active candidates' text), so it is deterministic and can be recomputed
for any task (``manage.py score_chooser_complexity``). It is a
reading-and-checking load, not a rating of how clinically hard the case is.

A task with no score (NULL) was built before the score existed, or its
scoring failed. The next-task ordering (``ordered_for_judge``) treats it as
COMPLEXITY_MAX without storing that, so a real score can be filled in later.
"""

import dataclasses
import re
from typing import Iterable, List, Optional

from django.conf import settings
from django.db.models import Case, F, IntegerField, QuerySet, Value, When

COMPLEXITY_MIN = 1
COMPLEXITY_MAX = 5

# The reading speed assumed of a voter going through the drafts and the case
# with care: slower than casual reading, since the point is to check claims.
_WORDS_PER_MINUTE = 230
# Minutes per citation in a draft: finding it in the evidence list, or
# noticing it isn't there. The list itself is read, and counted, as words.
_MINUTES_PER_CITATION = 0.2
# Citations past this add no work: by then the voter is checking the same
# handful of articles again.
_MAX_CITATIONS = 20
# The most minutes a case may take and still be in each band below the top
# one: at most 8 minutes is a 1, at most 14 a 2, and so on; over the last
# ceiling is COMPLEXITY_MAX. Provisional: set from what tasks look like, to
# be tuned against the real pool (score_chooser_complexity --dry-run -v 2
# reports every task's minutes) and applied with --rescore.
_BAND_CEILINGS = (8, 14, 21, 30)
assert len(_BAND_CEILINGS) == COMPLEXITY_MAX - COMPLEXITY_MIN

# What a citation looks like in a draft: a PMID, a PubMed link, a DOI, an
# "et al.", a bracketed number, or an author-year parenthesis. A draft may
# also cite an article by its title; those are counted by _title_mentions.
_CITATION_RE = re.compile(
    r"(?i:PMID)\s*:?\s*\d{4,9}"
    r"|(?i:pubmed\.ncbi\.nlm\.nih\.gov/\d+)"
    r"|\b(?i:doi)\s*:"
    r"|\bet al\b"
    r"|\[\d{1,2}\]"
    r"|\([A-Z][A-Za-z\-']+(?: (?:and|&) [A-Z][A-Za-z\-']+| et al\.?)?,? (?:19|20)\d{2}\)"
)
# A title shorter than this is too common a phrase to count its mentions.
_MIN_TITLE_CHARS = 12


@dataclasses.dataclass(frozen=True)
class ComplexityBreakdown:
    """What a case's score is made of, for the scoring command's report."""

    words: int
    articles: int
    citations: int
    drafts: int

    @property
    def minutes(self) -> float:
        """The estimated minutes to judge the case."""
        return self.words / _WORDS_PER_MINUTE + self.citations * _MINUTES_PER_CITATION

    @property
    def score(self) -> int:
        return band(self.minutes)


def band(minutes: float) -> int:
    """The complexity band ``minutes`` of judging falls in (_BAND_CEILINGS)."""
    for level, ceiling in enumerate(_BAND_CEILINGS, start=COMPLEXITY_MIN):
        if minutes <= ceiling:
            return level
    return COMPLEXITY_MAX


def _words(text: Optional[str]) -> int:
    return len((text or "").split())


def evidence_shown(context: Optional[dict]) -> List[dict]:
    """The evidence rows the voter is shown: those with a title (the page and
    the drafts' prompt both skip the rest)."""
    rows = (context or {}).get("evidence") or []
    return [e for e in rows if isinstance(e, dict) and e.get("title")]


def _case_text(context: Optional[dict]) -> List[str]:
    """The case as the voter reads it: every piece of text the page shows
    above the drafts (and the drafts were given), for an appeal or a chat."""
    context = context or {}
    pieces: List[str] = []
    for key in (
        "procedure",
        "diagnosis",
        "insurance_company",
        "denial_text_preview",
        "prompt",
    ):
        value = context.get(key)
        if isinstance(value, str):
            pieces.append(value)
    pieces.extend(str(fact) for fact in context.get("facts") or [] if fact)
    for pair in context.get("qa_pairs") or []:
        if isinstance(pair, dict):
            pieces.append(str(pair.get("question") or ""))
            pieces.append(str(pair.get("answer") or ""))
    for item in evidence_shown(context):
        pieces.append(str(item.get("title") or ""))
        pieces.append(str(item.get("summary") or ""))
    for turn in context.get("history") or []:
        if isinstance(turn, dict):
            pieces.append(str(turn.get("content") or ""))
        elif isinstance(turn, str):
            pieces.append(turn)
    return pieces


def _title_mentions(context: Optional[dict], drafts: List[str]) -> int:
    """How often the drafts cite an article on file by its title."""
    titles = {
        str(item["title"]).strip().lower()
        for item in evidence_shown(context)
        if len(str(item["title"]).strip()) >= _MIN_TITLE_CHARS
    }
    if not titles:
        return 0
    lowered = [draft.lower() for draft in drafts]
    return sum(draft.count(title) for title in titles for draft in lowered)


def count_citations(drafts: Iterable[str], context: Optional[dict] = None) -> int:
    """Citation-like mentions across ``drafts`` (and, given the case, mentions
    of its articles' titles), capped at _MAX_CITATIONS."""
    texts = [draft or "" for draft in drafts]
    found = sum(len(_CITATION_RE.findall(draft)) for draft in texts)
    return min(_MAX_CITATIONS, found + _title_mentions(context, texts))


def breakdown(context: Optional[dict], drafts: Iterable[str]) -> ComplexityBreakdown:
    """The judging load of a case given as its context and its drafts' text."""
    texts = [draft for draft in drafts if draft]
    words = sum(_words(draft) for draft in texts)
    words += sum(_words(piece) for piece in _case_text(context))
    return ComplexityBreakdown(
        words=words,
        articles=len(evidence_shown(context)),
        citations=count_citations(texts, context),
        drafts=len(texts),
    )


def score_task(context: Optional[dict], drafts: Iterable[str]) -> int:
    """The complexity score of a case: see the module docstring."""
    return breakdown(context, drafts).score


def _drafts_of(task) -> "QuerySet":
    from fighthealthinsurance.models import ChooserCandidate

    return ChooserCandidate.objects.filter(task=task, is_active=True).values_list(
        "content", flat=True
    )


def breakdown_for(task) -> ComplexityBreakdown:
    """``breakdown`` of a stored task, from its active candidates."""
    return breakdown(task.context_json, list(_drafts_of(task)))


async def ascore(task) -> int:
    """The score of a stored task, from its active candidates."""
    drafts = [draft async for draft in _drafts_of(task)]
    return score_task(task.context_json, drafts)


def warmup_rounds_per_level() -> int:
    """Votes a judge casts before the complexity ceiling rises a level
    (CHOOSER_WARMUP_ROUNDS_PER_LEVEL); 0 turns the warm-up off. Read per
    call so a deployment, or a test, can change it without a restart."""
    return max(0, int(getattr(settings, "CHOOSER_WARMUP_ROUNDS_PER_LEVEL", 1)))


def warmup_saturation_votes() -> int:
    """Votes after which a case has been judged enough to yield its place in
    the warm-up to a less-judged one (CHOOSER_WARMUP_SATURATION_VOTES)."""
    return max(1, int(getattr(settings, "CHOOSER_WARMUP_SATURATION_VOTES", 5)))


def warmup_level(rounds: int) -> int:
    """The complexity a judge who has cast ``rounds`` votes is ready for:
    COMPLEXITY_MIN to begin with, one level more every
    warmup_rounds_per_level() votes, and COMPLEXITY_MAX (every case) once
    warmed up, or at once when the warm-up is off."""
    per_level = warmup_rounds_per_level()
    if per_level <= 0:
        return COMPLEXITY_MAX
    return min(COMPLEXITY_MAX, COMPLEXITY_MIN + max(0, rounds) // per_level)


# The order the next-task endpoint has always served in: the newest case
# pipeline first (its votes are wanted first), then fewest votes, then oldest.
USUAL_ORDER = ("-case_version", "vote_count", "created_at")


def ordered_for_judge(tasks: "QuerySet", task_type: str, rounds: int) -> "QuerySet":
    """``tasks`` (annotated with ``vote_count``) in the order they are served
    to a judge who has cast ``rounds`` votes on ``task_type`` tasks.

    While the judge is warming up (warmup_level below COMPLEXITY_MAX):

    1. an appeal case with evidence attached before one without, so the
       first rounds still practise checking citations;
    2. a case judged fewer than warmup_saturation_votes() times before one
       judged that often, so the simplest cases do not take every new
       judge's first votes while the rest wait;
    3. the newest case pipeline first, as always;
    4. then by how far a case's complexity is above the judge's level, an
       unscored case counting as COMPLEXITY_MAX: every case within reach
       ties at zero and the usual keys decide between them, and when none
       is within reach the nearest is served;
    5. then fewest votes, then oldest.

    Once warmed up, or with the warm-up off, the usual order alone.
    """
    level = warmup_level(rounds)
    if level >= COMPLEXITY_MAX:
        return tasks.order_by(*USUAL_ORDER)
    keys: List[str] = []
    if task_type == "appeal":
        # evidence[0] exists: the list is non-empty. A missing key, a NULL
        # context and an empty list all read as no evidence.
        tasks = tasks.annotate(
            no_evidence=Case(
                When(context_json__evidence__0__isnull=False, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        keys.append("no_evidence")
    # A CASE rather than GREATEST(COALESCE(..) - level, 0): the subtraction
    # only runs where it stays positive, since on MySQL the column is an
    # unsigned smallint and a negative difference is an error, not a number;
    # and the unscored branch never reaches a NULL, whose handling GREATEST
    # and SQLite's MAX disagree on.
    tasks = tasks.annotate(
        saturated=Case(
            When(vote_count__gte=warmup_saturation_votes(), then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        ),
        over_level=Case(
            When(complexity__isnull=True, then=Value(COMPLEXITY_MAX - level)),
            When(complexity__gt=level, then=F("complexity") - Value(level)),
            default=Value(0),
            output_field=IntegerField(),
        ),
    )
    return tasks.order_by(
        *keys, "saturated", "-case_version", "over_level", "vote_count", "created_at"
    )

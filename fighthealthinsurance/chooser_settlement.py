"""When an Appeal Arena (chooser) case has been judged enough to stop
serving it: it has a **clear winner**, or it is **clearly undecided**.

Every review a case takes is a review some other case didn't get. Once the
votes already say which draft wins (or that none is usable), more of them
add little; and a case the judges split evenly on, or keep skipping, is
not going to give a clear answer however long it is served. Either way the
case is *settled*: ``ChooserTask.status`` goes to ``EXHAUSTED`` with
``settled_reason`` saying why, the next-task endpoint (which serves READY
tasks only) stops handing it out, and the pool counts the refill reads
stop counting it, so fresh cases are written in its place. Its votes stay
where they are and keep counting everywhere they did (rankings, exports).

The rules (``verdict``), from the task's votes and skips:

- A **review** is a vote, an all-bad one included. Each review's point is
  shared between its winners (``ChooserVote.shares``); an all-bad review
  gives its point to the outcome "no usable draft", which is ranked like a
  draft so that judges agreeing nothing is usable counts as an answer too.
- Two outcomes are compared **head to head** over the reviews that showed
  both: a review counts for one when it gave it more of its point than the
  other, and is no evidence either way when it gave them the same (both
  tied winners, or neither won).
- **Clear winner**: at least CHOOSER_SETTLE_MIN_REVIEWS reviews, and the
  leading outcome (the most credit) beats every other outcome head to head
  by a one-sided sign test at p <= CLEAR_WINNER_P (5-0, 6-0, 7-1 or 8-1, and
  so on, against its closest rival; 6-1 is not enough).
- **Clearly undecided**, when there is no clear winner:
  - *skipped*: at least CHOOSER_SETTLE_MIN_REVIEWS judges saw it (reviews
    plus counted skips, see ``counted_skips``) and at least
    CHOOSER_UNDECIDED_SKIP_SHARE of them skipped,
    so judges can't tell the drafts apart, or the case can't be judged;
  - *split*: at least CHOOSER_UNDECIDED_REVIEWS reviews, and the leader
    beats the runner-up in no more than EVEN_SPLIT of the reviews that
    tell them apart (or none tell them apart): the top is a coin toss;
  - *never resolved*: CHOOSER_UNDECIDED_MAX_REVIEWS reviews and still no
    clear winner.

``settle_task`` runs the rules after every vote and skip; it only ever
settles a READY task, never reopens one. ``manage.py settle_chooser_tasks``
applies them to the whole pool (after a change to the settings, or to
settle tasks voted on before this existed), and with ``--reopen`` puts a
settled task the rules no longer settle back in the pool.
"""

import dataclasses
import math
from typing import Any, Dict, Hashable, Iterable, List, Optional, Sequence, Tuple

from django.conf import settings
from django.utils import timezone
from loguru import logger

from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserSkip,
    ChooserTask,
    ChooserVote,
)

CLEAR_WINNER = "clear_winner"
UNDECIDED = "undecided"
SETTLED_REASONS = (CLEAR_WINNER, UNDECIDED)

# The outcome an all-bad review votes for, ranked beside the drafts.
NO_USABLE_DRAFT = "none"

# One-sided sign-test p the leader must reach against every other outcome.
CLEAR_WINNER_P = 0.05
# The leader's head-to-head win rate over the runner-up at or below which a
# well-reviewed case counts as split.
EVEN_SPLIT = 0.6


def enabled() -> bool:
    """Whether cases are settled at all (CHOOSER_SETTLE_TASKS)."""
    return bool(getattr(settings, "CHOOSER_SETTLE_TASKS", True))


def min_reviews() -> int:
    return max(1, int(getattr(settings, "CHOOSER_SETTLE_MIN_REVIEWS", 5)))


def undecided_reviews() -> int:
    return max(1, int(getattr(settings, "CHOOSER_UNDECIDED_REVIEWS", 10)))


def undecided_max_reviews() -> int:
    return max(1, int(getattr(settings, "CHOOSER_UNDECIDED_MAX_REVIEWS", 20)))


def undecided_skip_share() -> float:
    return float(getattr(settings, "CHOOSER_UNDECIDED_SKIP_SHARE", 0.5))


@dataclasses.dataclass(frozen=True)
class Review:
    """One vote, as the rules read it: the candidates shown, and the share
    of the vote's point each outcome got (an all-bad vote's goes to
    NO_USABLE_DRAFT)."""

    presented: frozenset
    shares: Dict[Hashable, float]

    @classmethod
    def from_vote(
        cls,
        presented_candidate_ids: Optional[Sequence[Any]],
        chosen_candidate_id: Optional[int],
        tied_candidate_ids: Optional[Sequence[int]],
        all_bad: bool,
    ) -> "Review":
        shares: Dict[Hashable, float] = {NO_USABLE_DRAFT: 1.0}
        if not all_bad:
            shares = {
                cid: share
                for cid, share in ChooserVote.shares(
                    chosen_candidate_id, list(tied_candidate_ids or [])
                ).items()
            }
        presented = frozenset(
            cid for cid in (presented_candidate_ids or []) if isinstance(cid, int)
        )
        return cls(presented=presented | {NO_USABLE_DRAFT}, shares=shares)


@dataclasses.dataclass(frozen=True)
class Verdict:
    """Why a case is settled: ``reason`` (CLEAR_WINNER or UNDECIDED) and a
    line for the logs."""

    reason: str
    detail: str


def sign_test_p(wins: int, losses: int) -> float:
    """One-sided sign-test p: the chance of at least ``wins`` of
    ``wins + losses`` head-to-head reviews going one way if either side
    were as likely. 1.0 with no reviews to go on."""
    n = wins + losses
    if n <= 0:
        return 1.0
    return sum(math.comb(n, k) for k in range(wins, n + 1)) / (1 << n)


def head_to_head(
    reviews: Iterable[Review], a: Hashable, b: Hashable
) -> Tuple[int, int]:
    """(reviews preferring ``a``, reviews preferring ``b``) over the reviews
    that showed both; a review that gave them the same share is neither."""
    wins = losses = 0
    for review in reviews:
        if a not in review.presented or b not in review.presented:
            continue
        sa, sb = review.shares.get(a, 0.0), review.shares.get(b, 0.0)
        if sa > sb:
            wins += 1
        elif sb > sa:
            losses += 1
    return wins, losses


def verdict(
    reviews: Sequence[Review], skips: int, candidate_ids: Iterable[int]
) -> Optional[Verdict]:
    """Whether a case with these ``reviews`` (its votes), ``skips`` and
    active ``candidate_ids`` is settled, and why; None while it is still
    worth serving. See the module docstring for the rules."""
    reviews = list(reviews)
    n = len(reviews)
    outcomes: List[Hashable] = [*dict.fromkeys(candidate_ids), NO_USABLE_DRAFT]
    credit: Dict[Hashable, float] = {o: 0.0 for o in outcomes}
    for review in reviews:
        for outcome, share in review.shares.items():
            if outcome in credit:
                credit[outcome] += share
    # Highest credit first; a dead heat keeps the candidates' own order.
    ranked = sorted(outcomes, key=lambda o: -credit[o])
    leader = ranked[0]
    rivals = ranked[1:]

    if n >= min_reviews() and credit[leader] > 0 and rivals:
        worst_p = max(
            sign_test_p(*head_to_head(reviews, leader, rival)) for rival in rivals
        )
        if worst_p <= CLEAR_WINNER_P:
            name = "no usable draft" if leader == NO_USABLE_DRAFT else f"{leader}"
            return Verdict(
                CLEAR_WINNER,
                f"{name} leads every other outcome (worst sign-test "
                f"p={worst_p:.3f}) over {n} review(s)",
            )

    seen = n + skips
    if seen >= min_reviews() and skips >= undecided_skip_share() * seen:
        return Verdict(UNDECIDED, f"skipped by {skips} of {seen} judge(s)")
    if n >= undecided_max_reviews():
        return Verdict(UNDECIDED, f"no clear winner after {n} review(s)")
    if n >= undecided_reviews() and rivals:
        wins, losses = head_to_head(reviews, leader, ranked[1])
        decided = wins + losses
        if decided == 0 or wins / decided <= EVEN_SPLIT:
            return Verdict(
                UNDECIDED,
                f"leader and runner-up split {wins}-{losses} over {n} review(s)",
            )
    return None


def counted_skips(task_id: int) -> int:
    """The task's skips that count toward settling it: made after the case
    was served to that session (ChooserSkip.served_at), by a judge who has
    not since voted on it (by session or signed-in user, as the endpoints
    tell judges apart), so nobody is both a reviewer and a skipper."""
    votes = ChooserVote.objects.filter(task_id=task_id)
    return (
        ChooserSkip.objects.filter(task_id=task_id, served_at__isnull=False)
        .exclude(session_key__in=votes.values("session_key"))
        .exclude(user__in=votes.filter(user__isnull=False).values("user"))
        .count()
    )


def task_verdict(task_id: int) -> Optional[Verdict]:
    """``verdict`` for the stored task: its votes, skips and active
    candidates, read from the database."""
    reviews = [
        Review.from_vote(presented, chosen, tied, all_bad)
        for presented, chosen, tied, all_bad in ChooserVote.objects.filter(
            task_id=task_id
        ).values_list(
            "presented_candidate_ids",
            "chosen_candidate_id",
            "tied_candidate_ids",
            "all_bad",
        )
    ]
    skips = counted_skips(task_id)
    candidate_ids = ChooserCandidate.objects.filter(
        task_id=task_id, is_active=True
    ).values_list("id", flat=True)
    return verdict(reviews, skips, sorted(candidate_ids))


def settle_task(task_id: int) -> Optional[Verdict]:
    """Settle the task if the rules say so and it is still READY: status
    EXHAUSTED, with ``settled_reason`` and ``settled_at``. Returns the
    verdict it settled on, or None (rules off, not settled, or no longer
    READY). Called after each vote and skip; never reopens a task."""
    if not enabled():
        return None
    found = task_verdict(task_id)
    if found is None:
        return None
    # Conditional, so two votes landing together settle it once.
    updated = ChooserTask.objects.filter(id=task_id, status="READY").update(
        status="EXHAUSTED",
        settled_reason=found.reason,
        settled_at=timezone.now(),
        updated_at=timezone.now(),
    )
    if not updated:
        return None
    logger.info(f"Chooser task {task_id} settled ({found.reason}): {found.detail}")
    return found

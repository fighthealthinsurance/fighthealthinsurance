"""What HumanJudge's reviewers make of the Arena's models, for the public
rankings page and for staff.

HumanJudge's verdicts (``HumanJudgeVerdict``, see chooser_humanjudge) are
kept apart from the Arena's own votes, and they are counted apart here too:
a pass rate per model sits beside the organic win rate, never folded into
it, so the rankings stay ordered by our voters alone.

* ``model_verdict_stats`` is the per-model tally both pages read.
* ``breakdown`` is the staff page's data (the admin's HumanJudge breakdown,
  ``HumanJudgeVerdictAdmin.breakdown_view``): each model's verdicts beside
  its Arena record, flag categories, how often HumanJudge's pick agrees
  with the Arena's voters and with Jev and fhi-local, and the reviewers'
  latest written feedback. Feedback is free text from outside reviewers, so
  it is shown to staff only, never on the public page.

Only verdicts on synthetic appeal cases are counted, though nothing else is
ever sent.
"""

import datetime
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List, Optional

from django.db.models import Count, Q
from django.db.models.functions import Coalesce

from fighthealthinsurance.ml.model_identity import normalize_model_label
from fighthealthinsurance.models import (
    ChooserTask,
    ChooserVote,
    HumanJudgeSubmission,
    HumanJudgeVerdict,
)

SYNTHETIC_SOURCE = "synthetic"
APPEAL_KIND = "appeal_letter"
# The judges whose picks are compared with HumanJudge's (scoring_json keys).
OTHER_JUDGES = (("jev", "Jev"), ("fhi_local", "fhi-local"))
RECENT_FEEDBACK = 25

LabelFor = Callable[[Optional[str]], Optional[str]]


def _verdicts(since: Optional[datetime.datetime] = None):
    verdicts = HumanJudgeVerdict.objects.filter(
        task__source=SYNTHETIC_SOURCE, candidate__kind=APPEAL_KIND
    )
    if since is not None:
        # A vote HumanJudge gave no time for counts from when we stored it.
        verdicts = verdicts.annotate(at=Coalesce("voted_at", "fetched_at")).filter(
            at__gte=since
        )
    return verdicts


def _rate(part: int, whole: int) -> Optional[float]:
    return round(100.0 * part / whole, 1) if whole else None


def model_verdict_stats(
    since: Optional[datetime.datetime] = None,
    label_for: LabelFor = normalize_model_label,
) -> Dict[str, Dict[str, Any]]:
    """Each model's HumanJudge verdicts on Arena appeal drafts since
    ``since``: reviews, passes, flags, pass rate (a percentage, like the
    Arena's win rate) and flags by category. ``label_for`` groups stored
    model names as the caller's table does."""
    rows = (
        _verdicts(since)
        .values("candidate__model_name", "verdict", "flag_category")
        .annotate(n=Count("id"))
    )
    stats: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        label = label_for(row["candidate__model_name"])
        if label is None:
            continue
        entry = stats.setdefault(
            label,
            {"reviews": 0, "passes": 0, "flags": 0, "flag_categories": Counter()},
        )
        entry["reviews"] += row["n"]
        if row["verdict"] == "pass":
            entry["passes"] += row["n"]
        else:
            entry["flags"] += row["n"]
            entry["flag_categories"][row["flag_category"] or "(none given)"] += row["n"]
    for entry in stats.values():
        entry["pass_rate"] = _rate(entry["passes"], entry["reviews"])
        entry["flag_categories"] = dict(entry["flag_categories"].most_common())
    return stats


def _arena_winner(task_votes: List[tuple]) -> Optional[int]:
    """The candidate the Arena's voters gave the most credit on one case
    (votes as (chosen id, tied ids)), or None with no winner or a tie for
    most."""
    credit: Dict[int, float] = defaultdict(float)
    for chosen_id, tied_ids in task_votes:
        for candidate_id, share in ChooserVote.shares(chosen_id, tied_ids).items():
            credit[candidate_id] += share
    ranked = sorted(credit.items(), key=lambda item: -item[1])[:2]
    if not ranked or (len(ranked) > 1 and ranked[0][1] == ranked[1][1]):
        return None
    return int(ranked[0][0])


def _agreement() -> List[Dict[str, Any]]:
    """How often HumanJudge's pick for a case is the same draft as each
    other judge's: the Arena's voters (their most-credited draft), Jev and
    fhi-local. Only cases where both picked count."""
    picks: Dict[int, int] = {}
    others: Dict[str, Dict[int, int]] = defaultdict(dict)
    for task_id, scoring in ChooserTask.objects.filter(
        source=SYNTHETIC_SOURCE, scoring_json__has_key="humanjudge"
    ).values_list("id", "scoring_json"):
        pick = (scoring or {}).get("humanjudge") or {}
        if "candidate_id" not in pick:
            continue
        picks[task_id] = pick["candidate_id"]
        for key, _ in OTHER_JUDGES:
            other = (scoring or {}).get(key) or {}
            if "candidate_id" in other:
                others[key][task_id] = other["candidate_id"]
    votes_by_task: Dict[int, List[tuple]] = defaultdict(list)
    for task_id, chosen_id, tied_ids in ChooserVote.objects.filter(
        task_id__in=list(picks)
    ).values_list("task_id", "chosen_candidate_id", "tied_candidate_ids"):
        votes_by_task[task_id].append((chosen_id, tied_ids))
    others["arena"] = {
        task_id: winner
        for task_id, task_votes in votes_by_task.items()
        if (winner := _arena_winner(task_votes)) is not None
    }
    out = []
    for key, label in (("arena", "Arena voters"), *OTHER_JUDGES):
        both = [t for t in picks if t in others[key]]
        agree = sum(1 for t in both if picks[t] == others[key][t])
        out.append(
            {
                "judge": label,
                "cases": len(both),
                "agree": agree,
                "agree_rate": _rate(agree, len(both)),
            }
        )
    return out


def breakdown() -> Dict[str, Any]:
    """Everything the staff breakdown page and its JSON show."""
    from fighthealthinsurance.staff_views import ModelUsageDashboardView

    verdict_stats = model_verdict_stats()
    arena = {
        row["model_name"]: row
        for row in ModelUsageDashboardView._chooser_stats(
            APPEAL_KIND, None, source=SYNTHETIC_SOURCE
        )
    }
    uploaded: Counter = Counter()
    for model_name, n in (
        HumanJudgeSubmission.objects.filter(status="SENT")
        .values_list("candidate__model_name")
        .annotate(n=Count("id"))
    ):
        label = normalize_model_label(model_name)
        if label is not None:
            uploaded[label] += n
    models = []
    for label in sorted(set(verdict_stats) | set(uploaded)):
        stats = verdict_stats.get(label) or {
            "reviews": 0,
            "passes": 0,
            "flags": 0,
            "pass_rate": None,
            "flag_categories": {},
        }
        arena_row = arena.get(label) or {}
        models.append(
            {
                "model": label,
                "uploaded": uploaded[label],
                **stats,
                "arena_shown": arena_row.get("presented", 0),
                "arena_won": arena_row.get("chosen", 0),
                "arena_win_rate": arena_row.get("win_rate"),
                "arena_flag_rate": arena_row.get("flag_rate"),
            }
        )
    models.sort(key=lambda r: (-(r["pass_rate"] or -1.0), -r["reviews"], r["model"]))

    verdicts = _verdicts()
    totals = verdicts.aggregate(
        reviews=Count("id"),
        passes=Count("id", filter=Q(verdict="pass")),
        flags=Count("id", filter=Q(verdict="flag")),
        cases_reviewed=Count("task_id", distinct=True),
    )
    submissions = dict(
        HumanJudgeSubmission.objects.values_list("status").annotate(n=Count("id"))
    )
    totals.update(
        {
            "drafts_sent": submissions.get("SENT", 0),
            "drafts_failed": submissions.get("FAILED", 0),
            "drafts_pending": submissions.get("PENDING", 0),
            "cases_uploaded": HumanJudgeSubmission.objects.filter(status="SENT")
            .values("task_id")
            .distinct()
            .count(),
            "cases_with_pick": ChooserTask.objects.filter(
                source=SYNTHETIC_SOURCE, scoring_json__has_key="humanjudge"
            ).count(),
            "pass_rate": _rate(totals["passes"], totals["reviews"]),
        }
    )
    flag_categories = dict(
        Counter(
            {
                (row["flag_category"] or "(none given)"): row["n"]
                for row in verdicts.filter(verdict="flag")
                .values("flag_category")
                .annotate(n=Count("id"))
            }
        ).most_common()
    )
    recent = [
        {
            "task_id": v.task_id,
            "candidate_id": v.candidate_id,
            "model": normalize_model_label(v.candidate.model_name),
            "verdict": v.verdict,
            "flag_category": v.flag_category,
            "feedback": v.feedback,
            "voted_at": v.voted_at.isoformat() if v.voted_at else None,
        }
        for v in verdicts.exclude(feedback="")
        .select_related("candidate")
        .order_by("-fetched_at", "-id")[:RECENT_FEEDBACK]
    ]
    return {
        "totals": totals,
        "models": models,
        "agreement": _agreement(),
        "flag_categories": flag_categories,
        "recent_feedback": recent,
    }

"""The public rankings page: ``/chooser/rankings/``.

What the Appeal Arena's votes say about the models, for anyone to read,
and which conditions and treatments people fight denials over most. Each
table names its leader (the first model past PUBLIC_RANKINGS_MIN_SHOWN), and
``leaders`` at the top of the data repeats the all-time ones so the page and
the Arena's landing can say who is winning without reading the tables.

Everything on it is either synthetic or aggregate:

* The model tables count ``ChooserVote`` rows on tasks whose source is
  ``synthetic`` and nothing else. Real patients' picks in the denial flow
  (``ProposedAppeal``), call logs, chat turns, session counts and anything
  else the staff dashboard shows stay off this page.
* The condition and treatment lists come from ``condition_rankings``: terms
  on at least k distinct denials, shaped like medical terms, vouched for by
  a medical vocabulary, with counts rounded down to a multiple of k. Only
  the top ``CONDITION_RANKING_LIMIT`` (5) of each are shown: a short list of the
  most common terms is defense in depth should something that is not a
  medical term (a name, say) ever slip past those filters, since a string
  only reaches the top few by being something a great many people typed.

Models are shown by model name alone (``public_model_label``): a
"provider/model" label is cut to "model", and every provider serving the same
model counts as one row, on the broad assumption that they serve the same
weights.

The numbers are computed at most once per ``PUBLIC_RANKINGS_CACHE_SECONDS``
per process and never touch a model or NCBI. ``?format=json`` returns the
same data as JSON for embedding.
"""

import datetime
from typing import Any, Dict, List, Optional, cast

from django.conf import settings
from django.core.cache import cache
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.generic import TemplateView
from loguru import logger

from fighthealthinsurance import condition_rankings
from fighthealthinsurance.ml.model_identity import normalize_model_label
from fighthealthinsurance.models import ChooserTask, ChooserVote
from fighthealthinsurance.staff_views import ModelUsageDashboardView
from fighthealthinsurance.utils import arena_rankings_page_enabled

PUBLIC_RANKINGS_CACHE_SECONDS = getattr(
    settings, "PUBLIC_RANKINGS_CACHE_SECONDS", 15 * 60
)
# A model's win rate is shown as a rate once it has been in this many votes;
# below that the row is listed as still warming up and sorted to the end.
PUBLIC_RANKINGS_MIN_SHOWN = getattr(settings, "PUBLIC_RANKINGS_MIN_SHOWN", 10)
# The earliest vote the page counts. Votes cast before blind voting shipped
# were collected under a payload that named the model, so they do not belong
# in a leaderboard that says a vote is for the letter. By default that is
# when the migration that shipped with blind voting was applied (read from
# django_migrations); set to an ISO 8601 datetime to override, or to "" to
# count every vote.
PUBLIC_RANKINGS_SINCE = getattr(settings, "PUBLIC_RANKINGS_SINCE", None)
_BLIND_VOTING_MIGRATION = "0240_chooser_vote_flags_and_medical_term_check"
SYNTHETIC_SOURCE = "synthetic"
_CACHE_KEY = "public_rankings:data"

WINDOWS = (("all", "All time", None), ("30d", "Last 30 days", 30))
KINDS = (("appeal_letter", "Appeal letters"), ("chat_response", "Chat replies"))


def blind_since() -> Optional[datetime.datetime]:
    """When blind voting began here: the lower bound on the votes the page
    counts (see PUBLIC_RANKINGS_SINCE). None counts every vote, and only an
    explicit empty setting says so; when the date cannot be established the
    page fails closed and counts nothing, since a leaderboard of votes that
    may have seen the model name is worse than an empty one."""
    if PUBLIC_RANKINGS_SINCE is not None:
        if not PUBLIC_RANKINGS_SINCE:
            return None
        try:
            parsed = datetime.datetime.fromisoformat(str(PUBLIC_RANKINGS_SINCE))
        except ValueError:
            logger.warning(
                f"PUBLIC_RANKINGS_SINCE is not an ISO 8601 datetime: "
                f"{PUBLIC_RANKINGS_SINCE!r}; counting no votes until it is fixed"
            )
            return timezone.now()
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, datetime.timezone.utc)
        return parsed
    from django.db.migrations.recorder import MigrationRecorder

    try:
        applied = (
            MigrationRecorder.Migration.objects.filter(
                app="fighthealthinsurance", name=_BLIND_VOTING_MIGRATION
            )
            .values_list("applied", flat=True)
            .first()
        )
    except Exception as e:
        logger.warning(f"Could not read when blind voting began: {e}")
        applied = None
    if applied is None:
        # No record of the migration: nothing says which votes were blind.
        return timezone.now()
    return cast(datetime.datetime, applied)


def _later(a: Optional[datetime.datetime], b: Optional[datetime.datetime]):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def public_model_label(raw: Optional[str]) -> Optional[str]:
    """A stored model name as the public page shows and groups it: the
    reporting label (normalize_model_label) without any "provider/" prefix,
    so "lunaroute/deepseek-4.1-flash" and "deepseek-4.1-flash" are one row.
    We assume, broadly, that providers serving one model are equivalent."""
    label = normalize_model_label(raw)
    if label is None:
        return None
    return label.rsplit("/", 1)[-1].strip() or label


def _model_rows(kind: str, since: Optional[datetime.datetime]) -> List[Dict[str, Any]]:
    """The leaderboard rows for ``kind`` since ``since``: synthetic votes
    only, models with a denominator only, best win rate first among those
    past PUBLIC_RANKINGS_MIN_SHOWN, then the rest by votes shown."""
    rows = ModelUsageDashboardView._chooser_stats(
        kind, since, None, source=SYNTHETIC_SOURCE, label_for=public_model_label
    )
    out = []
    for r in rows:
        if r["presented"] <= 0:
            continue
        out.append(
            {
                "model": r["model_name"],
                "won": r["chosen"],
                "shown": r["presented"],
                "win_rate": r["win_rate"],
                "flagged": r["flagged"],
                "flag_rate": r["flag_rate"],
                "warming_up": r["presented"] < PUBLIC_RANKINGS_MIN_SHOWN,
            }
        )
    out.sort(
        key=lambda r: (
            r["warming_up"],
            -(r["win_rate"] or 0.0) if not r["warming_up"] else -r["shown"],
            r["flag_rate"] or 0.0,
            r["model"],
        )
    )
    for rank, r in enumerate(out, start=1):
        r["rank"] = rank
    return out


def _leader(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The model winning a table: its first ranked row, or None while every
    model is still warming up (a leader on three votes is noise, and the
    page says so instead of crowning one)."""
    for r in rows:
        if not r["warming_up"]:
            return r
    return None


def _table(
    kind: str, kind_label: str, since: Optional[datetime.datetime]
) -> Dict[str, Any]:
    """One leaderboard: its rows, how many of them are ranked (past warming
    up, so the ones a leader is ahead of), and its leader."""
    rows = _model_rows(kind, since)
    return {
        "kind": kind,
        "label": kind_label,
        "rows": rows,
        "ranked": sum(1 for r in rows if not r["warming_up"]),
        "leader": _leader(rows),
    }


def _totals(since: Optional[datetime.datetime]) -> Dict[str, int]:
    votes = ChooserVote.objects.filter(task__source=SYNTHETIC_SOURCE)
    if since is not None:
        votes = votes.filter(created_at__gte=since)
    return {
        "votes": votes.count(),
        "tasks_judged": votes.values("task_id").distinct().count(),
        "tasks_ready": ChooserTask.objects.filter(
            source=SYNTHETIC_SOURCE, status="READY"
        ).count(),
    }


def _term_rows(kind: str) -> List[Dict[str, Any]]:
    rows = condition_rankings.ranked_terms_cached(
        kind, condition_rankings.CONDITION_RANKING_LIMIT
    )
    # Bar widths come from the rounded counts, like the numbers beside
    # them: a width from the true counts would tell two "5+" terms apart.
    top = rows[0].public_count if rows else 0
    return [
        {
            "rank": i,
            "name": r.display,
            "count": r.public_count,
            "share": round(100.0 * r.public_count / top) if top else 0,
        }
        for i, r in enumerate(rows, start=1)
    ]


def compute_rankings(now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """Everything the page and its JSON show, uncached."""
    now = now or timezone.now()
    floor = blind_since()
    windows: List[Dict[str, Any]] = []
    for slug, label, days in WINDOWS:
        since = _later(now - datetime.timedelta(days=days) if days else None, floor)
        windows.append(
            {
                "slug": slug,
                "label": label,
                "tables": [
                    _table(kind, kind_label, since) for kind, kind_label in KINDS
                ],
            }
        )
    # Who is winning, in one place: the all-time leader of each table, for
    # the top of the page and for anything that embeds the JSON (the Arena's
    # own landing shows the appeal-letter standings from it).
    leaders = {t["kind"]: t["leader"] for t in windows[0]["tables"]}
    return {
        "generated_at": now.isoformat(),
        "leaders": leaders,
        "min_shown": PUBLIC_RANKINGS_MIN_SHOWN,
        "k_anonymity": condition_rankings.CONDITION_RANKING_MIN_PEOPLE,
        "blind_since": floor.isoformat() if floor else None,
        "totals": _totals(floor),
        "windows": windows,
        "conditions": _term_rows(condition_rankings.KIND_CONDITION),
        "treatments": _term_rows(condition_rankings.KIND_TREATMENT),
    }


def rankings() -> Dict[str, Any]:
    """compute_rankings, remembered for PUBLIC_RANKINGS_CACHE_SECONDS. A
    cache that cannot answer is not an error."""
    try:
        hit = cache.get(_CACHE_KEY)
    except Exception as e:
        logger.debug(f"public rankings cache read failed: {e}")
        hit = None
    if hit is not None:
        return cast(Dict[str, Any], hit)
    data = compute_rankings()
    try:
        cache.set(_CACHE_KEY, data, PUBLIC_RANKINGS_CACHE_SECONDS)
    except Exception as e:
        logger.debug(f"public rankings cache write failed: {e}")
    return data


class ChooserRankingsView(TemplateView):
    """The page and its JSON. Staged behind ARENA_RANKINGS_PAGE_ENABLED: the
    route always exists so reverse() and templates never break, but every
    method 404s until the flag is on, and the links to it hide with it
    (``context_processors.arena_rankings_context``, the sitemap)."""

    template_name = "chooser_rankings.html"

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        if not arena_rankings_page_enabled():
            raise Http404("This page is not available yet.")
        return super().dispatch(request, *args, **kwargs)

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.GET.get("format") == "json":
            response = JsonResponse(rankings())
            response["Cache-Control"] = (
                f"public, max-age={PUBLIC_RANKINGS_CACHE_SECONDS}"
            )
            return response
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> Dict[str, Any]:
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "Appeal Arena Rankings"
        ctx["data"] = rankings()
        return ctx

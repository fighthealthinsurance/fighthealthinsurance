"""HumanJudge (humanjudge.com) as an outside judge of Appeal Arena drafts.

HumanJudge's reviewers are people, and they read what they are sent, so only
the Arena's synthetic appeal drafts ever go there: never a real denial, a
real appeal or anything about a person. Each draft is sent on its own, as a
trace (``POST /api/v1/traces/ingest``) whose input is the made-up case and
whose output is the draft. Reviewers, blind to the model, give it a pass or a
flag. HumanJudge has no side-by-side comparison, so the case's pick is the
draft with the best smoothed pass rate once every draft has enough verdicts.

The verdicts are kept in their own table (``HumanJudgeVerdict``), apart from
the Arena's ``ChooserVote`` rows, so they never count toward our voters'
tallies or the public rankings. What they decide is one entry,
``"humanjudge"``, in ``ChooserTask.scoring_json``, beside Jev's and
fhi-local's picks.

``sync`` does one round: send the drafts not sent yet, fetch new verdicts
(``GET /api/v1/models/{id}/evaluations?detail=votes``) and update the picks
of the cases they touched. The chooser refill actor runs it every tick, and
``manage.py humanjudge_sync`` runs it by hand. It is off unless
``HUMANJUDGE_API_KEY`` is set (and ``CHOOSER_HUMANJUDGE`` is not false).
"""

import datetime
import hashlib
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import httpx
from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone
from loguru import logger

from fighthealthinsurance.chooser_scoring import _pick
from fighthealthinsurance.context_utils import truncate_at_boundary
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserTask,
    HumanJudgeSubmission,
    HumanJudgeVerdict,
)

PICK_KEY = "humanjudge"
SCORER = "humanjudge/pass-rate-1"
# What HumanJudge files the traces under: one name for every draft, so the
# reviewers learn nothing about which model wrote one.
TRACE_NAME = "fhi-appeal-arena"
# Cases whose drafts have been in front of our voters. Never QUEUED (still
# being written) or DISABLED (not fit to show anyone).
SENDABLE_STATUSES = ("READY", "IN_USE", "EXHAUSTED")
MAX_ATTEMPTS = 5
_HTTP_TIMEOUT_SECONDS = 20.0
# HumanJudge's page cap (the SDK's PAGE_SIZE), and how many pages one sync reads.
_PAGE_SIZE = 1000
_MAX_PAGES = 20
# Verdicts are re-read this far back from the newest one stored, for votes
# HumanJudge records late; the unique key drops the ones already stored.
_REFETCH_OVERLAP = datetime.timedelta(days=2)
_EVIDENCE_LIMIT = 6
_SUMMARY_CHARS = 600
_DRAFT_CHARS = 12000

_REVIEWER_BRIEF = (
    "This is a made-up health insurance denial, written to test AI-drafted "
    "appeal letters; there is no real patient. Read the case, then judge the "
    "appeal letter written for it. Pass it if it is accurate to the case, "
    "answers the denial's reason, and could be sent with light edits. Flag "
    "it if it invents facts, studies, laws or test results that are not in "
    "the case, misses the point of the denial, or would not help."
)


class HumanJudgeError(Exception):
    """A HumanJudge request that did not succeed."""


class HumanJudgeAuthError(HumanJudgeError):
    """HumanJudge refused the key: every further request would fail too."""


def enabled() -> bool:
    return bool(
        getattr(settings, "CHOOSER_HUMANJUDGE", False)
        and getattr(settings, "HUMANJUDGE_API_KEY", "")
    )


def new_inference_id() -> str:
    """An id in the form HumanJudge's own SDK makes (gj_inf_<ms>_<hex>)."""
    return f"gj_inf_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


def render_case(context: Optional[dict]) -> str:
    """The case as a reviewer reads it: the brief, then the made-up facts,
    the simulated interview and the evidence the drafts were given."""
    context = context or {}
    lines = [_REVIEWER_BRIEF, "", "THE CASE (synthetic)"]
    for label, key in (
        ("Treatment denied", "procedure"),
        ("Diagnosis", "diagnosis"),
        ("Insurer", "insurance_company"),
        ("Denial reason", "denial_text_preview"),
    ):
        value = context.get(key)
        if value:
            lines.append(f"{label}: {value}")
    facts = [str(f) for f in context.get("facts") or [] if f]
    if facts:
        lines += ["", "Case facts:"] + [f"- {fact}" for fact in facts]
    qa_pairs = [
        pair
        for pair in context.get("qa_pairs") or []
        if isinstance(pair, dict) and pair.get("question")
    ]
    if qa_pairs:
        lines += ["", "What the patient said when asked:"]
        for pair in qa_pairs:
            lines.append(f"Q: {pair.get('question')}")
            lines.append(f"A: {pair.get('answer') or ''}")
    evidence = [
        item
        for item in context.get("evidence") or []
        if isinstance(item, dict) and item.get("title")
    ][:_EVIDENCE_LIMIT]
    if evidence:
        lines += ["", "Evidence the letter writers were given:"]
        for item in evidence:
            lines.append(
                f"- {item['title']}" + (f" ({item['url']})" if item.get("url") else "")
            )
            if item.get("summary"):
                summary = truncate_at_boundary(str(item["summary"]), _SUMMARY_CHARS)
                lines.append(f"  {summary}")
    return "\n".join(lines)


class HumanJudgeClient:
    """The two HumanJudge calls the Arena needs, over one HTTP connection
    pool. ``transport`` is for tests."""

    def __init__(self, transport: Optional[httpx.AsyncBaseTransport] = None):
        self._api_key = settings.HUMANJUDGE_API_KEY
        # A personal access token reads more than a model key can (the SDK
        # prefers it too); sending always takes the model key.
        self._read_key = getattr(settings, "HUMANJUDGE_TOKEN", "") or self._api_key
        self._base_url = settings.HUMANJUDGE_BASE_URL.rstrip("/")
        self._transport = transport
        self._http: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "HumanJudgeClient":
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=_HTTP_TIMEOUT_SECONDS,
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError("HumanJudgeClient used outside 'async with'")
        return self._http

    @staticmethod
    def _check(response: httpx.Response, what: str) -> None:
        if response.status_code in (401, 403):
            raise HumanJudgeAuthError(f"{what}: HTTP {response.status_code}")
        if response.status_code not in (200, 201):
            raise HumanJudgeError(f"{what}: HTTP {response.status_code}")

    async def ingest(
        self, inference_id: str, input_text: str, output_text: str, metadata: dict
    ) -> None:
        response = await self.http.post(
            "/api/v1/traces/ingest",
            json={
                "gj_inference_id": inference_id,
                "name": TRACE_NAME,
                "input": input_text,
                "output": output_text,
                "model": TRACE_NAME,
                "metadata": metadata,
            },
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        self._check(response, "trace ingest")

    async def model_id(self) -> Optional[str]:
        """HUMANJUDGE_MODEL_ID, or the key's only model."""
        configured = getattr(settings, "HUMANJUDGE_MODEL_ID", "")
        if configured:
            return str(configured)
        response = await self.http.get(
            "/api/v1/models/me",
            headers={"Authorization": f"Bearer {self._read_key}"},
        )
        self._check(response, "models/me")
        models = _records(response.json())
        if len(models) != 1 or not models[0].get("id"):
            logger.warning(
                f"HumanJudge: the key has {len(models)} models; set "
                "HUMANJUDGE_MODEL_ID to the Arena's to read its verdicts"
            )
            return None
        return str(models[0]["id"])

    async def votes(
        self, model_id: str, from_date: Optional[datetime.date]
    ) -> list[dict]:
        """Every vote on the model since ``from_date``, a page at a time."""
        out: list[dict] = []
        params: dict[str, Any] = {"detail": "votes", "limit": _PAGE_SIZE}
        if from_date is not None:
            params["from_date"] = from_date.isoformat()
        for page in range(_MAX_PAGES):
            response = await self.http.get(
                f"/api/v1/models/{model_id}/evaluations",
                params={**params, "offset": page * _PAGE_SIZE},
                headers={"Authorization": f"Bearer {self._read_key}"},
            )
            self._check(response, "evaluations")
            records = _records(response.json())
            out.extend(records)
            if len(records) < _PAGE_SIZE:
                break
        else:
            logger.warning(
                f"HumanJudge: stopped reading votes after {_MAX_PAGES} pages; "
                "the rest come on the next sync"
            )
        return out


def _records(body: Any) -> list[dict]:
    """A page's rows: the SDK reads a bare list; accept the usual envelopes too."""
    if isinstance(body, dict):
        for key in ("data", "results", "votes", "items", "models"):
            if isinstance(body.get(key), list):
                body = body[key]
                break
    if not isinstance(body, list):
        return []
    return [row for row in body if isinstance(row, dict)]


def _record_inference_id(record: dict) -> Optional[str]:
    """The gj_inference_id a vote is about. Where it sits in a vote record is
    not documented, so look in each place it plausibly is."""
    for key in ("gj_inference_id", "inference_id", "trace_id"):
        value = record.get(key)
        if value:
            return str(value)
    trace = record.get("trace")
    if isinstance(trace, dict):
        return _record_inference_id(trace)
    return None


def _record_verdict(record: dict) -> Optional[str]:
    value = record.get("verdict", record.get("vote"))
    if isinstance(value, bool):
        return "pass" if value else "flag"
    text = str(value or "").strip().lower()
    if text in ("pass", "passed", "good", "true", "approve", "approved"):
        return "pass"
    if text in ("flag", "flagged", "bad", "false", "fail", "failed"):
        return "flag"
    return None


def _record_time(record: dict) -> Optional[datetime.datetime]:
    raw = record.get("timestamp") or record.get("created_at")
    if not raw:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime.timezone.utc)
    return parsed


def _external_key(record: dict, inference_id: str) -> str:
    vote_id = record.get("id") or record.get("vote_id")
    if vote_id:
        return f"id:{vote_id}"[:128]
    basis = "|".join(
        [
            inference_id,
            str(record.get("evaluator_id") or ""),
            str(record.get("timestamp") or record.get("created_at") or ""),
            str(record.get("verdict") or ""),
        ]
    )
    return "sha256:" + hashlib.sha256(basis.encode()).hexdigest()


def _sendable_candidates():
    """Active drafts of synthetic Arena appeal cases. The source check is
    the privacy line: nothing that is not synthetic is ever sent."""
    return ChooserCandidate.objects.filter(
        is_active=True,
        task__task_type="appeal",
        task__source="synthetic",
        task__status__in=SENDABLE_STATUSES,
    )


def _retriable():
    return HumanJudgeSubmission.objects.filter(
        Q(status="PENDING") | Q(status="FAILED", attempts__lt=MAX_ATTEMPTS),
        # Checked again at send time: a case can be disabled after queuing.
        candidate__in=_sendable_candidates(),
    )


async def _queue_unsent(limit: int) -> int:
    """Make submission rows for the newest cases' unsent drafts, whole cases
    at a time, until ``limit`` drafts are waiting to go. Drafts still waiting
    from earlier rounds (HumanJudge down, say) count, so an outage does not
    queue the whole backlog."""
    room = limit - await _retriable().acount()
    if room <= 0:
        return 0
    # Every case has at least one unsent draft, so ``room`` cases is plenty.
    task_ids = [
        task_id
        async for task_id in _sendable_candidates()
        .filter(humanjudge_submission__isnull=True)
        .order_by("-task__created_at")
        .values_list("task_id", flat=True)
        .distinct()[:room]
    ]
    queued = 0
    for task_id in task_ids:
        if queued >= room:
            break
        async for candidate in _sendable_candidates().filter(
            task_id=task_id, humanjudge_submission__isnull=True
        ):
            _, created = await HumanJudgeSubmission.objects.aget_or_create(
                candidate=candidate,
                defaults={"task_id": task_id, "inference_id": new_inference_id()},
            )
            queued += int(created)
    return queued


async def submit_pending(client: HumanJudgeClient, limit: int) -> int:
    """Send up to ``limit`` unsent drafts; returns how many went."""
    await _queue_unsent(limit)
    pending = (
        _retriable()
        .select_related("candidate", "task")
        .order_by("-task__created_at", "candidate__candidate_index")[:limit]
    )
    sent = 0
    async for submission in pending:
        candidate = submission.candidate
        submission.attempts += 1
        try:
            await client.ingest(
                submission.inference_id,
                render_case(submission.task.context_json),
                truncate_at_boundary(candidate.content, _DRAFT_CHARS),
                # Opaque ids only, to match verdicts back; never the model.
                {"task_id": submission.task_id, "candidate_id": candidate.id},
            )
        except HumanJudgeAuthError:
            # Spend no attempt on a refused key: nothing else will go either.
            submission.attempts -= 1
            raise
        except (HumanJudgeError, httpx.HTTPError) as e:
            submission.status = "FAILED"
            submission.last_error = f"{type(e).__name__}: {e}"[:200]
            await submission.asave(update_fields=["status", "attempts", "last_error"])
            continue
        submission.status = "SENT"
        submission.sent_at = timezone.now()
        submission.last_error = ""
        await submission.asave(
            update_fields=["status", "attempts", "last_error", "sent_at"]
        )
        sent += 1
    return sent


async def fetch_verdicts(client: HumanJudgeClient) -> set[int]:
    """Store the verdicts not stored yet; returns the ids of the cases that
    got one."""
    model_id = await client.model_id()
    if not model_id:
        return set()
    newest = (
        await HumanJudgeVerdict.objects.exclude(voted_at=None)
        .order_by("-voted_at")
        .values_list("voted_at", flat=True)
        .afirst()
    )
    since = (newest - _REFETCH_OVERLAP).date() if newest else None
    records = await client.votes(model_id, since)
    by_inference: dict[str, list[dict]] = {}
    for record in records:
        inference_id = _record_inference_id(record)
        if inference_id:
            by_inference.setdefault(inference_id, []).append(record)
    submissions = {
        s.inference_id: s
        async for s in HumanJudgeSubmission.objects.filter(
            inference_id__in=list(by_inference)
        )
    }
    touched: set[int] = set()
    unmatched = skipped = 0
    for inference_id, rows in by_inference.items():
        submission = submissions.get(inference_id)
        if submission is None:
            unmatched += len(rows)
            continue
        for record in rows:
            verdict = _record_verdict(record)
            if verdict is None:
                skipped += 1
                continue
            _, created = await HumanJudgeVerdict.objects.aget_or_create(
                external_key=_external_key(record, inference_id),
                defaults={
                    "submission": submission,
                    "task_id": submission.task_id,
                    "candidate_id": submission.candidate_id,
                    "verdict": verdict,
                    "flag_category": str(record.get("flag_category") or "")[:100],
                    "feedback": str(record.get("feedback") or ""),
                    "evaluator_id": str(record.get("evaluator_id") or "")[:128],
                    "voted_at": _record_time(record),
                    "raw": record,
                },
            )
            if created:
                touched.add(submission.task_id)
    if unmatched or skipped:
        logger.info(
            f"HumanJudge: {unmatched} vote(s) matched no Arena draft and "
            f"{skipped} had no pass/flag verdict; left unstored"
        )
    unkeyed = len(records) - sum(len(rows) for rows in by_inference.values())
    if unkeyed:
        logger.warning(
            f"HumanJudge: {unkeyed} vote(s) carried no inference id to match"
        )
    return touched


@dataclass
class _Tally:
    passes: int = 0
    total: int = 0

    @property
    def score(self) -> float:
        # Laplace-smoothed, so three passes out of three beat one of one.
        return (self.passes + 1) / (self.total + 2)


async def update_pick(task: ChooserTask) -> Optional[dict[str, Any]]:
    """Record HumanJudge's pick in the task's scoring_json, or drop it while
    a draft lacks verdicts or the best are tied. Returns the pick."""
    candidates = [
        c
        async for c in ChooserCandidate.objects.filter(
            task=task, is_active=True
        ).order_by("candidate_index")
    ]
    tallies = {c.id: _Tally() for c in candidates}
    rows = (
        HumanJudgeVerdict.objects.filter(task=task, candidate_id__in=list(tallies))
        .values("candidate_id")
        .annotate(total=Count("id"), passes=Count("id", filter=Q(verdict="pass")))
    )
    async for row in rows:
        tallies[row["candidate_id"]] = _Tally(row["passes"], row["total"])
    minimum = max(1, int(getattr(settings, "CHOOSER_HUMANJUDGE_MIN_VERDICTS", 1)))
    pick: Optional[dict[str, Any]] = None
    if len(candidates) >= 2 and all(t.total >= minimum for t in tallies.values()):
        scores = [tallies[c.id].score for c in candidates]
        if scores.count(max(scores)) == 1:
            pick = _pick(candidates, scores, SCORER)
            if pick is not None:
                winner = tallies[pick["candidate_id"]]
                pick["passes"] = winner.passes
                pick["verdicts"] = sum(t.total for t in tallies.values())
    scoring = dict(task.scoring_json or {})
    if pick is None:
        scoring.pop(PICK_KEY, None)
    else:
        scoring[PICK_KEY] = pick
    if scoring != (task.scoring_json or {}):
        task.scoring_json = scoring
        await task.asave(update_fields=["scoring_json", "updated_at"])
    return pick


async def update_picks(task_ids: Iterable[int]) -> int:
    picked = 0
    async for task in ChooserTask.objects.filter(id__in=list(task_ids)):
        picked += int(await update_pick(task) is not None)
    return picked


async def sync(
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Optional[dict[str, int]]:
    """One round: send, fetch, pick. None when HumanJudge is off. A refused
    key stops the round with an error logged; other failures propagate to
    the caller, which treats the round as best-effort."""
    if not enabled():
        return None
    limit = max(0, int(getattr(settings, "CHOOSER_HUMANJUDGE_SUBMIT_BATCH", 20)))
    async with HumanJudgeClient(transport=transport) as client:
        try:
            sent = await submit_pending(client, limit)
            touched = await fetch_verdicts(client)
        except HumanJudgeAuthError as e:
            logger.error(f"HumanJudge refused the key ({e}); check HUMANJUDGE_API_KEY")
            return {"sent": 0, "cases_with_new_verdicts": 0, "picks": 0}
    picks = await update_picks(touched)
    summary = {"sent": sent, "cases_with_new_verdicts": len(touched), "picks": picks}
    if sent or touched:
        logger.info(f"HumanJudge sync: {summary}")
    return summary

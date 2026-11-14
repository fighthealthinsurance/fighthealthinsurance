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

Staff drive it from the admin, so a person decides what goes out and when:
"Upload to HumanJudge" on the Chooser Task list sends the selected cases'
drafts (``upload_tasks``), and "Download verdicts from HumanJudge" on the
HumanJudge Verdict list fetches the new verdicts
(``GET /api/v1/models/{id}/evaluations?detail=votes``) and updates the
picks of the cases they touch (``download_verdicts``). Nothing runs on a timer. It is off
unless ``HUMANJUDGE_API_KEY`` is set (and ``CHOOSER_HUMANJUDGE`` is not
false).
"""

import datetime
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import httpx
from django.conf import settings
from django.db.models import Case, Count, Exists, IntegerField, OuterRef, Q, Value, When
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
# Cases one upload may send. Each draft is its own request, so the admin
# runs up to INLINE_UPLOAD_LIMIT cases while the page waits and anything
# bigger in a background thread (see start_upload).
INLINE_UPLOAD_LIMIT = 25
MAX_TASKS_PER_UPLOAD = 300
_HTTP_TIMEOUT_SECONDS = 20.0
# HumanJudge's page cap (the SDK's PAGE_SIZE), and how many pages one download reads.
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
            # Each download starts again at the same date and offset, so
            # votes past the cap in one window are not reached by
            # downloading again; at the Arena's volume (a few drafts per
            # case, volunteer reviewers) that is far off.
            logger.error(
                f"HumanJudge: stopped reading votes after {_MAX_PAGES} pages "
                f"({_MAX_PAGES * _PAGE_SIZE} votes); later ones in this window "
                "are not read. Raise _MAX_PAGES"
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


def sendable_tasks():
    """Synthetic Arena appeal cases that have reached voters. The source
    check is the privacy line: nothing that is not synthetic is ever sent."""
    return ChooserTask.objects.filter(
        task_type="appeal", source="synthetic", status__in=SENDABLE_STATUSES
    )


@dataclass
class UploadResult:
    sent: int = 0
    failed: int = 0
    already_sent: int = 0
    # Selected cases that may not go: not synthetic, not appeals, or never
    # shown to voters.
    not_sendable: int = 0


async def _send(client: HumanJudgeClient, submission: HumanJudgeSubmission) -> bool:
    """Send one draft; returns whether it went. Two uploads can hold the
    same draft (a second click while a background upload runs): sending it
    twice is harmless, since HumanJudge de-duplicates on the inference id,
    but a failure must never overwrite another upload's SENT, so a draft
    already SENT is skipped and FAILED is written only over a row not SENT."""
    if await HumanJudgeSubmission.objects.filter(
        pk=submission.pk, status="SENT"
    ).aexists():
        return True
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
        # Nothing else will go either; the caller says so. No attempt spent.
        raise
    except (HumanJudgeError, httpx.HTTPError) as e:
        submission.status = "FAILED"
        submission.last_error = f"{type(e).__name__}: {e}"[:200]
        await HumanJudgeSubmission.objects.filter(pk=submission.pk).exclude(
            status="SENT"
        ).aupdate(
            status="FAILED",
            attempts=submission.attempts,
            last_error=submission.last_error,
        )
        return False
    submission.status = "SENT"
    submission.sent_at = timezone.now()
    submission.last_error = ""
    await submission.asave(
        update_fields=["status", "attempts", "last_error", "sent_at"]
    )
    return True


def unsent_task_ids(limit: int) -> list[int]:
    """Up to ``limit`` sendable cases with an active draft not yet sent:
    clearly undecided ones first (the Arena's voters split on them, so a
    second opinion helps most; see chooser_settlement), then the newest."""
    unsent_draft = ChooserCandidate.objects.filter(
        task=OuterRef("pk"), is_active=True
    ).exclude(humanjudge_submission__status="SENT")
    return list(
        sendable_tasks()
        .filter(Exists(unsent_draft))
        .annotate(
            undecided_first=Case(
                When(settled_reason="undecided", then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("undecided_first", "-created_at", "-id")
        .values_list("id", flat=True)[: max(0, limit)]
    )


def start_upload(task_ids: list[int]) -> Optional[UploadResult]:
    """Upload these cases from a sync caller (the admin): while the caller
    waits when there are at most INLINE_UPLOAD_LIMIT of them, returning the
    result; otherwise in a background thread, returning None at once. Checks
    first, so a refusal (not configured, too many) is raised to the caller
    either way. The thread's progress shows as HumanJudgeSubmission rows."""
    from asgiref.sync import async_to_sync

    from fighthealthinsurance.utils import run_in_registered_daemon_thread

    ids = list(dict.fromkeys(task_ids))
    _check_upload(ids)
    if len(ids) <= INLINE_UPLOAD_LIMIT:
        return async_to_sync(upload_tasks)(ids)
    logger.info(f"HumanJudge upload of {len(ids)} cases started in the background")
    run_in_registered_daemon_thread(async_to_sync(_upload_in_background), ids)
    return None


async def _upload_in_background(task_ids: list[int]) -> None:
    """upload_tasks for start_upload's thread, where no page is waiting to
    show a refused key: the drafts this upload had not sent yet are marked
    FAILED with the reason, so HumanJudge Submissions shows why it stopped
    (and "Upload again" retries them once the key is fixed)."""
    try:
        await upload_tasks(task_ids)
    except HumanJudgeAuthError as e:
        logger.error(f"HumanJudge background upload stopped: the key was refused ({e})")
        await HumanJudgeSubmission.objects.filter(task_id__in=task_ids).exclude(
            status="SENT"
        ).aupdate(
            status="FAILED",
            last_error=f"Upload stopped: HumanJudge refused the key ({e})"[:200],
        )


def _check_upload(task_ids: list[int]) -> None:
    if not enabled():
        raise HumanJudgeError("HumanJudge is not configured (HUMANJUDGE_API_KEY)")
    if len(task_ids) > MAX_TASKS_PER_UPLOAD:
        raise HumanJudgeError(
            f"Upload at most {MAX_TASKS_PER_UPLOAD} cases at a time "
            f"({len(task_ids)} selected)"
        )


async def upload_tasks(
    task_ids: Iterable[int],
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> UploadResult:
    """Send the active drafts of these cases that have not gone yet (failed
    ones again). Raises HumanJudgeError when HumanJudge is not configured,
    HumanJudgeAuthError when it refuses the key."""
    wanted = set(task_ids)
    _check_upload(list(wanted))
    sendable = [
        task_id
        async for task_id in sendable_tasks()
        .filter(id__in=wanted)
        .values_list("id", flat=True)
    ]
    result = UploadResult(not_sendable=len(wanted) - len(sendable))
    async for candidate in ChooserCandidate.objects.filter(
        task_id__in=sendable, is_active=True
    ):
        await HumanJudgeSubmission.objects.aget_or_create(
            candidate=candidate,
            defaults={"task_id": candidate.task_id, "inference_id": new_inference_id()},
        )
    submissions = [
        s
        async for s in HumanJudgeSubmission.objects.filter(
            task_id__in=sendable, candidate__is_active=True
        )
        .select_related("candidate", "task")
        .order_by("task_id", "candidate__candidate_index")
    ]
    async with HumanJudgeClient(transport=transport) as client:
        for submission in submissions:
            if submission.status == "SENT":
                result.already_sent += 1
            elif await _send(client, submission):
                result.sent += 1
            else:
                result.failed += 1
    if result.sent or result.failed:
        logger.info(f"HumanJudge upload: {result}")
    return result


@dataclass
class DownloadResult:
    new_verdicts: int = 0
    # The cases that got a new verdict.
    task_ids: set[int] = field(default_factory=set)
    # Every case with a verdict in what was read, new or already stored:
    # their picks are recomputed, so a download cut short after storing
    # verdicts but before updating picks is repaired by the next one.
    matched_task_ids: set[int] = field(default_factory=set)
    # Cases the new verdicts settled on a pick for.
    picks: int = 0
    # Votes on traces that are not Arena drafts we sent, or without a
    # pass/flag verdict; left unstored.
    unmatched: int = 0

    @property
    def cases(self) -> int:
        return len(self.task_ids)


async def fetch_verdicts(client: HumanJudgeClient) -> DownloadResult:
    """Store the verdicts not stored yet (picks are not updated here)."""
    result = DownloadResult()
    model_id = await client.model_id()
    if not model_id:
        raise HumanJudgeError("No HumanJudge model to read: set HUMANJUDGE_MODEL_ID")
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
            result.matched_task_ids.add(submission.task_id)
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
                result.task_ids.add(submission.task_id)
                result.new_verdicts += 1
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
    result.unmatched = unmatched + skipped + unkeyed
    return result


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


async def update_picks(task_ids: Iterable[int]) -> set[int]:
    """Update these cases' picks; returns the ids of those with a pick."""
    picked: set[int] = set()
    async for task in ChooserTask.objects.filter(id__in=list(task_ids)):
        if await update_pick(task) is not None:
            picked.add(task.id)
    return picked


async def download_verdicts(
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> DownloadResult:
    """Fetch HumanJudge's new verdicts and update the picks of the cases
    they touch. Raises as upload_tasks does."""
    if not enabled():
        raise HumanJudgeError("HumanJudge is not configured (HUMANJUDGE_API_KEY)")
    async with HumanJudgeClient(transport=transport) as client:
        result = await fetch_verdicts(client)
    picked = await update_picks(result.matched_task_ids)
    result.picks = len(picked & result.task_ids)
    if result.new_verdicts:
        logger.info(f"HumanJudge download: {result}")
    return result

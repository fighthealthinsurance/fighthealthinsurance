"""Paid Appeal Arena reviewers, hired through RentAHuman (rentahuman.ai).

The Arena only ever shows synthetic cases, so a stranger can judge them
without seeing anyone's health information. A batch of paid reviewers is one
RentAHuman bounty (``PaidReviewBounty``):

1. ``post_paid_review_bounty`` posts the bounty, with a join link to
   ``/chooser/paid/<join_token>/`` in its description.
2. A worker opens the link. ``join`` ties their browser session to a
   ``PaidReviewSession`` and sends them to the Arena, where they judge as
   anyone does.
3. Each vote that session casts is tagged (``ChooserVote.paid_review``).
   A vote counts toward the run when it clears the quality floor
   (``vote_counts``). After ``reviews_required`` of them the session is
   given a completion code, which the worker pastes into their submission.
4. RentAHuman tells us of the submission (the webhook, or the
   ``sync_paid_reviews`` poll). ``process_submission`` finds the code in it,
   claims the run for that submission, and approves it, or asks for a redo
   with the reason.

Approving a submission moves no money on RentAHuman: the account owner
releases the escrow there, as the platform asks agents to leave to them.
"""

import hashlib
import hmac
import json
import re
import secrets
import time
import typing
import uuid
from decimal import Decimal

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
)
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View
from loguru import logger

from fighthealthinsurance.models import (
    ChooserVote,
    PaidReviewBounty,
    PaidReviewSession,
)

# The Django session key that holds this browser's PaidReviewSession id.
SESSION_KEY = "paid_review_session_id"

# Codes avoid the characters people misread (0/O, 1/I/L).
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"FHI-([A-HJKMNP-Z2-9]{4})-([A-HJKMNP-Z2-9]{4})", re.IGNORECASE)

# Minutes a careful review takes, for the bounty's estimate.
MINUTES_PER_REVIEW = 3
# What RentAHuman adds on top of the worker's pay.
RAH_FEE_RATE = Decimal("0.18")
REQUEST_TIMEOUT_SECONDS = 8
# How old a webhook's timestamp may be (RentAHuman's documented window).
WEBHOOK_TOLERANCE_SECONDS = 300


def new_join_token() -> str:
    return secrets.token_urlsafe(24)


def new_completion_code() -> str:
    chars = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
    return f"FHI-{chars[:4]}-{chars[4:]}"


def join_url(bounty: PaidReviewBounty) -> str:
    origin = settings.PAID_REVIEW_SITE_ORIGIN.rstrip("/")
    return f"{origin}/chooser/paid/{bounty.join_token}/"


def total_cost(price: Decimal, spots: int) -> Decimal:
    """What a bounty costs us all in: the workers' pay plus RentAHuman's fee."""
    return (price * spots * (1 + RAH_FEE_RATE)).quantize(Decimal("0.01"))


# --- Judging in the Arena -------------------------------------------------


def session_for(request) -> typing.Optional[PaidReviewSession]:
    """The paid run this browser session is in, if its bounty is open."""
    run_id = request.session.get(SESSION_KEY)
    if not run_id:
        return None
    return (
        PaidReviewSession.objects.select_related("bounty")
        .filter(id=run_id, bounty__status="open")
        .first()
    )


def join(request, bounty: PaidReviewBounty) -> PaidReviewSession:
    """Put this browser session in a paid run for ``bounty``: the run it is
    already in for that bounty, or a new one."""
    current = session_for(request)
    if current is not None and current.bounty_id == bounty.id:
        return current
    if not request.session.session_key:
        request.session.create()
    run = PaidReviewSession.objects.create(
        bounty=bounty, session_key=request.session.session_key
    )
    request.session[SESSION_KEY] = run.id
    return run


def vote_counts(vote: ChooserVote) -> typing.Tuple[bool, str]:
    """Whether a paid vote counts toward the run, and if not, why (shown to
    the worker). The floor stops click-through, not honest disagreement:
    any outcome counts, as long as the case was read and the pick explained."""
    seconds = vote.seconds_to_vote
    if seconds is None or seconds < settings.PAID_REVIEW_MIN_SECONDS:
        return (
            False,
            f"That one was too quick to count: take at least "
            f"{settings.PAID_REVIEW_MIN_SECONDS} seconds to read the case and the drafts.",
        )
    if len(vote.rationale.strip()) < settings.PAID_REVIEW_MIN_RATIONALE_CHARS:
        return (
            False,
            "That one did not count: add a one-sentence reason for your pick in the “Why” box.",
        )
    if (vote.review_signals or {}).get("webdriver") is True:
        return False, "That one did not count: automated browsers are not paid."
    return True, ""


def counted_votes(run: PaidReviewSession) -> int:
    return sum(1 for vote in run.votes.all() if vote_counts(vote)[0])


def progress(
    run: PaidReviewSession, last_vote: typing.Optional[ChooserVote] = None
) -> dict:
    """The run's progress as the Arena shows it, issuing the completion code
    once enough votes count. ``last_vote`` adds whether it counted."""
    counted = counted_votes(run)
    required = run.bounty.reviews_required
    if counted >= required and not run.completion_code:
        _issue_code(run)
    result: dict = {
        "counted": min(counted, required),
        "required": required,
        "completion_code": run.completion_code or "",
    }
    if last_vote is not None:
        ok, note = vote_counts(last_vote)
        result["last_vote_counted"] = ok
        result["note"] = note
    return result


def _issue_code(run: PaidReviewSession) -> None:
    for _ in range(5):
        code = new_completion_code()
        try:
            with transaction.atomic():
                updated = PaidReviewSession.objects.filter(
                    id=run.id, completion_code__isnull=True
                ).update(
                    completion_code=code,
                    code_issued_at=timezone.now(),
                    status="code_issued",
                )
        except IntegrityError:
            continue  # The code was taken; draw another.
        run.refresh_from_db()
        if updated:
            logger.info(f"Paid review {run.id}: completion code issued")
        return
    logger.error(f"Paid review {run.id}: could not draw a free completion code")


# --- RentAHuman's API -----------------------------------------------------


class RentAHumanError(Exception):
    pass


class RentAHumanClient:
    """The few RentAHuman calls a paid bounty needs (rentahuman.ai/api-docs).
    Prices are in dollars. Writes carry an idempotency key."""

    def __init__(
        self,
        api_key: typing.Optional[str] = None,
        base: typing.Optional[str] = None,
        session: typing.Optional[requests.Session] = None,
    ):
        self.api_key = api_key if api_key is not None else settings.RENTAHUMAN_API_KEY
        self.base = (base or settings.RENTAHUMAN_API_BASE).rstrip("/")
        self.http = session or requests.Session()
        if not self.api_key:
            raise RentAHumanError("RENTAHUMAN_API_KEY is not set")

    def _call(
        self,
        method: str,
        path: str,
        body: typing.Optional[dict] = None,
        params: typing.Optional[dict] = None,
        idempotency_key: typing.Optional[str] = None,
    ) -> typing.Any:
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = self.http.request(
            method,
            f"{self.base}{path}",
            json=body,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            raise RentAHumanError(
                f"{method} {path} answered {response.status_code}: {response.text[:300]}"
            )
        return response.json() if response.content else {}

    def create_bounty(self, payload: dict) -> dict:
        key = payload.get("idempotencyKey") or str(uuid.uuid4())
        return _as_dict(
            self._call(
                "POST",
                "/bounties",
                {**payload, "idempotencyKey": key},
                idempotency_key=key,
            )
        )

    def list_submissions(self, bounty_id: str, status: str = "pending_review") -> list:
        found = self._call(
            "GET", f"/bounties/{bounty_id}/submissions", params={"status": status}
        )
        return _as_list(found, "submissions")

    def get_submission(self, bounty_id: str, submission_id: str) -> dict:
        found = self._call("GET", f"/bounties/{bounty_id}/submissions/{submission_id}")
        submission = _as_dict(found).get("submission")
        if isinstance(submission, dict):
            return submission
        return _as_dict(found)

    def review_submission(
        self, bounty_id: str, submission_id: str, action: str, response: str = ""
    ) -> None:
        body: dict = {"action": action}
        if response:
            body["response"] = response
        self._call(
            "PATCH",
            f"/bounties/{bounty_id}/submissions/{submission_id}",
            body,
            idempotency_key=f"fhi-{submission_id}-{action}",
        )


def _as_list(found: typing.Any, name: str) -> list:
    """A list endpoint's items, whether it answered with a bare list or
    wrapped them (the API docs do not pin the envelope down)."""
    if isinstance(found, list):
        return found
    if isinstance(found, dict):
        for key in (name, "data", "items", "results"):
            items = found.get(key)
            if isinstance(items, list):
                return items
    return []


def _as_dict(found: typing.Any) -> dict:
    return found if isinstance(found, dict) else {}


def find_id(found: typing.Any, *names: str) -> str:
    """The first of ``names`` in a response, looking one wrapper deep."""
    if not isinstance(found, dict):
        return ""
    for name in names:
        if found.get(name) not in (None, ""):
            return str(found[name])
    for wrapper in ("bounty", "submission", "data", "object"):
        inner = found.get(wrapper)
        if isinstance(inner, dict):
            nested = find_id(inner, *names)
            if nested:
                return nested
    return ""


# --- Posting a bounty -----------------------------------------------------


def bounty_payload(bounty: PaidReviewBounty, dry_run: bool) -> dict:
    minutes = bounty.reviews_required * MINUTES_PER_REVIEW
    description = (
        "Help a nonprofit that helps patients appeal health insurance denials "
        "decide which AI-written appeal letter is best.\n\n"
        f"Open {join_url(bounty)} and judge {bounty.reviews_required} cases in "
        "the Appeal Arena. Each case is fictional (no real patient data): read "
        "the denial and the case facts, read the AI drafts, pick the one most "
        "likely to win the appeal (or a tie, or 'these are all bad'), flag any "
        "draft that makes things up, and write one sentence on why.\n\n"
        f"A review only counts if you spend at least "
        f"{settings.PAID_REVIEW_MIN_SECONDS} seconds on it and give a reason. "
        f"After {bounty.reviews_required} counted reviews the page shows a "
        "completion code like FHI-ABCD-EFGH: paste it as your submission.\n\n"
        "No medical or legal background is needed: judge like an insurance "
        "reviewer who has to say yes or no. Use one browser for the whole "
        "run; the code is shown in the browser that did the reviews."
    )
    return {
        "title": bounty.title,
        "description": description,
        "completionCriteria": (
            f"Submit the completion code shown after {bounty.reviews_required} "
            "counted reviews. Codes are checked automatically and each works once."
        ),
        "evidenceTypes": ["text"],
        "price": float(bounty.price_dollars),
        "priceType": "fixed",
        "estimatedHours": round(minutes / 60, 2),
        "category": "computer-gigs",
        "location": {"isRemoteAllowed": True},
        "spotsAvailable": bounty.spots,
        "autoAccept": True,
        "dryRun": dry_run,
        "idempotencyKey": f"fhi-paid-review-{bounty.join_token[:16]}",
    }


# --- Checking submissions -------------------------------------------------


def codes_in(submission: typing.Any) -> typing.List[str]:
    """Every completion code anywhere in a submission, normalized. Searched
    across the whole object, since the field the text lands in is not ours."""
    text = json.dumps(submission, ensure_ascii=False)
    return list(
        dict.fromkeys(f"FHI-{a.upper()}-{b.upper()}" for a, b in CODE_RE.findall(text))
    )


def process_submission(
    bounty: PaidReviewBounty,
    submission: dict,
    client: typing.Optional[RentAHumanClient],
) -> str:
    """Check one RentAHuman submission against the bounty's runs and answer
    it: approve a fresh, valid code; ask for a redo otherwise. Returns what
    happened (for logs and the sync command). With no client the verdict is
    recorded here but nothing is sent."""
    submission_id = find_id(submission, "id", "submissionId", "_id")
    if not submission_id:
        return "skipped: no submission id"

    already = PaidReviewSession.objects.filter(rah_submission_id=submission_id).first()
    if already is not None:
        if already.status == "verified" and client is not None:
            _approve(client, bounty, submission_id, already)
        return f"already handled ({already.status})"

    redo = ""
    claimed: typing.Optional[PaidReviewSession] = None
    codes = codes_in(submission)
    if not codes:
        redo = (
            f"We could not find a completion code. Finish "
            f"{bounty.reviews_required} reviews at {join_url(bounty)} and paste "
            "the code it shows (it looks like FHI-ABCD-EFGH)."
        )
    else:
        for code in codes:
            with transaction.atomic():
                updated = PaidReviewSession.objects.filter(
                    bounty=bounty,
                    completion_code=code,
                    rah_submission_id__isnull=True,
                ).update(
                    rah_submission_id=submission_id,
                    claimed_at=timezone.now(),
                    status="verified",
                )
            if updated:
                claimed = PaidReviewSession.objects.get(completion_code=code)
                break
        if claimed is None:
            known = PaidReviewSession.objects.filter(
                bounty=bounty, completion_code__in=codes
            ).exists()
            redo = (
                "That completion code was already used by another submission."
                if known
                else "That completion code is not one we issued for this task. "
                "Copy it exactly from the Arena page."
            )

    if claimed is not None:
        logger.info(f"Paid review {claimed.id}: claimed by submission {submission_id}")
        if client is not None:
            _approve(client, bounty, submission_id, claimed)
        return "approved" if claimed.status == "approved" else "verified"

    logger.info(f"Paid review submission {submission_id}: redo ({redo})")
    if client is not None:
        client.review_submission(
            bounty.rah_bounty_id, submission_id, "request_redo", redo
        )
    return "redo requested"


def _approve(
    client: RentAHumanClient,
    bounty: PaidReviewBounty,
    submission_id: str,
    run: PaidReviewSession,
) -> None:
    client.review_submission(
        bounty.rah_bounty_id,
        submission_id,
        "approve",
        "Thank you! Your reviews help patients get better appeal letters.",
    )
    PaidReviewSession.objects.filter(id=run.id).update(status="approved")
    run.status = "approved"


def sync_bounty(bounty: PaidReviewBounty, client: RentAHumanClient) -> dict:
    """Answer every submission waiting on review for ``bounty``."""
    outcomes: dict = {}
    for submission in client.list_submissions(bounty.rah_bounty_id):
        try:
            outcome = process_submission(bounty, submission, client)
        except RentAHumanError as e:
            logger.warning(f"Paid review bounty {bounty.id}: {e}")
            outcome = "error"
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return outcomes


# --- Webhooks -------------------------------------------------------------


def signature_ok(
    secret: str, header: str, body: bytes, now: typing.Optional[float] = None
) -> bool:
    """Check RentAHuman's ``X-RentAHuman-Signature: t=<unix>,v1=<hex>``:
    HMAC-SHA256 of ``"<t>.<raw body>"`` with the endpoint's secret, sent
    within the last five minutes."""
    if not secret or not header:
        return False
    parts: dict = {}
    for item in header.split(","):
        name, _, value = item.strip().partition("=")
        parts.setdefault(name, []).append(value)
    try:
        sent_at = int(parts.get("t", [""])[0])
    except ValueError:
        return False
    if (
        abs((now if now is not None else time.time()) - sent_at)
        > WEBHOOK_TOLERANCE_SECONDS
    ):
        return False
    expected = hmac.new(
        secret.encode(), f"{sent_at}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return any(hmac.compare_digest(expected, v) for v in parts.get("v1", []))


def handle_event(event: dict) -> str:
    """Act on one verified webhook event; only new submissions matter."""
    if event.get("type") != "submission.submitted":
        return "ignored"
    data = event.get("data") or {}
    bounty_id = find_id(data, "bountyId", "bounty_id")
    submission_id = find_id(data, "submissionId", "submission_id", "id")
    if not bounty_id or not submission_id:
        return "ignored: no bounty or submission id"
    bounty = PaidReviewBounty.objects.filter(rah_bounty_id=bounty_id).first()
    if bounty is None:
        return "ignored: not our bounty"
    client = RentAHumanClient()
    # The event says which submission; its contents come from the API.
    submission = client.get_submission(bounty_id, submission_id)
    if not find_id(submission, "id", "submissionId", "_id"):
        submission = {**submission, "id": submission_id}
    return process_submission(bounty, submission, client)


# --- Views ----------------------------------------------------------------


class PaidReviewJoinView(View):
    """``/chooser/paid/<join_token>/``: the link in a bounty. Puts this
    browser in a paid run and opens the Arena."""

    def get(self, request: HttpRequest, join_token: str) -> HttpResponse:
        bounty = PaidReviewBounty.objects.filter(join_token=join_token).first()
        if bounty is None or bounty.status != "open":
            response = render(
                request, "paid_review_closed.html", {"title": "Task closed"}, status=410
            )
            response["X-Robots-Tag"] = "noindex"
            return response
        join(request, bounty)
        return redirect("chooser")


class RentAHumanWebhookView(View):
    """RentAHuman's event deliveries (docs/webhooks): signed with the
    endpoint's secret, retried on anything but a 2xx, deduplicated on the
    event id."""

    DEDUPE_SECONDS = 2 * 24 * 60 * 60

    def post(self, request: HttpRequest) -> HttpResponse:
        secret = settings.RENTAHUMAN_WEBHOOK_SECRET
        if not secret:
            return HttpResponseForbidden("webhooks are not configured")
        header = request.headers.get("X-RentAHuman-Signature", "")
        if not signature_ok(secret, header, request.body):
            return HttpResponseForbidden("bad signature")
        try:
            event = json.loads(request.body)
        except ValueError:
            return HttpResponseBadRequest("not JSON")
        if not isinstance(event, dict):
            return HttpResponseBadRequest("not an event")
        dedupe_key = f"rah_event:{event.get('id', '')}"
        if event.get("id") and cache.get(dedupe_key):
            return HttpResponse("duplicate")
        try:
            outcome = handle_event(event)
        except (RentAHumanError, requests.RequestException) as e:
            # A 5xx makes RentAHuman try again later.
            logger.warning(f"RentAHuman event {event.get('id')}: {e}")
            return HttpResponse("retry later", status=502)
        if event.get("id"):
            cache.set(dedupe_key, 1, self.DEDUPE_SECONDS)
        logger.info(f"RentAHuman event {event.get('id')}: {outcome}")
        return HttpResponse(outcome)

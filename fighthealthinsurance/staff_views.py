import csv
import datetime
import json
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db import connection
from django.db.models import Avg, Count, F, Max, Min, QuerySet
from django.db.models.functions import Lower
from django.http import HttpResponse, HttpResponseBase, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.db.models import Q
from django.utils import timezone
from django.views import View, generic

import ray
import requests
from loguru import logger

from fighthealthinsurance import common_view_logic, forms as core_forms
from fighthealthinsurance.common_view_logic import schedule_follow_ups
from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.followup_emails import (
    FollowUpEmailSender,
    ThankyouEmailSender,
)
from fighthealthinsurance.forms import FollowUpTestForm
from fighthealthinsurance.helpers.fax_helpers import SendFaxHelper
from fighthealthinsurance.base_actor_ref import ray_cluster_available
from fighthealthinsurance.mailing_list_actor_ref import mailing_list_actor_ref
from fighthealthinsurance.models import (
    ChooserCandidate,
    ChooserVote,
    Denial,
    FollowUpSched,
    InterestedProfessional,
    MailingListSubscriber,
    ModelBackendHealthCheckResult,
    ProfessionalDomainRelation,
    ProfessionalUser,
    ProposedAppeal,
    ScheduledEmail,
    UserDomain,
)
from fighthealthinsurance.email_utils import is_sendable_email
from fighthealthinsurance.business_hours import describe_send_window
from fighthealthinsurance.ml import letter_quality, model_query
from fighthealthinsurance.ml.model_identity import (
    LEGACY_UNATTRIBUTED_LABEL,
    normalize_model_label,
)
from fighthealthinsurance.proconnector import (
    PROCONNECTOR_INTRO_SUBJECT,
    address_max_length,
    address_problem,
    build_intro_letter_blocks,
    build_letter_document_title,
    build_search_links,
    claim_email_for_send,
    clean_address,
    cofactor_cc_problem,
    default_intro_cc_recipients,
    generate_intro_email,
    get_cofactor_cc_email,
    get_next_interested_professional,
    get_professional_cc_email,
    intro_wording_problem,
    mailable_interested_professionals,
    mark_email_queued,
    mark_email_sent,
    mark_email_skipped,
    release_email_claim,
    non_spam_interested_professionals,
    queue_proconnector_intro_email,
    quick_intro_block_reason,
    save_address,
    subject_wording_problem,
    remaining_interested_professionals_count,
    send_proconnector_intro_email,
    send_proconnector_test_email,
)
from fighthealthinsurance.type_utils import User
from fighthealthinsurance.utils import mask_email_for_logging

# Sort key standing in for a workflow row whose start time is missing, so such
# a row lands last instead of raising. Timezone-aware because everything it is
# compared against (the Temporal SDK's timestamps) is.
_UNDATED = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


class AdminDeleteDataView(generic.FormView):
    """Staff view to delete all data for a user by email address.

    Used when handling data deletion requests received via email.
    Skips the token confirmation flow since staff authentication
    serves as authorization.
    """

    template_name = "pro_domain_task.html"
    form_class = core_forms.DeleteDataForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Delete User Data"
        context["heading"] = "Delete User Data"
        context["description"] = (
            "Enter the email address of the user whose data should be deleted. "
            "This will permanently remove all associated denials, appeals, "
            "follow-ups, chats, and mailing list entries."
        )
        context["button_text"] = "Delete Data"
        return context

    def form_valid(self, form):
        email = form.cleaned_data["email"]
        masked = mask_email_for_logging(email)
        try:
            with transaction.atomic():
                RemoveDataHelper.remove_data_for_email(email)
        except Exception:
            logger.opt(exception=True).error(
                f"Staff user {self.request.user.username} failed to delete data for {masked}"
            )
            return HttpResponse(
                f"Error deleting data for {masked}. Please try again.",
                status=500,
            )
        logger.info(
            f"Staff user {self.request.user.username} deleted data for {masked}"
        )
        return HttpResponse(f"All data for {masked} has been deleted.")


class StaffDashboardView(generic.TemplateView):
    """Staff dashboard with links to all staff views."""

    template_name = "staff_dashboard.html"


def _lifetime_counters() -> Dict[str, Any]:
    """The lifetime numbers: counters that only go up (lifetime_counters.py)
    plus the running deletion count. Raises on failure; the caller renders
    "unavailable" rather than a fabricated zero."""
    from fighthealthinsurance.models import DataRemovalTotals, LifetimeCounters

    counters = LifetimeCounters.objects.filter(pk=LifetimeCounters.SINGLETON_ID).first()
    removals = DataRemovalTotals.objects.filter(
        pk=DataRemovalTotals.SINGLETON_ID
    ).first()
    return {
        "appeals_generated": counters.appeals_generated if counters else 0,
        "people_with_draft_lifetime": counters.people_with_draft if counters else 0,
        "faxes_sent_lifetime": counters.faxes_sent if counters else 0,
        "faxes_delivered_lifetime": counters.faxes_delivered if counters else 0,
        "counting_since": counters.since if counters else None,
        "removal_requests": removals.requests if removals else 0,
        "removed_denials": removals.denials if removals else 0,
        "removals_since": removals.since if removals else None,
    }


def _sequence_value(model, column: str) -> Optional[int]:
    """The id sequence's current value: how many rows were EVER created,
    which deletion cannot lower (Melanie, 2026-09-11). Not Max(id) of the
    remaining rows, which drops the moment the newest row is deleted
    (review). Postgres and sqlite both keep such a counter; anything else
    gets None and the page says so. A rolled-back insert consumes an id, so
    this is a ceiling by a hair."""
    table = model._meta.db_table
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute("SELECT pg_get_serial_sequence(%s, %s)", [table, column])
            seq = cursor.fetchone()[0]
            if not seq:
                return None
            cursor.execute(f"SELECT last_value, is_called FROM {seq}")
            last_value, is_called = cursor.fetchone()
            return int(last_value) if is_called else 0
        if connection.vendor == "sqlite":
            cursor.execute("SELECT seq FROM sqlite_sequence WHERE name = %s", [table])
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    return None


class AdminStatusView(generic.TemplateView):
    """Staff system-status dashboard.

    A one-stop live health view for on-call: which ML model backends are up,
    Ray polling-actor health, whether the Sonic fax backend can authenticate,
    queued/pending fax counts, and external storage reachability.

    Each subsystem is gathered independently and wrapped in its own error
    handling so a single failing check degrades to an error row instead of
    breaking the whole page. The model and Sonic checks make live network
    calls (bounded by timeouts), so this page is intentionally staff-only and
    a little slower than a cached endpoint.
    """

    template_name = "admin_status.html"

    # How many recent SendFaxWorkflow runs the Temporal panel lists.
    _RECENT_WORKFLOW_LIMIT = 10
    # Candidates pulled before sorting. Wider than the limit because the store
    # cannot order by start time for us (see _temporal_status), so a run that
    # started recently may sit well down the store's own ordering. Bounded so a
    # busy namespace cannot turn a status page into a full scan.
    _RECENT_WORKFLOW_SCAN = 100

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "System Status"
        ctx["generated_at"] = timezone.now()
        ctx["models"] = self._model_status()
        ctx["actors"] = self._actor_status()
        ctx["fax"] = self._fax_backend_status()
        ctx["fax_queue"] = self._fax_queue_status()
        ctx["temporal"] = self._temporal_status()
        # The lifetime counters are read once and feed both panels.
        counters = self._lifetime()
        ctx["fax_outcomes"] = self._fax_outcome_status(counters)
        ctx["all_time"] = self._all_time_status(counters)
        ctx["intake_funnel"] = self._intake_funnel_status()
        ctx["letter_scoring"] = self._letter_scoring_status()
        ctx["storage"] = self._storage_status()
        return ctx

    @staticmethod
    def _model_status() -> Dict[str, Any]:
        """ML model backend health: a fresh, per-backend probe plus router summary.

        Uses ``compute_model_health_details`` (a standalone check) rather than
        ``health_status.get_snapshot``. The latter, on first access, runs its
        own full refresh *and* can fire an alert email / start a background
        timer — surprising side effects to attach to rendering a status page,
        and a redundant second check pass. ``generated_at`` conveys freshness.
        """
        out: Dict[str, Any] = {"ok": True, "error": None, "details": []}
        try:
            from fighthealthinsurance.ml.health_status import (
                compute_model_health_details,
            )
            from fighthealthinsurance.ml.ml_router import ml_router

            details = compute_model_health_details()
            out["details"] = details
            out["alive"] = sum(1 for d in details if d["ok"])
            out["total"] = len(details)
            out["internal_alive"] = sum(
                1 for d in details if d["ok"] and not d["external"]
            )
            out["internal_total"] = sum(1 for d in details if not d["external"])
            out["working"] = ml_router.working()
        except Exception as e:
            logger.opt(exception=True).error("Error computing model status")
            out["ok"] = False
            out["error"] = str(e)
        return out

    @staticmethod
    def _actor_status() -> Dict[str, Any]:
        """Ray polling-actor health via the shared check_actor_health helper."""
        out: Dict[str, Any] = {
            "ok": True,
            "error": None,
            "details": [],
            "alive_actors": 0,
            "total_actors": 0,
        }
        try:
            from fighthealthinsurance.actor_health_status import check_actor_health

            out.update(check_actor_health())
        except Exception as e:
            logger.opt(exception=True).error("Error checking actor health")
            out["ok"] = False
            out["error"] = str(e)
        return out

    @staticmethod
    def _fax_backend_status() -> Dict[str, Any]:
        """Fax backend health, including a live Sonic login probe."""
        out: Dict[str, Any] = {
            "ok": True,
            "error": None,
            "backends": [],
            "sonic": {"configured": False, "active": False, "ok": False, "error": None},
        }
        try:
            from fighthealthinsurance.fax_health_status import (
                check_fax_backends_health,
            )

            out.update(check_fax_backends_health())
        except Exception as e:
            logger.opt(exception=True).error("Error checking fax backends")
            out["ok"] = False
            out["error"] = str(e)
            out["sonic"] = {
                "configured": False,
                "active": False,
                "ok": False,
                "error": str(e),
            }
        return out

    @staticmethod
    def _fax_queue_status() -> Dict[str, Any]:
        """Counts of queued / stuck / abandoned / failed faxes from FaxesToSend.

        The buckets staff act on:

        * ``ready_queued``: confirmed by the user (``should_send=True``) and
          not yet sent. The sender picks these up.
        * ``due_now``: the subset older than an hour. A non-zero value here is
          a STUCK fax -- the sender should already have taken it.
        * ``awaiting_confirmation``: consumer rows never confirmed and never
          attempted. Mostly drafts whose user never clicked send in the
          confirmation email (they accumulate for months), but the consumer
          appeal-staging path also creates paid rows before dispatching, so
          a dispatch that fails there lands here too; the label does not
          call this bucket "not actionable" (review). Telling those two apart
          needs a send-intent flag on the row: follow-up, not this change.
        * ``in_flight``: an attempt started in the last two hours that has not
          finished. Unbounded, this counter showed rows from January whose
          ``attempting_to_send_as_of`` was never cleared (2026-09-11).
        * ``stale_attempts``: an attempt started MORE than two hours ago that
          never finished: a worker died mid-send, or a row that nothing will
          ever clear. These are the actionable leftovers, kept visible on
          purpose (review): the professional path dispatches without
          ``should_send``, so a stranded send there is not "queued" either.
        * ``failures_recent``: sent in the last week and not delivered.

        * ``requested_unpicked``: a professional's fax. That path dispatches
          without ``should_send`` (fax_helpers.stage_appeal_as_fax), so a
          requested, paid send that never reached precheck would otherwise
          sit in the "never confirmed" bucket and be called not actionable
          (review). Non-zero here is a stranded send.

        ``due_now`` counts only rows the sender never touched (no attempt
        timestamp): an attempted leftover is a stale attempt, not "not
        taken", so one fax never lights two red cards. ``awaiting_confirmation``
        excludes anything ever attempted and anything professional. All the
        buckets come from ONE aggregate query so a row that moves bucket
        mid-refresh cannot be counted in two (review).
        """
        out: Dict[str, Any] = {"ok": True, "error": None}
        try:
            from fighthealthinsurance.models import FaxesToSend

            now = timezone.now()
            one_hour_ago = now - datetime.timedelta(hours=1)
            two_hours_ago = now - datetime.timedelta(hours=2)
            week_ago = now - datetime.timedelta(days=7)

            unsent = Q(sent=False)
            never_attempted = Q(attempting_to_send_as_of__isnull=True)
            live_attempt = Q(attempting_to_send_as_of__gte=two_hours_ago)
            stale_attempt = Q(attempting_to_send_as_of__lt=two_hours_ago)
            counts = FaxesToSend.objects.aggregate(
                unsent_total=Count("fax_id", filter=unsent),
                ready_queued=Count("fax_id", filter=unsent & Q(should_send=True)),
                due_now=Count(
                    "fax_id",
                    filter=unsent
                    & never_attempted
                    & Q(should_send=True, date__lt=one_hour_ago),
                ),
                awaiting_confirmation=Count(
                    "fax_id",
                    filter=unsent
                    & never_attempted
                    & Q(should_send=False, professional=False),
                ),
                requested_unpicked=Count(
                    "fax_id",
                    filter=unsent
                    & never_attempted
                    & Q(should_send=False, professional=True),
                ),
                in_flight=Count("fax_id", filter=unsent & live_attempt),
                stale_attempts=Count("fax_id", filter=unsent & stale_attempt),
                failures_recent=Count(
                    "fax_id",
                    # Recent by ATTEMPT, falling back to creation for rows
                    # never stamped: the same window the Fax delivery panel
                    # uses, so the two cards cannot disagree. Keyed on
                    # creation alone, a resend of an old fax that failed
                    # again could never appear here (review).
                    filter=Q(sent=True, fax_success=False)
                    & (
                        Q(attempting_to_send_as_of__gte=week_ago)
                        | Q(date__gte=week_ago)
                    ),
                ),
            )
            out.update(counts)
        except Exception as e:
            logger.opt(exception=True).error("Error computing fax queue status")
            out["ok"] = False
            out["error"] = str(e)
        return out

    @staticmethod
    def _lifetime() -> Optional[Dict[str, Any]]:
        """None when the counters could not be read: the panels then say
        "unavailable" rather than render a fabricated zero."""
        try:
            return _lifetime_counters()
        except Exception:
            logger.opt(exception=True).error("Error reading the lifetime counters")
            return None

    @staticmethod
    def _all_time_status(counters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Lifetime totals (Melanie, 2026-09-11): counters that only go up
        (appeals generated, people with a generated draft, faxes delivered,
        deletion requests), which deleting a person's data cannot change;
        "ever created" from the id sequences as a cross-check; and what is
        still present today.
        """
        out: Dict[str, Any] = {"ok": True, "error": None}
        try:
            from fighthealthinsurance.models import Denial, FaxesToSend, ProposedAppeal

            live = Denial.objects.aggregate(
                denials=Count("denial_id"), first=Min("date")
            )
            out["denials"] = live["denials"]
            out["first_denial"] = live["first"]
            out["drafts"] = ProposedAppeal.objects.filter(chosen=False).count()
            out["denials_with_draft"] = (
                Denial.objects.filter(proposedappeal__chosen=False)
                .values("pk")
                .distinct()
                .count()
            )
            out["people_with_draft"] = (
                Denial.objects.filter(proposedappeal__chosen=False)
                .exclude(hashed_email="")
                .values("hashed_email")
                .distinct()
                .count()
            )
            out["faxes_sent"] = FaxesToSend.objects.filter(sent=True).count()
            out["faxes_delivered"] = FaxesToSend.objects.filter(
                fax_success=True
            ).count()
            out["denials_ever"] = _sequence_value(Denial, "denial_id")
            out["drafts_ever"] = _sequence_value(ProposedAppeal, "id")
            out["faxes_ever"] = _sequence_value(FaxesToSend, "fax_id")
            keys = (
                "appeals_generated",
                "people_with_draft_lifetime",
                "faxes_sent_lifetime",
                "faxes_delivered_lifetime",
                "counting_since",
                "removal_requests",
                "removed_denials",
                "removals_since",
            )
            for key in keys:
                out[key] = None if counters is None else counters[key]
        except Exception as e:
            logger.opt(exception=True).error("Error computing all-time status")
            out["ok"] = False
            out["error"] = str(e)
        return out

    @staticmethod
    def _fax_outcome_status(
        counters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Fax delivery outcomes (last 7 days) from the database.

        The Temporal panel above shows workflow *status*, where "Completed"
        only means the workflow finished -- a failed send still completes after
        notifying the user (its Result is false). This panel answers the
        question staff actually have: did the faxes get delivered? A row that
        failed with ``vendor_send_completed`` still True cannot be re-sent by
        the normal paths until the claim is released, so it is called out.
        """
        from fighthealthinsurance.models import FaxesToSend

        try:
            since = timezone.now() - datetime.timedelta(days=7)
            # Filter on the last send attempt, not creation time: a fax created
            # weeks ago that failed today must show here (PR #959 review).
            recent = FaxesToSend.objects.filter(
                Q(attempting_to_send_as_of__gte=since) | Q(date__gte=since),
                sent=True,
            )
            # Order by attempt recency too, or an old-created fax admitted by
            # the attempt filter gets displaced from the 10-row slice below by
            # newer-created rows whose failures are actually older.
            failed_qs = recent.filter(fax_success=False).order_by(
                F("attempting_to_send_as_of").desc(nulls_last=True), "-date"
            )
            failed = [
                {
                    "uuid": str(f.uuid),
                    "date": f.date,
                    "claim_stuck": f.vendor_send_completed,
                }
                for f in failed_qs[:10]
            ]
            return {
                "sent": recent.count(),
                "delivered": recent.filter(fax_success=True).count(),
                # Melanie (2026-09-11): the number that matters over time. One
                # aggregate, so the count and the date come from one snapshot
                # (review). `date` is the row's creation time; nothing records
                # the delivery time, so the label says "oldest ... created".
                # Lifetime delivered: a counter bumped at each successful
                # finalize, untouched by deletion (lifetime_counters.py).
                "delivered_all_time": (
                    None if counters is None else counters["faxes_delivered_lifetime"]
                ),
                "failed": failed_qs.count(),
                "stuck_claims": failed_qs.filter(vendor_send_completed=True).count(),
                "recent_failures": failed,
                "error": None,
            }
        except Exception as e:
            logger.opt(exception=True).warning("Fax outcome status failed")
            return {"error": str(e)}

    @staticmethod
    def _intake_funnel_status() -> Dict[str, Any]:
        """Intake funnel (last 7 days), derived from the database: stage
        tallies only, no case content. Where do people stop between starting
        a denial submission and holding drafts?"""
        from fighthealthinsurance.models import Denial, ProposedAppeal

        try:
            since = timezone.now().date() - datetime.timedelta(days=7)
            denials = Denial.objects.filter(date__gte=since)
            started = denials.count()
            attempted = denials.filter(gen_attempts__gt=0).count()
            with_drafts = (
                denials.filter(
                    proposedappeal__speculative=False,
                )
                .distinct()
                .count()
            )
            chosen = denials.filter(proposedappeal__chosen=True).distinct().count()
            return {
                "started": started,
                "attempted_generation": attempted,
                "with_drafts": with_drafts,
                "chose_appeal": chosen,
                "error": None,
            }
        except Exception as e:  # pragma: no cover - defensive dashboard path
            return {"error": str(e)}

    @staticmethod
    def _temporal_error_message(exc: Exception) -> str:
        """One operator-readable sentence for a Temporal failure.

        The strings the frontend hands back name a symptom and nothing else
        ("invalid query: operation is not supported: 'ORDER BY' clause"), which
        on a status page reads as an outage even when the cluster is perfectly
        healthy. The failures we can explain get a hint that says whose problem
        it is; anything else falls back to the exception type plus its text, so
        an unrecognised error is still attributable and never renders blank.
        """
        detail = str(exc).strip()
        lowered = detail.lower()
        if isinstance(exc, TimeoutError):
            return (
                "Temporal did not answer within the status page's 8s budget: "
                "the frontend is unreachable or overloaded. Fax dispatch may "
                "be affected -- check the temporal-frontend pods."
            )
        if "order by" in lowered:
            return (
                "Temporal rejected the status page's visibility query: this "
                "cluster keeps visibility in Postgres, and only an "
                "Elasticsearch-backed cluster accepts an ORDER BY clause. That "
                "is a bug in this page, not a fax outage -- the counts above "
                f"are still live. Detail: {detail}"
            )
        if "invalid query" in lowered or "is not supported" in lowered:
            return (
                "Temporal rejected the status page's visibility query -- a bug "
                "in this page rather than a fax outage. "
                f"Detail: {detail}"
            )
        if not detail:
            return f"{type(exc).__name__} (no detail)"
        return f"{type(exc).__name__}: {detail}"

    @staticmethod
    def _temporal_status() -> Dict[str, Any]:
        """Temporal fax orchestration: connectivity, recent SendFaxWorkflow runs.

        Answers "is Temporal up and is it doing the fax work?" from the same
        client the app dispatches through (``temporal_client``), so what this
        shows is what the web pods actually see. The Temporal Web UI is
        deliberately not exposed outside the cluster; this section carries the
        numbers on-call needs and points at the port-forward for the rest.

        Bounded: one connection plus a handful of visibility queries under a
        single timeout, so a wedged frontend degrades to an error row.
        """
        from django.conf import settings

        enabled = bool(getattr(settings, "TEMPORAL_ENABLED", False))
        out: Dict[str, Any] = {
            "ok": True,
            "error": None,
            "enabled": enabled,
            "host": getattr(settings, "TEMPORAL_HOST", None),
            "namespace": getattr(settings, "TEMPORAL_NAMESPACE", None),
            "task_queue": getattr(settings, "TEMPORAL_TASK_QUEUE", None),
            "counts": {},
            "recent": [],
            "recent_error": None,
        }
        if not enabled:
            return out
        try:
            import asyncio

            from asgiref.sync import async_to_sync

            from fighthealthinsurance.temporal_client import get_temporal_client

            since = (timezone.now() - datetime.timedelta(days=7)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            base = f"WorkflowType='SendFaxWorkflow' AND StartTime > '{since}'"
            statuses = ("Running", "Completed", "Failed", "TimedOut", "Terminated")

            async def gather() -> Dict[str, Any]:
                client = await get_temporal_client()
                counts: Dict[str, int] = {}
                for status in statuses:
                    # Running is a live state, so count every open run; the
                    # terminal states are bounded to the last seven days.
                    scope = (
                        "WorkflowType='SendFaxWorkflow'"
                        if status == "Running"
                        else base
                    )
                    result = await client.count_workflows(
                        f"{scope} AND ExecutionStatus='{status}'"
                    )
                    counts[status] = int(result.count)
                limit = AdminStatusView._RECENT_WORKFLOW_LIMIT
                scan = AdminStatusView._RECENT_WORKFLOW_SCAN
                recent: List[Dict[str, Any]] = []
                recent_error: Optional[str] = None
                try:
                    # Deliberately no ORDER BY. Visibility here is the Postgres
                    # store (k8s/temporal/values.yaml -- no Elasticsearch), and
                    # a SQL visibility store rejects the entire query with
                    # "operation is not supported: 'ORDER BY' clause".
                    #
                    # Which means the store's own order is not the one this
                    # panel wants. Temporal's default is
                    # "ClosedTime DESC NULL FIRST, StartTime DESC", so taking
                    # the first ten and sorting THOSE lets an old workflow that
                    # closed recently push out a genuinely recent one, and the
                    # panel quietly shows the wrong ten. Scan a wider bounded
                    # candidate set, then sort and slice locally.
                    #
                    # `base` and not a bare type filter: it carries the same
                    # seven-day window the counts use. Without it the scan is
                    # over ALL history, and since we can neither order in the
                    # query nor scan without a bound, a namespace with more
                    # than `scan` old runs would fill the candidate set with
                    # them and crowd out the recent ones -- the bounded scan
                    # would then reintroduce the very defect it was added to
                    # fix, and the panel and its own counts would disagree.
                    #
                    # It follows the TERMINAL counts, not the Running one:
                    # Running is deliberately unscoped above because a live
                    # run matters however old it is, but this is a "recent
                    # runs" table. A fax still open after seven days is
                    # reported by the Running count rather than listed here.
                    async for wf in client.list_workflows(
                        base,
                        page_size=scan,
                    ):
                        duration = None
                        if wf.start_time and wf.close_time:
                            duration = round(
                                (wf.close_time - wf.start_time).total_seconds()
                            )
                        recent.append(
                            {
                                "id": wf.id,
                                "status": wf.status.name if wf.status else "UNKNOWN",
                                "started": wf.start_time,
                                "closed": wf.close_time,
                                "duration_s": duration,
                            }
                        )
                        if len(recent) >= scan:
                            break
                except Exception as e:
                    # The counts are the numbers on-call actually pages on, so
                    # a listing that blows up costs the table, not the panel.
                    logger.opt(exception=True).warning(
                        "Temporal recent-workflow listing failed"
                    )
                    recent_error = AdminStatusView._temporal_error_message(e)
                # Sort the whole candidate set, THEN take the ten. Slicing
                # before sorting is what let the store's ordering decide which
                # runs the panel could even consider.
                recent.sort(key=lambda row: row["started"] or _UNDATED, reverse=True)
                recent = recent[:limit]
                return {
                    "counts": counts,
                    "recent": recent,
                    "recent_error": recent_error,
                }

            async def bounded() -> Dict[str, Any]:
                return await asyncio.wait_for(gather(), timeout=8.0)

            data = async_to_sync(bounded)()
            out["counts"] = data["counts"]
            out["recent"] = data["recent"]
            out["recent_error"] = data["recent_error"]
        except Exception as e:
            logger.opt(exception=True).warning("Temporal status check failed")
            out["ok"] = False
            out["error"] = AdminStatusView._temporal_error_message(e)
        return out

    @staticmethod
    def _scoring_failure_hint(summary: str) -> str:
        """What a recorded scoring failure most likely means, for on-call."""
        if summary == "HTTP 402":
            return "payment required: TypeSafe credits or billing"
        if summary in ("HTTP 401", "HTTP 403"):
            return "the API key was rejected"
        if summary == "HTTP 429":
            return "rate limited"
        if summary.startswith("HTTP 5"):
            return "TypeSafe server error"
        if summary == "timeout":
            return "no answer within TYPESAFE_TIMEOUT_SECONDS"
        return ""

    @staticmethod
    def _letter_scoring_status() -> Dict[str, Any]:
        """TypeSafe draft scoring (last 24h): on or off, scoring or not, and
        if not, why. Counts and a short failure summary only, no letter text.

        Built from the database, not from this process's counters: the page
        is served by whichever web pod the browser is pinned to, and the
        Temporal worker scores drafts too. The scoring call site keeps one
        ExternalServiceHealth row current; the draft rows give the counts.
        """
        out: Dict[str, Any] = {
            "ok": True,
            "error": None,
            "level": "off",
            "key_present": False,
            "flag_on": False,
            "timeout_seconds": None,
            "window_hours": 24,
            "scored": 0,
            "unscored": 0,
            "stalled": 0,
            "last_success_at": None,
            "last_failure_at": None,
            "last_failure": "",
            "last_failure_hint": "",
            "recovered": False,
        }
        try:
            from django.conf import settings

            from fighthealthinsurance.letter_quality_metrics import WINDOW
            from fighthealthinsurance.models import ExternalServiceHealth

            out["key_present"] = bool(getattr(settings, "TYPESAFE_API_KEY", None))
            out["flag_on"] = bool(
                getattr(settings, "TYPESAFE_LETTER_RANKING_ENABLED", False)
            )
            out["timeout_seconds"] = getattr(settings, "TYPESAFE_TIMEOUT_SECONDS", None)
            out["window_hours"] = int(WINDOW.total_seconds() // 3600)

            now = timezone.now()
            since = now - WINDOW
            # The same eligibility as the unscored count below, so the two
            # numbers describe one population and a scored draft that is
            # speculative or unconsented cannot make the level SCORING by
            # itself (review).
            out["scored"] = ProposedAppeal.objects.filter(
                quality_scored_at__gte=since,
                speculative=False,
                for_denial__use_external=True,
            ).count()
            # Drafts that should have been scored and were not: consented,
            # real (not speculative), old enough that a score in flight would
            # have landed, and still without one.
            settled = now - datetime.timedelta(seconds=letter_quality.DRAIN_SECONDS)
            eligible_unscored = ProposedAppeal.objects.filter(
                created_at__gte=since,
                created_at__lt=settled,
                speculative=False,
                for_denial__use_external=True,
                quality_score__isnull=True,
            )
            out["unscored"] = eligible_unscored.count()

            health = ExternalServiceHealth.objects.filter(
                service=letter_quality.SERVICE
            ).first()
            success_at = health.last_success_at if health else None
            failure_at = health.last_failure_at if health else None
            out["last_success_at"] = success_at
            out["last_failure_at"] = failure_at
            out["last_failure"] = health.last_failure if health else ""
            out["last_failure_hint"] = AdminStatusView._scoring_failure_hint(
                out["last_failure"]
            )
            out["recovered"] = bool(
                failure_at and success_at and success_at > failure_at
            )
            fresh_failure = bool(
                failure_at
                and failure_at >= since
                and (success_at is None or failure_at > success_at)
            )
            # Eligible drafts newer than the last recorded success that never
            # got a score: scoring stopped in a way the failure hook cannot
            # see (it never ran), and one older score must not keep the badge
            # green all day (review).
            stalled = eligible_unscored
            if success_at is not None:
                stalled = stalled.filter(created_at__gt=success_at)
            out["stalled"] = stalled.count()

            if not letter_quality.enabled():
                out["level"] = "off"
            elif fresh_failure:
                out["level"] = "failing"
            elif out["stalled"]:
                out["level"] = "not_scoring"
            elif out["scored"]:
                out["level"] = "scoring"
            elif out["unscored"]:
                out["level"] = "not_scoring"
            else:
                out["level"] = "idle"
        except Exception as e:
            logger.opt(exception=True).warning("Letter scoring health check failed")
            out["ok"] = False
            out["error"] = str(e)
        return out

    @staticmethod
    def _storage_status() -> Dict[str, Any]:
        """Whether the external (encrypted) storage backend is reachable."""
        out: Dict[str, Any] = {"ok": False, "error": None, "location": None}
        try:
            from django.conf import settings
            from stopit import ThreadingTimeout as Timeout

            es = settings.EXTERNAL_STORAGE
            out["location"] = getattr(es, "location", None)
            with Timeout(3.0):
                es.listdir("./")
                out["ok"] = True
                return out
            out["error"] = "timeout"
        except FileNotFoundError as e:
            # The storage root itself is missing: in prod that means the shared
            # volume isn't mounted, locally it usually means the directory was
            # never created. A full traceback adds nothing over the path, so
            # log a one-liner instead of dumping a stack on every page load.
            logger.warning(f"External storage location missing: {e}")
            out["error"] = f"storage location does not exist: {e.filename}"
        except Exception as e:
            logger.opt(exception=True).warning("External storage health check failed")
            out["error"] = str(e)
        return out


class AdminModelQueryView(View):
    """Staff page to send an ad-hoc prompt at one specific model backend.

    Reached from the "query" link on each row of the system status page. The
    status page answers "is this backend up?"; this answers the follow-up
    question, "what does it actually say?" -- useful for telling a backend
    that is merely slow apart from one returning refusals, empty text, or a
    credentials error, without waiting for a real appeal to route to it.

    The prompt goes straight to the chosen backend via
    ``model_query.query_model``: no router selection, no fan-out, no
    fallback to a different model, so the captured response is attributable
    to that backend. It makes a real (billable) inference call, hence
    staff-only and never triggered by simply loading the page.
    """

    template_name = "admin_model_query.html"
    unknown_ref_error = (
        "Unknown model reference. It may be from an older deployment "
        "-- pick a backend below and try again."
    )

    def get(self, request):
        ref = request.GET.get("ref") or ""
        return self._render(request, ref=ref)

    def post(self, request):
        ref = request.POST.get("ref") or ""
        prompt = (request.POST.get("prompt") or "").strip()
        system_prompt = (request.POST.get("system_prompt") or "").strip()
        temperature = model_query.clamp_temperature(request.POST.get("temperature"))
        timeout = model_query.clamp_timeout(request.POST.get("timeout"))

        result: Optional[Dict[str, Any]] = None
        error: Optional[str] = None
        model = self._lookup(ref)
        if model is None:
            error = self.unknown_ref_error
        elif not prompt:
            error = "Enter a prompt to send."
        elif len(prompt) > model_query.MAX_PROMPT_LENGTH:
            error = (
                f"Prompt is too long ({len(prompt)} characters); "
                f"the limit is {model_query.MAX_PROMPT_LENGTH}."
            )
        else:
            logger.info(
                f"Staff user {request.user.username} sending a direct query to "
                f"model {model} ({len(prompt)} chars, timeout {timeout:g}s)"
            )
            result = model_query.query_model(
                model,
                prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                timeout=timeout,
            )

        return render(
            request,
            self.template_name,
            self._context(
                ref=ref,
                prompt=prompt,
                system_prompt=system_prompt,
                temperature=temperature,
                timeout=timeout,
                result=result,
                error=error,
            ),
        )

    @staticmethod
    def _lookup(ref: str):
        """Resolve a model reference, treating a broken router as "not found"
        so the page renders an error row instead of a 500."""
        try:
            return model_query.find_model_by_ref(ref)
        except Exception:
            logger.opt(exception=True).error("Error resolving model reference")
            return None

    def _render(self, request, **kwargs):
        return render(request, self.template_name, self._context(**kwargs))

    def _context(
        self,
        ref: str = "",
        prompt: str = "",
        system_prompt: str = "",
        temperature: float = model_query.DEFAULT_TEMPERATURE,
        timeout: float = model_query.DEFAULT_TIMEOUT,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> Dict[str, Any]:
        available: List[Dict[str, Any]] = []
        selected: Optional[Dict[str, Any]] = None
        enumeration_error: Optional[str] = None
        try:
            for candidate_ref, candidate in model_query.enumerate_queryable_models():
                described = model_query.describe_model(candidate, ref=candidate_ref)
                available.append(described)
                if candidate_ref == ref:
                    selected = described
        except Exception as e:
            logger.opt(exception=True).error("Error enumerating model backends")
            enumeration_error = str(e)

        if ref and selected is None and error is None and enumeration_error is None:
            error = self.unknown_ref_error

        return {
            "title": "Query a Model Backend",
            "ref": ref,
            "selected": selected,
            "available": available,
            "enumeration_error": enumeration_error,
            "prompt": prompt,
            "system_prompt": system_prompt,
            "temperature": temperature,
            "timeout": timeout,
            "default_system_prompt": model_query.DEFAULT_SYSTEM_PROMPT,
            "max_prompt_length": model_query.MAX_PROMPT_LENGTH,
            "max_timeout": model_query.MAX_TIMEOUT,
            "result": result,
            "error": error,
        }


class ResendStuckFaxView(View):
    """Release a stranded vendor-send claim and re-dispatch the fax.

    A fax whose worker died mid-send (OOM, eviction, node loss) leaves
    ``vendor_send_completed`` True with no in-process ``except`` alive to
    release it. ``send_fax_via_vendor`` then short-circuits on that claim
    forever, so the fax can never be retried by any normal path -- it just
    sits in the status dashboard as "STUCK". Clearing it by hand meant a
    shell on a prod pod; this is that operation, gated and audited.

    POST only, and deliberately so: a GET would let a crawler, a prefetch or
    a mis-pasted link re-fax a patient's appeal to an insurer.

    The one thing this must never do is re-send a fax that actually went
    through. A stranded claim means the send was CLAIMED, not that it
    completed -- the claim is taken before the document is handed to the
    vendor. So the guard is ``fax_success``: if the fax succeeded, refuse,
    whatever the claim says. ``precheck_fax`` enforces the same rule
    downstream, but refusing here keeps a dashboard mis-click from ever
    reaching the send path.
    """

    def post(self, request):
        from asgiref.sync import async_to_sync

        from fighthealthinsurance import temporal_client
        from fighthealthinsurance.models import FaxesToSend

        fax_uuid = (request.POST.get("uuid") or "").strip()
        if not fax_uuid:
            return HttpResponse("Missing uuid", status=400)
        try:
            fax = FaxesToSend.objects.get(uuid=fax_uuid)
        except (FaxesToSend.DoesNotExist, ValidationError, ValueError):
            return HttpResponse("No such fax", status=404)

        if fax.fax_success:
            # Already delivered; re-sending would fax the insurer twice.
            logger.warning(
                f"Staff resend refused for fax uuid={fax.uuid}: already successful"
            )
            return HttpResponse("Fax already sent successfully; refusing", status=409)
        if not fax.destination or not fax.destination.strip():
            # Blank and whitespace-only pass an `is None` check but precheck_fax
            # treats them as missing, so the endpoint would report a resend
            # started that could never send (external review).
            return HttpResponse("Fax has no destination", status=400)

        released = FaxesToSend.objects.filter(pk=fax.pk).update(
            vendor_send_completed=False
        )
        logger.info(
            f"Staff {request.user} resending fax uuid={fax.uuid} "
            f"(claim released: {bool(released)})"
        )
        try:
            # force_restart because this IS the explicit resend that flag is
            # for: the workflow id is deterministic per fax, so a failed send
            # whose run is still open would raise WorkflowAlreadyStartedError
            # and this recovery endpoint would answer 502 for the one case it
            # exists to handle (external review).
            workflow_id = async_to_sync(temporal_client.start_send_fax_workflow)(
                fax.hashed_email, str(fax.uuid), force_restart=True
            )
        except Exception as e:
            logger.opt(exception=True).error(
                f"Staff resend failed to start workflow for fax uuid={fax.uuid}"
            )
            return HttpResponse(f"Could not start send: {e}", status=502)
        return HttpResponse(f"Resend started: {workflow_id}")


class ScheduleFollowUps(View):
    """A view to go through and schedule any missing follow ups.

    Runs schedule_follow_ups on all denials with an email address.
    The function is idempotent (uses update_or_create and skips
    past-dated follow-ups) so it's safe to run on denials that
    already have some or all follow-ups scheduled.
    """

    def get(self, request):
        denials = Denial.objects.filter(raw_email__isnull=False).iterator()
        c = 0
        for denial in denials:
            if denial.raw_email is None:
                continue
            schedule_follow_ups(denial.raw_email, denial)
            c = c + 1
        return HttpResponse(str(c))


class FollowUpEmailSenderView(generic.FormView):
    """A view to test the follow up sender."""

    template_name = "followup_test.html"
    form_class = FollowUpTestForm

    def form_valid(self, form):
        s = FollowUpEmailSender()
        field = form.cleaned_data.get("email")
        try:
            count = int(field)
            sent = s.send_all(count=field)
        except ValueError:
            sent = s.dosend(email=field)
        return HttpResponse(str(sent))


class ThankyouSenderView(generic.FormView):
    """A view to test the thankyou sender."""

    template_name = "followup_test.html"
    form_class = core_forms.FollowUpTestForm

    def form_valid(self, form):
        s = ThankyouEmailSender()
        field = form.cleaned_data.get("email")
        try:
            count = int(field)
            sent = s.send_all(count=field)
        except ValueError:
            sent = s.dosend(email=field)
        return HttpResponse(str(sent))


class ActivateProUserView(generic.FormView):
    template_name = "pro_domain_task.html"
    form_class = core_forms.ActivateProForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Activate Pro User"
        context["heading"] = "Activate Pro User Domain"
        context["description"] = "Enter the phone number of the domain to activate."
        context["button_text"] = "Activate"
        return context

    def form_valid(self, form):
        phonenumber = form.cleaned_data.get("phonenumber")
        try:
            domain = UserDomain.objects.get(visible_phone_number=phonenumber)
        except UserDomain.DoesNotExist:
            return HttpResponse(
                f"No domain found with phone number {phonenumber}", status=404
            )
        domain.active = True
        domain.save()
        # Update all professionals associated with the domain
        professionals = ProfessionalUser.objects.filter(domains__in=[domain])
        professionals.update(active=True)
        # Bulk update the auth users
        user_ids = list(professionals.values_list("user_id", flat=True))
        User.objects.filter(id__in=user_ids).update(is_active=True)
        # Bulk update domain relations
        ProfessionalDomainRelation.objects.filter(domain=domain).update(
            active_domain_relation=True,
            pending_domain_relation=False,
            suspended=False,
            rejected=False,
        )
        return HttpResponse("Pro user activated")


class EnableBetaForDomainView(generic.FormView):
    """A view to enable beta features for a user domain by phone number."""

    template_name = "pro_domain_task.html"
    form_class = core_forms.ActivateProForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Enable Beta Features"
        context["heading"] = "Enable Beta Features for Domain"
        context["description"] = (
            "Enter the phone number of the domain to enable beta features."
        )
        context["button_text"] = "Enable Beta"
        return context

    def form_valid(self, form):
        try:
            phonenumber = form.cleaned_data.get("phonenumber")
            domain = UserDomain.objects.get(visible_phone_number=phonenumber)
            with transaction.atomic():
                domain.beta = True
                domain.save()
            return HttpResponse(
                f"Beta features enabled for domain {domain.name} ({phonenumber})"
            )
        except UserDomain.DoesNotExist:
            return HttpResponse(
                f"No domain found with phone number {phonenumber}", status=404
            )
        except Exception as e:
            logger.opt(exception=True).error(
                f"Error enabling beta for domain with phone {phonenumber}: {str(e)}"
            )
            return HttpResponse(f"Error enabling beta: {str(e)}", status=500)


class FollowUpFaxSenderView(generic.FormView):
    """A view to test the follow up sender."""

    template_name = "followup_test.html"
    form_class = core_forms.FollowUpTestForm

    def form_valid(self, form):
        field = form.cleaned_data.get("email")

        if field.isdigit():
            sent = SendFaxHelper.blocking_dosend_all(count=field)
        else:
            sent = SendFaxHelper.blocking_dosend_target(email=field)

        return HttpResponse(str(sent))


class _SendBulkMailView(generic.FormView):
    """Shared machinery for the staff broadcast-email pages.

    Subclasses pick an audience by naming the actor method that knows how to
    enumerate its recipients and supplying a recipient count for the page. Both
    audiences use the same compose form (subject + HTML + text + optional test
    address), the same Ray-backed send, and the same result reporting; only the
    recipient set and the page copy differ.
    """

    template_name = "send_bulk_email.html"
    form_class = core_forms.SendMailingListMailForm

    # Name of the MailingListActor method that sends to this audience. Looked up
    # by name so the actor handle stays a plain Ray handle.
    actor_method_name: str = ""
    # Page copy: the heading, and how recipients are described in the count box,
    # the warning, and result/error messages.
    page_title: str = ""
    audience_label: str = ""
    audience_noun: str = ""

    def recipient_count(self) -> int:
        raise NotImplementedError

    @staticmethod
    def _distinct_email_count(qs: QuerySet[Any]) -> int:
        """Count distinct (case-insensitive) email addresses in a queryset.

        Matches what the actor actually sends -- it dedupes recipients by
        lowercased address -- so the page never overstates a send because
        someone signed up twice.
        """
        return int(
            qs.annotate(_lower_email=Lower("email"))
            .values("_lower_email")
            .distinct()
            .count()
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = self.page_title
        context["audience_label"] = self.audience_label
        context["recipient_count"] = self.recipient_count()
        return context

    def form_valid(self, form):
        subject = form.cleaned_data.get("subject")
        html_content = form.cleaned_data.get("html_content")
        text_content = form.cleaned_data.get("text_content")
        test_email = form.cleaned_data.get("test_email")

        # Without a cluster to attach to, touching the actor ref would auto-init
        # a local Ray cluster in this web process just to send staff mail. Tell
        # the operator plainly instead -- this is a synchronous admin action, so
        # a clear message beats a silent cluster (and a confusing timeout).
        if not ray_cluster_available():
            logger.warning(
                f"{self.audience_noun.capitalize()} send requested but no Ray "
                "cluster is available"
            )
            return HttpResponse(
                f"No Ray cluster available; {self.audience_noun} email not sent.",
                status=503,
            )

        try:
            # Use ray actor for sending emails
            actor = mailing_list_actor_ref.get
            remote_method = getattr(actor, self.actor_method_name)
            future = remote_method.remote(
                subject, html_content, text_content, test_email
            )
            sent_count, failed_count, blocked_count = ray.get(future)

            if test_email:
                masked_email = mask_email_for_logging(test_email)
                return HttpResponse(f"Test email sent successfully to {masked_email}")
            else:
                logger.info(
                    f"Staff user {self.request.user.username} sent a "
                    f"{self.audience_noun} email to {sent_count} recipients "
                    f"({failed_count} failed, {blocked_count} blocked)"
                )
                return HttpResponse(
                    f"{self.audience_label} email sent. Success: {sent_count}, "
                    f"Failed: {failed_count}, Blocked: {blocked_count}"
                )
        except Exception as e:
            logger.opt(exception=True).error(
                f"Error sending {self.audience_noun} email: {e}"
            )
            # Generic message only: exception text can carry hostnames or
            # addresses, and HttpResponse renders it unescaped. Details stay in
            # the log line above (matching ProConnectorProcessView's pattern).
            return HttpResponse(
                f"Error sending {self.audience_noun} email. The error has been "
                "logged; please try again.",
                status=500,
            )


class SendMailingListMailView(_SendBulkMailView):
    """A view to send emails to all mailing list subscribers."""

    actor_method_name = "send_mailing_list_email"
    page_title = "Send Mailing List Email"
    audience_label = "Mailing list"
    audience_noun = "mailing list"

    def recipient_count(self) -> int:
        return self._distinct_email_count(MailingListSubscriber.objects.all())


class SendInterestedProfessionalMailView(_SendBulkMailView):
    """A view to send emails to all interested professionals.

    The counterpart of the mailing-list broadcast for the professional signup
    list. Recipients are every professional signup except test/spam records and
    anyone who opted out (see ``mailable_interested_professionals``), deduped by
    address. Unlike the pro-connector workflow -- which introduces one
    professional at a time and records per-record state -- this is a one-shot
    broadcast and writes nothing back to the records, so it neither consumes nor
    disturbs the pro-connector queue.
    """

    actor_method_name = "send_interested_professional_email"
    page_title = "Send Interested Professional Email"
    audience_label = "Interested professional"
    audience_noun = "interested professional"

    def recipient_count(self) -> int:
        return self._distinct_email_count(mailable_interested_professionals())


# Bucket label for chosen ProposedAppeal rows whose model_name is NULL and
# whose created_at is set — i.e. a pick recorded after model tracking began
# that still couldn't be attributed back to a generated draft (heavy edit
# with multiple models in play, or the share-appeal flow). Surfacing them
# keeps the dashboard's total-picks number honest. Rows predating tracking
# (created_at NULL) or explicitly stamped by the backfill are reported under
# LEGACY_UNATTRIBUTED_LABEL instead so legacy gaps stay distinguishable.
UNKNOWN_MODEL_LABEL = "(unattributed)"


def _merge_stats(
    chosen: Dict[str, int],
    presented: Dict[str, int],
    quality: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Combine per-model chosen + presented counts into a sorted list of dicts.

    ``win_rate`` is chosen/presented as a percentage, or ``None`` when the
    model has no presented count (no denominator — e.g. legacy chosen rows
    without presentation records). Templates render ``None`` as an em dash;
    it must never be displayed as 0.0%.

    ``quality`` (optional) is per-model draft-quality aggregates from
    ``ml/letter_quality.py``: ``{"avg": 0..1, "scored": n, "ungrounded": n}``.
    Win rate is the lagging signal (it needs a user to pick a draft, weeks of
    them to mean anything); the quality average is the leading one, available
    the minute a backend ships. A model with no scored drafts gets ``None``
    for the average, rendered as a dash, never as 0.
    """
    quality = quality or {}
    rows: List[Dict[str, Any]] = []
    for model_name in set(chosen) | set(presented) | set(quality):
        c = chosen.get(model_name, 0)
        p = presented.get(model_name, 0)
        win_rate = (c / p * 100.0) if p > 0 else None
        q = quality.get(model_name) or {}
        rows.append(
            {
                "model_name": model_name,
                "chosen": c,
                "presented": p,
                "win_rate": win_rate,
                "quality_avg": q.get("avg"),
                "quality_scored": int(q.get("scored") or 0),
                "quality_ungrounded": int(q.get("ungrounded") or 0),
                "quality_scorer": q.get("scorer"),
                "quality_other_scorer": int(q.get("other_scorer") or 0),
            }
        )
    rows.sort(
        key=lambda r: (
            -r["chosen"],
            -(r["win_rate"] if r["win_rate"] is not None else -1.0),
            r["model_name"],
        )
    )
    return rows


class ModelUsageDashboardView(generic.TemplateView):
    """Staff dashboard showing which ML models users pick most often.

    Aggregates three signal sources across four time windows:
      * ProposedAppeal.chosen=True  - implicit pick from real denial flow
      * ChooserVote (kind=appeal_letter) - synthetic chooser appeal vote
      * ChooserVote (kind=chat_response) - synthetic chooser chat vote

    Time-window semantics (all timezone-aware, anchored on timezone.now()):
      * Windows are rolling: "Last 1 Day" = the preceding 24 hours, "Last 7
        Days" / "Last 30 Days" = the preceding 7/30 days. "All Time" has no
        lower bound and additionally includes rows that predate timestamp
        tracking (ProposedAppeal.created_at NULL, pre-migration-0182), which
        no bounded window can include.
      * The event timestamp is the *selection* event: the chosen
        ProposedAppeal row's created_at (the pick), or ChooserVote.created_at
        (the vote). Presented counts use the same event set: for votes, the
        candidates listed on the in-window votes; for ProposedAppeal, the
        drafts generated for denials picked in the window (a draft generated
        on day 0 and picked on day 1 still counts as presented in a 1-day
        window anchored on the pick).

    All stored model names pass through normalize_model_label so historical
    object-repr values aggregate per class (without memory addresses) even
    before the backfill_model_usage_attribution command has run.
    """

    template_name = "model_usage_dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = timezone.now()
        windows = [
            ("global", "All Time", None),
            ("1d", "Last 1 Day", now - datetime.timedelta(days=1)),
            ("7d", "Last 7 Days", now - datetime.timedelta(days=7)),
            ("30d", "Last 30 Days", now - datetime.timedelta(days=30)),
        ]
        windows_ctx = []
        for slug, label, since in windows:
            proposed = self._proposed_appeal_stats(since)
            chooser_appeal = self._chooser_stats("appeal_letter", since)
            chooser_chat = self._chooser_stats("chat_response", since)
            windows_ctx.append(
                {
                    "slug": slug,
                    "label": label,
                    "proposed_appeal": proposed,
                    "context_level": self._context_level_stats(since),
                    "chooser_appeal": chooser_appeal,
                    "chooser_chat": chooser_chat,
                    "chart_data_json": json.dumps(
                        self._chart_data(proposed, chooser_appeal, chooser_chat)
                    ),
                }
            )
        ctx["title"] = "ML Model Usage Dashboard"
        ctx["windows"] = windows_ctx
        return ctx

    @staticmethod
    def _chart_data(
        proposed: List[Dict[str, Any]],
        chooser_appeal: List[Dict[str, Any]],
        chooser_chat: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build a CanvasJS-friendly stacked-column data structure."""
        labels: List[str] = []
        seen = set()
        for source in (proposed, chooser_appeal, chooser_chat):
            for row in source:
                if row["model_name"] not in seen:
                    seen.add(row["model_name"])
                    labels.append(row["model_name"])

        def series_for(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            by_name = {r["model_name"]: r["chosen"] for r in rows}
            return [{"label": lbl, "y": by_name.get(lbl, 0)} for lbl in labels]

        return {
            "labels": labels,
            "series": [
                {
                    "name": "ProposedAppeal (denial flow)",
                    "color": "#1f77b4",
                    "dataPoints": series_for(proposed),
                },
                {
                    "name": "Chooser - Appeal",
                    "color": "#ff7f0e",
                    "dataPoints": series_for(chooser_appeal),
                },
                {
                    "name": "Chooser - Chat",
                    "color": "#2ca02c",
                    "dataPoints": series_for(chooser_chat),
                },
            ],
        }

    @staticmethod
    def _proposed_appeal_stats(
        since: Optional[datetime.datetime],
    ) -> List[Dict[str, Any]]:
        # Keep chosen rows with model_name=NULL: mark_proposal_chosen falls
        # back to None when a pick can't be matched to a draft, and those are
        # still real picks. NULL rows predating model tracking (created_at
        # NULL, pre-migration-0182) bucket as LEGACY_UNATTRIBUTED_LABEL —
        # matching what the backfill stamps — while later rows fall under
        # UNKNOWN_MODEL_LABEL, so legacy gaps stay distinct from current
        # attribution misses.
        chosen_qs = ProposedAppeal.objects.filter(chosen=True)
        if since is not None:
            chosen_qs = chosen_qs.filter(created_at__gte=since)

        # Tie the presented universe to denials picked within the window,
        # NOT to draft created_at: a draft generated on day 0 and picked on
        # day 1 should still count as presented in a 1-day window anchored on
        # the pick. Pass the subquery straight into __in so Django emits SQL
        # rather than materializing a large id list. Drafts without a
        # model_name (pre-tracking) are excluded — attributing them to any
        # bucket would fabricate a win-rate denominator — so legacy chosen
        # rows report presented=0 and an em-dash win rate.
        chosen_denial_ids = chosen_qs.values_list("for_denial_id", flat=True).distinct()
        # Exclude held-back speculative rows: they were never shown (they carry a
        # real internal model_name, so counting them would silently pad that
        # model's presented denominator and deflate its win rate). Matches the
        # speculative=False guard in _context_level_stats and the serving path.
        presented_qs = ProposedAppeal.objects.filter(
            chosen=False,
            model_name__isnull=False,
            speculative=False,
            for_denial_id__in=chosen_denial_ids,
        )

        chosen: Counter = Counter()
        for name, count in (
            chosen_qs.filter(model_name__isnull=False)
            .values_list("model_name")
            .annotate(c=Count("id"))
        ):
            label = normalize_model_label(name) or UNKNOWN_MODEL_LABEL
            chosen[label] += count
        null_named = chosen_qs.filter(model_name__isnull=True)
        legacy_count = null_named.filter(created_at__isnull=True).count()
        unattributed_count = null_named.filter(created_at__isnull=False).count()
        if legacy_count:
            chosen[LEGACY_UNATTRIBUTED_LABEL] += legacy_count
        if unattributed_count:
            chosen[UNKNOWN_MODEL_LABEL] += unattributed_count
        presented: Counter = Counter()
        for name, count in presented_qs.values_list("model_name").annotate(
            c=Count("id")
        ):
            presented_label = normalize_model_label(name)
            if presented_label is not None:
                presented[presented_label] += count
        return _merge_stats(
            dict(chosen),
            dict(presented),
            ModelUsageDashboardView._draft_quality_stats(since),
        )

    @staticmethod
    def _draft_quality_stats(
        since: Optional[datetime.datetime],
    ) -> Dict[str, Dict[str, Any]]:
        """Per-model draft quality (ml/letter_quality.py) over the window.

        Every scored, non-speculative draft counts, chosen or not: this is
        about what each backend PRODUCES, not what users picked, so it is
        anchored on the draft's own created_at rather than on a later pick.
        Labels are normalized the same way as chosen/presented so the three
        line up in one row.

        One EXACT scorer per table: the provenance string of the most
        recently SCORED row (by quality_scored_at, not the draft's age: old
        drafts are rescored) in the window. Rows scored by any other scorer
        (an older rubric, or the same rubric answered by a repointed
        model) are counted in ``other_scorer`` and never averaged in, so a
        change on TypeSafe's side cannot masquerade as backend drift.
        """
        scored_qs = ProposedAppeal.objects.filter(
            quality_score__isnull=False,
            speculative=False,
            model_name__isnull=False,
        )
        if since is not None:
            scored_qs = scored_qs.filter(created_at__gte=since)
        latest = None
        for candidate in (
            scored_qs.filter(
                quality_scorer__startswith="typesafe/",
                quality_scorer__endswith=letter_quality._RUBRIC_SUFFIX,
            )
            .values("quality_scorer")
            .annotate(newest=Max("quality_scored_at"))
            .order_by("-newest")
            .values_list("quality_scorer", flat=True)[:20]
        ):
            if letter_quality.same_rubric(candidate):
                latest = candidate
                break
        out: Dict[str, Dict[str, Any]] = {}
        for name, avg, scored, ungrounded in (
            (scored_qs.filter(quality_scorer=latest) if latest else scored_qs.none())
            .values_list("model_name")
            .annotate(
                avg=Avg("quality_score"),
                scored=Count("id"),
                ungrounded=Count(
                    "id",
                    filter=Q(grounding_score__lt=letter_quality.GROUNDING_DEMOTE_BELOW),
                ),
            )
            .values_list("model_name", "avg", "scored", "ungrounded")
        ):
            label = normalize_model_label(name)
            if label is None:
                continue
            bucket = out.setdefault(
                label, {"sum": 0.0, "scored": 0, "ungrounded": 0, "other_scorer": 0}
            )
            bucket["sum"] += float(avg or 0.0) * int(scored)
            bucket["scored"] += int(scored)
            bucket["ungrounded"] += int(ungrounded)
        for name, count in (
            scored_qs.exclude(quality_scorer=latest)
            .values_list("model_name")
            .annotate(c=Count("id"))
            .values_list("model_name", "c")
        ):
            label = normalize_model_label(name)
            if label is None:
                continue
            bucket = out.setdefault(
                label, {"sum": 0.0, "scored": 0, "ungrounded": 0, "other_scorer": 0}
            )
            bucket["other_scorer"] += int(count)
        for bucket in out.values():
            bucket["avg"] = (
                bucket["sum"] / bucket["scored"] if bucket["scored"] else None
            )
            bucket["scorer"] = latest
            del bucket["sum"]
        return out

    @staticmethod
    def _context_level_stats(
        since: Optional[datetime.datetime],
    ) -> List[Dict[str, Any]]:
        """Chosen/presented/win-rate bucketed by the context/shed level the
        appeal was generated at (full / tier1_shed / tier2_shed / speculative /
        synthesized / template). Shows whether users end up choosing shed or
        speculative appeals as often as full-context ones. Speculative drafts
        that were never promoted are excluded from the presented denominator
        (they were held back, not shown)."""
        chosen_qs = ProposedAppeal.objects.filter(chosen=True)
        if since is not None:
            chosen_qs = chosen_qs.filter(created_at__gte=since)
        chosen_denial_ids = chosen_qs.values_list("for_denial_id", flat=True).distinct()
        presented_qs = ProposedAppeal.objects.filter(
            chosen=False,
            speculative=False,
            for_denial_id__in=chosen_denial_ids,
        )
        chosen: Counter = Counter()
        for level, count in chosen_qs.values_list("context_level").annotate(
            c=Count("id")
        ):
            chosen[level or UNKNOWN_MODEL_LABEL] += count
        presented: Counter = Counter()
        for level, count in presented_qs.values_list("context_level").annotate(
            c=Count("id")
        ):
            presented[level or UNKNOWN_MODEL_LABEL] += count
        # _merge_stats labels the bucket key "model_name"; the value here is the
        # context level. Reusing the shared table partial (which reads
        # model_name) keeps the key -- the template passes a "Context level"
        # column header instead.
        return _merge_stats(dict(chosen), dict(presented))

    @staticmethod
    def _chooser_stats(
        kind: str, since: Optional[datetime.datetime]
    ) -> List[Dict[str, Any]]:
        # Both chosen and presented derive from the same vote set (filtered
        # by ChooserVote.created_at), so a window's win rates compare like
        # with like: every vote event contributes its chosen candidate once
        # and each distinct presented candidate once. Candidate creation
        # time is irrelevant — candidates are generated ahead of votes.
        chosen_qs = ChooserVote.objects.filter(chosen_candidate__kind=kind)
        if since is not None:
            chosen_qs = chosen_qs.filter(created_at__gte=since)
        chosen: Counter = Counter()
        for name, count in chosen_qs.values_list(
            "chosen_candidate__model_name"
        ).annotate(c=Count("id")):
            label = normalize_model_label(name) or UNKNOWN_MODEL_LABEL
            chosen[label] += count

        # Presented: walk votes' presented_candidate_ids JSON lists into a
        # counter. We reuse chosen_qs (same filter) and call .iterator() so
        # the All Time window doesn't load every vote into a result cache.
        # Dedupe ids within a vote: a candidate was shown once per vote
        # event, and a duplicated id (buggy/hostile client, pre-dedupe
        # historical rows) must not inflate the denominator.
        counter: Counter = Counter()
        for ids in chosen_qs.values_list(
            "presented_candidate_ids", flat=True
        ).iterator():
            if ids:
                counter.update(set(ids))
        cand_to_model = dict(
            ChooserCandidate.objects.filter(
                id__in=list(counter.keys()), kind=kind
            ).values_list("id", "model_name")
        )
        presented: Counter = Counter()
        for cid, n in counter.items():
            presented_label = normalize_model_label(cand_to_model.get(cid))
            if presented_label is not None:
                presented[presented_label] += n
        return _merge_stats(dict(chosen), dict(presented))


class ModelBackendStatusView(generic.TemplateView):
    """Staff page showing the state of every configured model backend.

    Per model: enabled/disabled, provider, configured (friendly) name,
    internal/wire key, whether it is registered in the router pools that feed
    the selection UI and recognized by usage reporting, the latest health
    check outcome (category, latency, timestamp, sanitized error), and the
    last time the model actually produced a stored generation
    (ProposedAppeal / ChooserCandidate rows).

    Enumeration reuses the health-check module's configuration classification
    but performs NO model invocations — this page must stay cheap to load.
    Run ``python manage.py check_model_backends`` (or deploy) to refresh the
    health data.
    """

    template_name = "model_backend_status.html"

    def get_context_data(self, **kwargs):
        from fighthealthinsurance.ml import model_health_check as mhc

        ctx = super().get_context_data(**kwargs)

        static_results, checkable = mhc.enumerate_backend_checks()
        entries = list(static_results) + [pending for pending, _ in checkable]
        entries.sort(key=lambda r: (not r.enabled, r.provider, r.model_name))

        names = [r.model_name for r in entries]
        latest_checks = self._latest_check_by_model(names)
        last_generation = self._last_generation_by_model(names)

        rows: List[Dict[str, Any]] = []
        for r in entries:
            check = latest_checks.get(r.model_name)
            rows.append(
                {
                    "provider": r.provider,
                    "model_name": r.model_name,
                    "internal_name": r.internal_name,
                    "enabled": r.enabled,
                    # Static category from configuration classification (e.g.
                    # NOT_CONFIGURED / DISABLED / FAIL_MISSING_CREDENTIALS);
                    # empty for backends that need a live probe to judge.
                    "config_category": (
                        r.category if r.category != mhc.CATEGORY_OTHER else ""
                    ),
                    "config_detail": r.error,
                    "ui_registered": r.ui_registered,
                    "reporting_registered": r.reporting_registered,
                    "last_check": check,
                    "last_generation": last_generation.get(r.model_name),
                }
            )

        ctx["title"] = "Model Backend Status"
        ctx["rows"] = rows
        ctx["healthy_count"] = sum(
            1 for row in rows if row["last_check"] is not None and row["last_check"].ok
        )
        return ctx

    @staticmethod
    def _latest_check_by_model(
        names: List[str],
    ) -> Dict[str, ModelBackendHealthCheckResult]:
        """Most recent health-check row per model name (one query, newest
        first, first-seen wins)."""
        latest: Dict[str, ModelBackendHealthCheckResult] = {}
        qs = ModelBackendHealthCheckResult.objects.filter(
            model_name__in=names
        ).order_by("-created_at")[:2000]
        for row in qs:
            if row.model_name not in latest:
                latest[row.model_name] = row
        return latest

    @staticmethod
    def _last_generation_by_model(
        names: List[str],
    ) -> Dict[str, datetime.datetime]:
        """Latest stored generation per model across ProposedAppeal and
        ChooserCandidate — evidence the model was actually invoked (and its
        metadata persisted) in a real flow."""
        from django.db.models import Max

        last: Dict[str, datetime.datetime] = {}
        # .order_by() clears any Meta ordering, which would otherwise leak
        # into the GROUP BY and break the aggregation.
        for name, ts in (
            ProposedAppeal.objects.filter(model_name__in=names)
            .order_by()
            .values_list("model_name")
            .annotate(latest=Max("created_at"))
        ):
            if ts is not None:
                last[name] = ts
        for name, ts in (
            ChooserCandidate.objects.filter(model_name__in=names)
            .order_by()
            .values_list("model_name")
            .annotate(latest=Max("created_at"))
        ):
            if ts is not None and (name not in last or ts > last[name]):
                last[name] = ts
        return last


def _intro_action_dispatch() -> (
    Dict[str, Tuple[Callable[..., Any], Callable[[str, str], int], str]]
):
    """Send/queue dispatch shared by the pro-connector intro flows.

    Maps each accepted action to (deliver-now-or-enqueue helper, matching
    per-email mark helper, past-tense verb for logs/notices). One table so the
    full processing workflow and the quick-intro view can never disagree about
    what an action does. Built at call time, not import time, so tests (and
    anything else) patching this module's helper names still take effect.
    """
    return {
        "send": (send_proconnector_intro_email, mark_email_sent, "sent"),
        "queue": (queue_proconnector_intro_email, mark_email_queued, "queued"),
    }


class ProConnectorProcessView(View):
    """Staff workflow to introduce interested professionals to Cofactor AI.

    After refocusing FHI on its consumer mission, we have a sourcing agreement
    to introduce interested professionals (who may be a fit) to Cofactor AI.
    This view shows one unprocessed ``InterestedProfessional`` at a time with all
    known details, research links, and an AI-drafted (editable) intro email.
    Staff either send the (edited) email -- which CCs the professional contact
    address and records the send -- queue it to go out during the recipient's
    likely business hours, or skip the record. Any of those actions advances to
    the next unprocessed record. Staff can also send a TEST-marked copy of the
    draft to a test address first; that emails nobody else, records nothing on
    the record, and stays on the same record. Nothing is sent automatically.

    The professional's mailing address is editable here too: signups rarely
    include one, so staff look it up (the page offers an address search link)
    and save it on the record, which is the recipient address the printable
    intro letter prints. Saving an address stays on the current record; every
    other action persists a pending address edit on its way past, so the lookup
    is never lost to whichever button is pressed next.
    """

    template_name = "proconnector.html"

    def _render_record(
        self,
        request,
        pro: InterestedProfessional,
        *,
        draft: Optional[str] = None,
        subject: Optional[str] = None,
        skip_reason: str = "",
        test_email: Optional[str] = None,
        address: Optional[str] = None,
        error: Optional[str] = None,
        notice: Optional[str] = None,
        status: int = 200,
    ) -> HttpResponse:
        """Render the processing page for a single record.

        ``draft`` is generated via AI only when not supplied so that re-renders
        after a validation/send error preserve the staff member's edits.
        ``test_email`` similarly falls back to whatever was posted (preserving
        the field across validation errors), then to the staff member's own
        address so the "send test" button works without retyping it.
        ``address`` defaults to what is on the record: every POST persists a
        valid address edit before dispatching (see :meth:`post`), so error
        re-renders show the saved value without each call site passing it --
        only a *rejected* address is passed back explicitly, so staff can fix
        the text they typed rather than the text they replaced.
        """
        if draft is None:
            draft = generate_intro_email(pro)
        if test_email is None:
            test_email = (request.POST.get("test_email") or "").strip() or (
                getattr(request.user, "email", "") or ""
            )
        if address is None:
            address = pro.address or ""
        links = build_search_links(pro)
        context = {
            "title": "Pro Connector",
            "pro": pro,
            "email_body": draft,
            "subject": subject or PROCONNECTOR_INTRO_SUBJECT,
            # The email CCs both the professional contact address and Cofactor
            # AI; the printable letter can only name the professional contact.
            # cofactor_cc_email is None when the Cofactor CC is disabled, so the
            # template doesn't promise a CC that isn't happening.
            "cc_emails": default_intro_cc_recipients(),
            "cofactor_cc_email": get_cofactor_cc_email(),
            "cofactor_cc_problem": cofactor_cc_problem(),
            "contact_email": get_professional_cc_email(),
            "google_search_url": links["google"],
            "linkedin_search_url": links["linkedin"],
            "google_address_search_url": links["google_address"],
            "skip_reason": skip_reason,
            "test_email": test_email,
            # The mailing address is editable here (and only here) so staff can
            # record what the address lookup turns up; the printable letter
            # reads it straight off the record.
            "address": address,
            "address_max_length": address_max_length(),
            "error": error,
            "notice": notice,
            "remaining_count": remaining_interested_professionals_count(),
            "send_window_hint": describe_send_window(pro.phone_number),
        }
        return render(request, self.template_name, context, status=status)

    def get(self, request) -> HttpResponse:
        pro = get_next_interested_professional()
        if pro is None:
            return render(
                request,
                self.template_name,
                {"title": "Pro Connector", "pro": None, "remaining_count": 0},
            )
        return self._render_record(request, pro)

    def post(self, request) -> HttpResponse:
        action = request.POST.get("action")
        pro_id = request.POST.get("interested_professional_id")
        pro = None
        if pro_id:
            try:
                pro = InterestedProfessional.objects.filter(pk=int(pro_id)).first()
            except (TypeError, ValueError):
                pro = None
        if pro is None:
            # The record vanished (deleted, bad id, or already processed in
            # another tab). Just advance to whatever is next.
            return redirect("proconnector_process")

        if pro.proconnector_attempted or pro.proconnector_skipped or pro.unsubscribed:
            # Already processed (stale tab, back button, or double submit) or
            # unsubscribed while the record sat open in a tab. Don't send or
            # overwrite; just advance. claim_email_for_send re-checks all three
            # atomically, so this is UX, not the safety net.
            return redirect("proconnector_process")

        # Persist an edited mailing address before dispatching, whichever button
        # was pressed: send/queue/skip advance to the next record, so an address
        # staff looked up and typed would otherwise be silently discarded by the
        # very actions they reach for next. Only the address is written here --
        # nothing about this claims or processes the record.
        address_response = self._save_submitted_address(request, pro)
        if address_response is not None:
            return address_response

        if action == "save_address":
            # Stay on this record: the point of saving an address is to then
            # print the letter for the professional in front of you.
            return self._render_record(
                request,
                pro,
                **self._posted_edits(request),
                notice=(
                    "Mailing address saved; the printable letter will use it."
                    if pro.address
                    else "Mailing address cleared; the letter will print blank "
                    "guide lines to write one on."
                ),
            )

        if action == "skip":
            skip_reason = (request.POST.get("skip_reason") or "").strip()
            # Resolve every signup sharing this email so duplicates don't return.
            # Conditional on still-unprocessed: if another session already
            # sent/queued it, mark returns 0 and we just advance rather than
            # forcing a contradictory sent+skipped state.
            if mark_email_skipped(pro.email, skip_reason) == 0:
                return redirect("proconnector_process")
            logger.info(
                f"Staff user {request.user.username} skipped pro-connector intro for "
                f"InterestedProfessional {pro.id} ({mask_email_for_logging(pro.email)})"
            )
            return redirect("proconnector_process")

        # "send_test" previews the draft in a real inbox: it emails only the
        # staff-supplied test address with TEST markers and never touches the
        # record, so the intro still shows as unsent and stays in the queue.
        if action == "send_test":
            return self._handle_send_test(request, pro)

        # "send" delivers now; "queue" defers to the recipient's likely business
        # hours. They share validation and edit-preserving error handling.
        dispatch = _intro_action_dispatch()
        if action in dispatch:
            validated = self._validated_intro(request, pro)
            if isinstance(validated, HttpResponse):
                return validated
            body, subject, skip_reason = validated
            deliver, mark, verb = dispatch[action]
            failed_msg = f"Failed to {action} the email."
            # Atomically claim the address before sending. Two staff sessions are
            # handed the same next record, so without this both could pass the
            # already-processed check above and double-send; losing the claim
            # means another request already handled it, so just advance.
            if claim_email_for_send(pro.email) == 0:
                return redirect("proconnector_process")
            try:
                deliver(pro, subject=subject, body=body)
            except Exception as e:
                logger.opt(exception=True).error(
                    f"Failed to {action} pro-connector intro to "
                    f"{mask_email_for_logging(pro.email)}: {e}"
                )
                # Release the claim so staff can retry the record. Keep the
                # exception detail in the logs (above) and show a generic message
                # so internal details aren't surfaced in the UI.
                release_email_claim(pro.email)
                return self._render_record(
                    request,
                    pro,
                    draft=body,
                    subject=subject,
                    skip_reason=skip_reason,
                    error=f"{failed_msg} The error has been logged; please try again.",
                    status=500,
                )
            # Record on every signup sharing this email so duplicate records are
            # resolved together and never resurface in the queue.
            mark(pro.email, body)
            logger.info(
                f"Staff user {request.user.username} {verb} pro-connector intro to "
                f"InterestedProfessional {pro.id} ({mask_email_for_logging(pro.email)})"
            )
            return redirect("proconnector_process")

        # Unknown / missing action -- re-render the current record.
        return self._render_record(request, pro, error="Unknown action.", status=400)

    @staticmethod
    def _posted_edits(request) -> Dict[str, Any]:
        """The staff member's typed intro fields, for an edit-preserving re-render.

        The body is passed through unstripped, and as ``None`` when the field is
        absent from the POST entirely, so :meth:`_render_record` pays for a
        fresh AI draft only when there was genuinely nothing typed to preserve.
        """
        return {
            "draft": request.POST.get("email_body"),
            "subject": (request.POST.get("subject") or "").strip()
            or PROCONNECTOR_INTRO_SUBJECT,
            "skip_reason": (request.POST.get("skip_reason") or "").strip(),
        }

    def _save_submitted_address(
        self, request, pro: InterestedProfessional
    ) -> Optional[HttpResponse]:
        """Store the posted mailing address on ``pro``, or return an error page.

        Returns ``None`` when there is nothing to do or the address was saved,
        an error ``HttpResponse`` (this record re-rendered, every edit
        preserved, including the rejected address itself) when the address will
        not fit the column, and a redirect to the next record when the write is
        lost -- the record was processed, unsubscribed, or deleted between the
        eligibility guard in :meth:`post` and the UPDATE. ``pro.address`` is
        updated in memory on a successful write, so the same request's address
        lookup link, "known information" table, and letter card all reflect
        what was just saved.
        """
        posted = request.POST.get("address")
        if posted is None:
            # No address field in the POST at all (an older tab, a scripted
            # submit). Leave whatever is on the record alone.
            return None
        address = clean_address(posted)
        problem = address_problem(address)
        if problem is not None:
            return self._render_record(
                request,
                pro,
                **self._posted_edits(request),
                address=posted,
                error=problem,
                status=400,
            )
        if address != (pro.address or ""):
            if save_address(pro, address) == 0:
                # Another session processed (or deleted) the record in the
                # window since the guard above. Advance rather than claim a
                # save that did not happen -- whichever action was pressed
                # would lose its own claim moments later anyway.
                return redirect("proconnector_process")
            pro.address = address
        return None

    @staticmethod
    def _intro_form_problem(body: str, subject: str) -> Optional[str]:
        """First problem with the editable intro fields, or ``None``.

        Shared by the real send/queue path and the test-send path so a draft
        that previews cleanly is exactly one that can be sent: same empty-body
        rule, same wording rules -- partner framing (body *or* subject) and the
        required compensation disclosure -- run against the *final* (possibly
        hand-edited) values, not just the AI draft, and the same length cap
        matching the scheduled-email column so the immediate and queued paths
        behave identically.
        """
        subject_max = ScheduledEmail._meta.get_field("subject").max_length
        if not body:
            return "Email body cannot be empty."
        if subject_max is not None and len(subject) > subject_max:
            return f"Subject is too long (max {subject_max} characters)."
        return intro_wording_problem(body) or subject_wording_problem(subject)

    def _validated_intro(
        self, request, pro: InterestedProfessional
    ) -> Union[Tuple[str, str, str], HttpResponse]:
        """Validate the submitted intro for send/queue.

        Returns ``(body, subject, skip_reason)`` when valid, or an error
        ``HttpResponse`` (re-rendering the record with the staff edits preserved)
        when the shared form checks fail (see :meth:`_intro_form_problem`) or
        the recipient address is unsendable.
        """
        body = (request.POST.get("email_body") or "").strip()
        subject = (
            request.POST.get("subject") or ""
        ).strip() or PROCONNECTOR_INTRO_SUBJECT
        skip_reason = (request.POST.get("skip_reason") or "").strip()
        error = _intro_send_problem(pro, body, subject)
        if error:
            return self._render_record(
                request,
                pro,
                draft=body or request.POST.get("email_body", ""),
                subject=subject,
                skip_reason=skip_reason,
                error=error,
                status=400,
            )
        return body, subject, skip_reason

    def _handle_send_test(self, request, pro: InterestedProfessional) -> HttpResponse:
        """Send a TEST-marked preview of the draft to the staff-supplied address.

        Runs the same form validation as a real send (so a draft that previews
        cleanly is exactly one that can be sent) but gates on the *test*
        address being sendable -- the professional is not emailed -- and never
        writes to the record: no claim, no attempted/sent/body updates, so the
        intro stays unsent and remains in the queue. Success re-renders the
        same record (edits preserved) rather than advancing.
        """
        body = (request.POST.get("email_body") or "").strip()
        subject = (
            request.POST.get("subject") or ""
        ).strip() or PROCONNECTOR_INTRO_SUBJECT
        skip_reason = (request.POST.get("skip_reason") or "").strip()
        test_email = (request.POST.get("test_email") or "").strip()
        error = self._intro_form_problem(body, subject)
        if error is None:
            if not test_email:
                error = "Enter a test email address to send the test to."
            elif not is_sendable_email(test_email):
                error = f"{test_email} is not a sendable address; cannot send the test."
        if error:
            return self._render_record(
                request,
                pro,
                draft=body or request.POST.get("email_body", ""),
                subject=subject,
                skip_reason=skip_reason,
                error=error,
                status=400,
            )
        try:
            send_proconnector_test_email(
                pro, subject=subject, body=body, test_email=test_email
            )
        except Exception as e:
            logger.opt(exception=True).error(
                f"Failed to send pro-connector TEST intro for "
                f"InterestedProfessional {pro.id} to "
                f"{mask_email_for_logging(test_email)}: {e}"
            )
            return self._render_record(
                request,
                pro,
                draft=body,
                subject=subject,
                skip_reason=skip_reason,
                error=(
                    "Failed to send the test email. The error has been logged; "
                    "please try again."
                ),
                status=500,
            )
        logger.info(
            f"Staff user {request.user.username} sent pro-connector TEST intro for "
            f"InterestedProfessional {pro.id} ({mask_email_for_logging(pro.email)}) "
            f"to {mask_email_for_logging(test_email)}"
        )
        return self._render_record(
            request,
            pro,
            draft=body,
            subject=subject,
            skip_reason=skip_reason,
            notice=(
                f"Test email sent to {test_email}. Nothing was recorded on this "
                "record -- the real intro has NOT been sent."
            ),
        )


def _intro_send_problem(
    pro: InterestedProfessional, body: str, subject: str
) -> Optional[str]:
    """First problem blocking a *real* send/queue of the intro to ``pro``, or
    ``None``.

    The full pre-claim validation chain shared by every real-send path (the
    processing workflow's ``_validated_intro`` and the quick-intro view), so a
    rule added for one can never silently miss the other: the editable-field
    rules (:meth:`ProConnectorProcessView._intro_form_problem`), then the
    recipient being sendable, then the Cofactor CC configuration -- the latter
    because a misconfigured CC makes the send helpers raise, and checking
    before the record is claimed means staff see the actual reason and the
    record stays in the queue for a retry once the setting is fixed.
    """
    problem = ProConnectorProcessView._intro_form_problem(body, subject)
    if problem is not None:
        return problem
    if not is_sendable_email(pro.email):
        return f"{pro.email} is not a sendable address; cannot send."
    return cofactor_cc_problem()


class ProConnectorLetterView(View):
    """Print-friendly physical intro letter for an interested professional.

    A companion to :class:`ProConnectorProcessView`: for professionals worth
    reaching by mail in addition to email, this renders the Cofactor AI
    introduction as a physical business letter that staff open, print, and mail.
    It shares the email's call to action -- reach out to the professional
    contact address to schedule a demo or learn more -- just formatted as a
    letter. This view only renders the letter; it
    never claims, sends, or records anything on the professional's record, so it
    can be opened without affecting the processing queue.
    """

    template_name = "proconnector_letter.html"

    def get(self, request, pro_id: int) -> HttpResponse:
        pro = InterestedProfessional.objects.filter(pk=pro_id).first()
        if pro is None:
            # Bad / stale id (deleted record, hand-edited URL). Send staff back
            # to the processing page rather than 404 on a transient link.
            return redirect("proconnector_process")
        context = {
            # Browsers seed the print-to-PDF file name from the page title, so
            # this names the recipient and their organization; staff printing a
            # batch get distinguishable files instead of a stack of identical
            # ones.
            "title": build_letter_document_title(pro),
            "pro": pro,
            # The letter's blocks, laid out by the template so the printed page
            # paginates like a letter -- see build_intro_letter_blocks.
            "letter": build_intro_letter_blocks(pro),
            # Stripped here because a whitespace-only address is an absent one:
            # it must reach the guide lines, the way the address lookup on the
            # processing page already treats it (see build_address_search_link).
            "recipient_address": (pro.address or "").strip(),
            "contact_email": get_professional_cc_email(),
        }
        return render(request, self.template_name, context)


class ProConnectorQuickIntroView(View):
    """One-press Cofactor AI introduction for a specific interested professional.

    Linked (as a button) from the team notification email each new
    interested-professional signup sends: staff press it, land here behind the
    staff login, and confirm with a single press -- the intro email is drafted
    automatically (AI-personalized, falling back to the approved base email)
    and sent and recorded exactly like a send from the full processing
    workflow (:class:`ProConnectorProcessView`). The GET only previews the
    draft; the send itself is a POST, so mail scanners prefetching the email's
    links can never trigger an introduction.

    The same eligibility rules as the processing queue apply (see
    :func:`quick_intro_block_reason`): an already-sent/queued/skipped record,
    an unsubscribed professional, or a filtered test/spam signup gets a status
    page instead of a send button, so the email link can't bypass the queue.
    """

    template_name = "proconnector_quick_intro.html"

    def _render(
        self,
        request,
        pro: InterestedProfessional,
        *,
        draft: Optional[str] = None,
        error: Optional[str] = None,
        notice: Optional[str] = None,
        status: int = 200,
    ) -> HttpResponse:
        """Render the quick-intro page for ``pro``.

        The AI draft is only generated when the record is actually sendable
        (no block reason) and no ``draft`` was supplied -- re-renders after an
        error preserve the posted body, and blocked records (including
        just-sent ones) show their stored state instead of paying for a fresh
        draft.
        """
        block_reason = quick_intro_block_reason(pro)
        if draft is None and block_reason is None:
            draft = generate_intro_email(pro)
        context = {
            "title": "Quick Cofactor AI Introduction",
            "pro": pro,
            "block_reason": block_reason,
            "email_body": draft,
            "subject": PROCONNECTOR_INTRO_SUBJECT,
            "cc_emails": default_intro_cc_recipients(),
            "cofactor_cc_problem": cofactor_cc_problem(),
            "error": error,
            "notice": notice,
            "send_window_hint": describe_send_window(pro.phone_number),
        }
        return render(request, self.template_name, context, status=status)

    def get(self, request, pro_id: int) -> HttpResponse:
        pro = InterestedProfessional.objects.filter(pk=pro_id).first()
        if pro is None:
            # Bad / stale id (deleted record, hand-edited URL). Send staff to
            # the processing queue rather than 404 on a link from an email.
            return redirect("proconnector_process")
        return self._render(request, pro)

    def post(self, request, pro_id: int) -> HttpResponse:
        pro = InterestedProfessional.objects.filter(pk=pro_id).first()
        if pro is None:
            return redirect("proconnector_process")
        action = request.POST.get("action")
        body = (request.POST.get("email_body") or "").strip()
        dispatch = _intro_action_dispatch()
        if action not in dispatch:
            # No default action: a POST that lost the submit button's value (a
            # scripted form.submit(), a mangled resubmission) must fail safe,
            # never fall through to an irreversible send.
            return self._render(
                request, pro, draft=body, error="Unknown action.", status=400
            )
        if quick_intro_block_reason(pro) is not None:
            # Already handled (double press, another session, an unsubscribe)
            # or filtered; the render shows the reason instead of a button.
            # claim_email_for_send below re-checks atomically -- this is UX.
            return self._render(request, pro)
        # The previewed draft round-trips through the (editable) form, so what
        # was shown (or hand-edited) is exactly what is sent. An empty body is
        # rejected like the full workflow's -- never auto-drafted -- so content
        # nobody reviewed can never go out.
        subject = PROCONNECTOR_INTRO_SUBJECT
        error = _intro_send_problem(pro, body, subject)
        if error:
            return self._render(request, pro, draft=body, error=error, status=400)

        deliver, mark, verb = dispatch[action]
        # Atomically claim the address (shared with the processing workflow) so
        # a double press here, or a concurrent send from the queue view, can
        # never double-introduce; losing the claim means someone else just
        # handled it.
        if claim_email_for_send(pro.email) == 0:
            # Re-fetch for the freshest state; the notice tells staff this
            # press did nothing even in the narrow race where the competing
            # send failed and released the claim (block reason gone again).
            pro = InterestedProfessional.objects.filter(pk=pro_id).first()
            if pro is None:
                return redirect("proconnector_process")
            return self._render(
                request,
                pro,
                draft=body,
                notice=(
                    "This press made no changes -- the record was just "
                    "handled in another session. Its current state is shown "
                    "below."
                ),
            )
        try:
            deliver(pro, subject=subject, body=body)
        except Exception as e:
            logger.opt(exception=True).error(
                f"Failed to {action} quick pro-connector intro to "
                f"{mask_email_for_logging(pro.email)}: {e}"
            )
            release_email_claim(pro.email)
            return self._render(
                request,
                pro,
                draft=body,
                error=(
                    f"Failed to {action} the email. The error has been "
                    "logged; please try again."
                ),
                status=500,
            )
        mark(pro.email, body)
        logger.info(
            f"Staff user {request.user.username} {verb} pro-connector intro to "
            f"InterestedProfessional {pro.id} ({mask_email_for_logging(pro.email)}) "
            "via the quick-intro flow"
        )
        # Re-fetch rather than refresh_from_db(): a concurrent delete of the
        # record must not 500 after an email that already went out.
        pro = InterestedProfessional.objects.filter(pk=pro_id).first()
        if pro is None:
            return redirect("proconnector_process")
        notice = (
            f"Introduction sent to {pro.email}."
            if action == "send"
            else (
                f"Introduction queued to send to {pro.email} during their "
                "likely business hours."
            )
        )
        return self._render(request, pro, notice=notice)


class _CSVEcho:
    """A file-like object that returns each written row, for CSV streaming."""

    def write(self, value: str) -> str:
        return value


def _csv_safe(value: Any) -> str:
    """Neutralize CSV formula injection in user-controlled fields.

    Spreadsheet apps (Excel/Sheets) interpret a cell beginning with =, +, -, or
    @ as a formula. They also trim leading whitespace/control characters first,
    so a payload like " =1+1" or "\\n=..." must be caught by its first
    *non-whitespace* character rather than its literal first character. Several
    exported fields come from the public, unauthenticated signup form, so prefix
    any such value with a single quote to force it to render as text. ``None``
    becomes an empty cell.
    """
    if value is None:
        return ""
    text = str(value)
    if text.lstrip()[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


class ProConnectorExtractCSVView(View):
    """Staff CSV export of interested professionals, excluding test/spam signups.

    Dumps the info we have on each (non-filtered) interested professional --
    including pro-connector processing state -- streamed as CSV. The same
    test/spam filter used by the processing queue (testing@, .ru/.ua, names
    containing a URL) is applied here.
    """

    columns = [
        "id",
        "name",
        "email",
        "business_name",
        "phone_number",
        "address",
        "job_title_or_provider_type",
        "most_common_denial",
        "comments",
        "paid",
        "clicked_for_paid",
        "signup_date",
        "mod_date",
        "proconnector_attempted",
        "proconnector_sent_at",
        "proconnector_skipped",
        "proconnector_skip_reason",
        "unsubscribed",
        "unsubscribed_at",
    ]

    def get(self, request) -> StreamingHttpResponse:
        qs = non_spam_interested_professionals().order_by("signup_date", "id")
        writer = csv.writer(_CSVEcho())

        def rows():
            yield writer.writerow(self.columns)
            for pro in qs.iterator():
                yield writer.writerow(
                    [_csv_safe(getattr(pro, column)) for column in self.columns]
                )

        response = StreamingHttpResponse(rows(), content_type="text/csv")
        response["Content-Disposition"] = (
            'attachment; filename="interested_professionals.csv"'
        )
        return response


def _temporal_ui_request(method: str, url: str, **kwargs: Any) -> requests.Response:
    """Single seam for the proxy's upstream call (patched in tests)."""
    return requests.request(method, url, **kwargs)


class TemporalUIProxyView(View):
    """Staff-only, read-only reverse proxy for the Temporal Web UI.

    The Temporal UI is a ClusterIP service with no auth of its own, so it is
    never exposed directly. This view serves it under ``/timbit/temporal/``
    behind the same staff login as the rest of the dashboard, forwarding only
    GET/HEAD to a fixed in-cluster upstream (``settings.TEMPORAL_UI_UPSTREAM``).

    The UI runs with ``TEMPORAL_UI_PUBLIC_PATH=/timbit/temporal`` so its assets
    and API calls come back through this view, and with
    ``TEMPORAL_DISABLE_WRITE_ACTIONS`` so nothing state-changing is offered.
    GET-only means that, even with no CSRF check on the proxied paths, no
    workflow action can pass through here.
    """

    http_method_names = ["get", "head"]
    PUBLIC_PATH = "/timbit/temporal"
    # Response headers worth passing back; everything else (hop-by-hop,
    # content-length, content-encoding) is dropped since we re-stream.
    PASS_HEADERS = ("Cache-Control", "ETag", "Last-Modified", "Content-Disposition")

    def get(self, request, path: str = "") -> HttpResponseBase:
        return self._proxy(request, path)

    def head(self, request, path: str = "") -> HttpResponseBase:
        return self._proxy(request, path)

    def _proxy(self, request, path: str) -> HttpResponseBase:
        from django.conf import settings

        if request.path.rstrip("/") == self.PUBLIC_PATH and not request.path.endswith(
            "/"
        ):
            return redirect(self.PUBLIC_PATH + "/")
        if ".." in path.split("/") or path.startswith("/"):
            return HttpResponse("bad path", status=400, content_type="text/plain")

        upstream = getattr(
            settings, "TEMPORAL_UI_UPSTREAM", "http://temporal-web:8080"
        ).rstrip("/")
        url = f"{upstream}{self.PUBLIC_PATH}/{path}"
        if request.META.get("QUERY_STRING"):
            url = f"{url}?{request.META['QUERY_STRING']}"

        headers = {"Accept-Encoding": "identity"}
        for name in ("Accept", "Accept-Language", "If-None-Match", "If-Modified-Since"):
            value = request.headers.get(name)
            if value:
                headers[name] = value

        try:
            upstream_resp = _temporal_ui_request(
                request.method,
                url,
                headers=headers,
                stream=True,
                timeout=(3, 30),
                allow_redirects=False,
            )
        except requests.RequestException as e:
            logger.warning(f"Temporal UI upstream unreachable: {e}")
            return HttpResponse(
                "Temporal UI is not reachable from the web pod right now.",
                status=502,
                content_type="text/plain",
            )

        def stream():
            # Close the upstream response even if the client stops reading
            # partway: Django closes this generator on response close, which
            # runs the finally, so a pooled requests connection is never
            # left behind.
            try:
                yield from upstream_resp.iter_content(chunk_size=64 * 1024)
            finally:
                upstream_resp.close()

        response = StreamingHttpResponse(
            stream(),
            status=upstream_resp.status_code,
            content_type=upstream_resp.headers.get(
                "Content-Type", "application/octet-stream"
            ),
        )
        for name in self.PASS_HEADERS:
            if name in upstream_resp.headers:
                response[name] = upstream_resp.headers[name]
        location = upstream_resp.headers.get("Location")
        if location:
            # Keep redirects on our side of the proxy.
            response["Location"] = (
                location[len(upstream) :] if location.startswith(upstream) else location
            )
        return response

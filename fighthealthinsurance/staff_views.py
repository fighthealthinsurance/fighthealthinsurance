import csv
import datetime
import json
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from django.db import transaction
from django.db.models import Count
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View, generic

import ray
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
from fighthealthinsurance.ml.model_identity import (
    LEGACY_UNATTRIBUTED_LABEL,
    normalize_model_label,
)
from fighthealthinsurance.proconnector import (
    PROCONNECTOR_INTRO_SUBJECT,
    build_search_links,
    claim_email_for_send,
    generate_intro_email,
    get_next_interested_professional,
    get_professional_cc_email,
    intro_wording_problem,
    mark_email_queued,
    mark_email_sent,
    mark_email_skipped,
    release_email_claim,
    non_spam_interested_professionals,
    queue_proconnector_intro_email,
    subject_wording_problem,
    remaining_interested_professionals_count,
    send_proconnector_intro_email,
    send_proconnector_test_email,
)
from fighthealthinsurance.type_utils import User
from fighthealthinsurance.utils import mask_email_for_logging


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

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "System Status"
        ctx["generated_at"] = timezone.now()
        ctx["models"] = self._model_status()
        ctx["actors"] = self._actor_status()
        ctx["fax"] = self._fax_backend_status()
        ctx["fax_queue"] = self._fax_queue_status()
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
        """Counts of queued / pending / failed faxes from FaxesToSend.

        Mirrors what the fax actor acts on: it sends faxes that are
        ``should_send=True, sent=False`` and at least an hour old.
        """
        out: Dict[str, Any] = {"ok": True, "error": None}
        try:
            from fighthealthinsurance.models import FaxesToSend

            now = timezone.now()
            one_hour_ago = now - datetime.timedelta(hours=1)
            week_ago = now - datetime.timedelta(days=7)

            unsent = FaxesToSend.objects.filter(sent=False)
            out["unsent_total"] = unsent.count()
            out["ready_queued"] = unsent.filter(should_send=True).count()
            out["due_now"] = unsent.filter(
                should_send=True, date__lt=one_hour_ago
            ).count()
            out["awaiting_confirmation"] = unsent.filter(should_send=False).count()
            out["in_flight"] = unsent.filter(
                attempting_to_send_as_of__isnull=False
            ).count()
            out["failures_recent"] = FaxesToSend.objects.filter(
                sent=True, fax_success=False, date__gte=week_ago
            ).count()
        except Exception as e:
            logger.opt(exception=True).error("Error computing fax queue status")
            out["ok"] = False
            out["error"] = str(e)
        return out

    @staticmethod
    def _storage_status() -> Dict[str, Any]:
        """Whether the external (encrypted) storage backend is reachable."""
        out: Dict[str, Any] = {"ok": False, "error": None}
        try:
            from django.conf import settings
            from stopit import ThreadingTimeout as Timeout

            es = settings.EXTERNAL_STORAGE
            with Timeout(3.0):
                es.listdir("./")
                out["ok"] = True
                return out
            out["error"] = "timeout"
        except Exception as e:
            logger.opt(exception=True).warning("External storage health check failed")
            out["error"] = str(e)
        return out


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


class SendMailingListMailView(generic.FormView):
    """A view to send emails to all mailing list subscribers."""

    template_name = "send_mailing_list_mail.html"
    form_class = core_forms.SendMailingListMailForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["subscriber_count"] = MailingListSubscriber.objects.count()
        return context

    def form_valid(self, form):
        subject = form.cleaned_data.get("subject")
        html_content = form.cleaned_data.get("html_content")
        text_content = form.cleaned_data.get("text_content")
        test_email = form.cleaned_data.get("test_email")

        try:
            # Use ray actor for sending emails
            actor = mailing_list_actor_ref.get
            future = actor.send_mailing_list_email.remote(
                subject, html_content, text_content, test_email
            )
            sent_count, failed_count, blocked_count = ray.get(future)

            if test_email:
                masked_email = mask_email_for_logging(test_email)
                return HttpResponse(f"Test email sent successfully to {masked_email}")
            else:
                return HttpResponse(
                    f"Mailing list email sent. Success: {sent_count}, Failed: {failed_count}, Blocked: {blocked_count}"
                )
        except Exception as e:
            logger.opt(exception=True).error(f"Error sending mailing list email: {e}")
            return HttpResponse(
                f"Error sending mailing list email: {str(e)}", status=500
            )


# Bucket label for chosen ProposedAppeal rows whose model_name is NULL and
# whose created_at is set — i.e. a pick recorded after model tracking began
# that still couldn't be attributed back to a generated draft (heavy edit
# with multiple models in play, or the share-appeal flow). Surfacing them
# keeps the dashboard's total-picks number honest. Rows predating tracking
# (created_at NULL) or explicitly stamped by the backfill are reported under
# LEGACY_UNATTRIBUTED_LABEL instead so legacy gaps stay distinguishable.
UNKNOWN_MODEL_LABEL = "(unattributed)"


def _merge_stats(
    chosen: Dict[str, int], presented: Dict[str, int]
) -> List[Dict[str, Any]]:
    """Combine per-model chosen + presented counts into a sorted list of dicts.

    ``win_rate`` is chosen/presented as a percentage, or ``None`` when the
    model has no presented count (no denominator — e.g. legacy chosen rows
    without presentation records). Templates render ``None`` as an em dash;
    it must never be displayed as 0.0%.
    """
    rows: List[Dict[str, Any]] = []
    for model_name in set(chosen) | set(presented):
        c = chosen.get(model_name, 0)
        p = presented.get(model_name, 0)
        win_rate = (c / p * 100.0) if p > 0 else None
        rows.append(
            {
                "model_name": model_name,
                "chosen": c,
                "presented": p,
                "win_rate": win_rate,
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
        # Keep chosen rows with model_name=NULL in the chosen aggregation —
        # mark_proposal_chosen intentionally falls back to None when the
        # picked text can't be matched to a generated draft, and those are
        # still real user picks worth surfacing. NULL rows that predate model
        # tracking entirely (created_at NULL, pre-migration-0182) are
        # bucketed as LEGACY_UNATTRIBUTED_LABEL — matching what the backfill
        # stamps — while post-tracking rows fall under UNKNOWN_MODEL_LABEL,
        # so legacy gaps stay distinguishable from current attribution
        # misses.
        chosen_qs = ProposedAppeal.objects.filter(chosen=True)
        if since is not None:
            chosen_qs = chosen_qs.filter(created_at__gte=since)

        # Tie the presented universe to denials that were picked within
        # the window. We intentionally do NOT filter presented_qs by
        # created_at: a user can generate appeals on day 0 and pick one on
        # day 1, and a 1-day window anchored on the pick should still count
        # the drafts that were actually presented. Pass the subquery
        # straight into __in to avoid materializing a potentially huge id
        # list (Django keeps it as a SQL subquery). Drafts without a
        # model_name (pre-tracking) stay excluded: attributing them to any
        # bucket would fabricate a win-rate denominator we cannot back up,
        # so legacy/unattributed chosen rows deliberately report presented=0
        # and an em-dash win rate. Excluding LEGACY_UNATTRIBUTED_LABEL is
        # defensive — the backfill only stamps chosen rows, so no draft
        # should ever carry it.
        chosen_denial_ids = chosen_qs.values_list("for_denial_id", flat=True).distinct()
        presented_qs = ProposedAppeal.objects.filter(
            chosen=False,
            model_name__isnull=False,
            for_denial_id__in=chosen_denial_ids,
        ).exclude(model_name=LEGACY_UNATTRIBUTED_LABEL)

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
            label = normalize_model_label(name)
            if label is not None:
                presented[label] += count
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
        """
        if draft is None:
            draft = generate_intro_email(pro)
        if test_email is None:
            test_email = (request.POST.get("test_email") or "").strip() or (
                getattr(request.user, "email", "") or ""
            )
        links = build_search_links(pro)
        context = {
            "title": "Pro Connector",
            "pro": pro,
            "email_body": draft,
            "subject": subject or PROCONNECTOR_INTRO_SUBJECT,
            "cc_email": get_professional_cc_email(),
            "google_search_url": links["google"],
            "linkedin_search_url": links["linkedin"],
            "skip_reason": skip_reason,
            "test_email": test_email,
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

        if pro.proconnector_attempted or pro.proconnector_skipped:
            # Already processed (stale tab, back button, or double submit). Don't
            # re-send or overwrite; just advance.
            return redirect("proconnector_process")

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
        if action in ("send", "queue"):
            validated = self._validated_intro(request, pro)
            if isinstance(validated, HttpResponse):
                return validated
            body, subject, skip_reason = validated
            dispatch: Dict[
                str, Tuple[Callable[..., Any], Callable[[str, str], int], str]
            ] = {
                "send": (send_proconnector_intro_email, mark_email_sent, "sent"),
                "queue": (queue_proconnector_intro_email, mark_email_queued, "queued"),
            }
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
        error = self._intro_form_problem(body, subject)
        if error is None and not is_sendable_email(pro.email):
            error = f"{pro.email} is not a sendable address; cannot send."
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

import datetime
import json
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from django.db import transaction
from django.db.models import Count
from django.http import HttpResponse
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
    ProfessionalDomainRelation,
    ProfessionalUser,
    ProposedAppeal,
    UserDomain,
)
from fighthealthinsurance.email_utils import is_sendable_email
from fighthealthinsurance.partner_intro import (
    PARTNER_INTRO_SUBJECT,
    build_search_links,
    generate_intro_email,
    get_next_interested_professional,
    get_professional_cc_email,
    mark_email_sent,
    mark_email_skipped,
    remaining_interested_professionals_count,
    send_partner_intro_email,
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


# Bucket label for chosen ProposedAppeal rows whose model_name is NULL —
# i.e. the user picked something we couldn't attribute back to a generated
# draft (heavy edit, share-appeal flow, or a row predating the model_name
# field). Surfacing them keeps the dashboard's total-picks number honest.
UNKNOWN_MODEL_LABEL = "(unknown)"


def _merge_stats(
    chosen: Dict[str, int], presented: Dict[str, int]
) -> List[Dict[str, Any]]:
    """Combine per-model chosen + presented counts into a sorted list of dicts."""
    rows: List[Dict[str, Any]] = []
    for model_name in set(chosen) | set(presented):
        c = chosen.get(model_name, 0)
        p = presented.get(model_name, 0)
        win_rate = (c / p * 100.0) if p > 0 else 0.0
        rows.append(
            {
                "model_name": model_name,
                "chosen": c,
                "presented": p,
                "win_rate": win_rate,
            }
        )
    rows.sort(key=lambda r: (-r["chosen"], -r["win_rate"], r["model_name"]))
    return rows


class ModelUsageDashboardView(generic.TemplateView):
    """Staff dashboard showing which ML models users pick most often.

    Aggregates three signal sources across three time windows:
      * ProposedAppeal.chosen=True  - implicit pick from real denial flow
      * ChooserVote (kind=appeal_letter) - synthetic chooser appeal vote
      * ChooserVote (kind=chat_response) - synthetic chooser chat vote
    """

    template_name = "model_usage_dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        now = timezone.now()
        windows = [
            ("global", "All Time", None),
            ("1d", "Last 1 Day", now - datetime.timedelta(days=1)),
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
        # still real user picks worth surfacing. They are bucketed under
        # "(unknown)" below so they don't get silently dropped.
        chosen_qs = ProposedAppeal.objects.filter(chosen=True)
        if since is not None:
            chosen_qs = chosen_qs.filter(created_at__gte=since)

        # Tie the presented universe to denials that were picked within
        # the window. We intentionally do NOT filter presented_qs by
        # created_at: a user can generate appeals on day 0 and pick one on
        # day 1, and a 1-day window anchored on the pick should still count
        # the drafts that were actually presented. Pass the subquery
        # straight into __in to avoid materializing a potentially huge id
        # list (Django keeps it as a SQL subquery).
        chosen_denial_ids = chosen_qs.values_list("for_denial_id", flat=True).distinct()
        presented_qs = ProposedAppeal.objects.filter(
            chosen=False,
            model_name__isnull=False,
            for_denial_id__in=chosen_denial_ids,
        )

        chosen: Dict[str, int] = {}
        for name, count in chosen_qs.values_list("model_name").annotate(c=Count("id")):
            label = name if name is not None else UNKNOWN_MODEL_LABEL
            chosen[label] = chosen.get(label, 0) + count
        presented = {
            name: count
            for name, count in presented_qs.values_list("model_name").annotate(
                c=Count("id")
            )
        }
        return _merge_stats(chosen, presented)

    @staticmethod
    def _chooser_stats(
        kind: str, since: Optional[datetime.datetime]
    ) -> List[Dict[str, Any]]:
        chosen_qs = ChooserVote.objects.filter(chosen_candidate__kind=kind)
        if since is not None:
            chosen_qs = chosen_qs.filter(created_at__gte=since)
        chosen = {
            name: count
            for name, count in chosen_qs.values_list(
                "chosen_candidate__model_name"
            ).annotate(c=Count("id"))
        }

        # Presented: walk votes' presented_candidate_ids JSON lists into a
        # counter. We reuse chosen_qs (same filter) and call .iterator() so
        # the All Time window doesn't load every vote into a result cache.
        counter: Counter = Counter()
        for ids in chosen_qs.values_list(
            "presented_candidate_ids", flat=True
        ).iterator():
            if ids:
                counter.update(ids)
        cand_to_model = dict(
            ChooserCandidate.objects.filter(
                id__in=list(counter.keys()), kind=kind
            ).values_list("id", "model_name")
        )
        presented: Dict[str, int] = defaultdict(int)
        for cid, n in counter.items():
            mn = cand_to_model.get(cid)
            if mn:
                presented[mn] += n
        return _merge_stats(chosen, dict(presented))


class PartnerIntroProcessView(View):
    """Staff workflow to introduce interested professionals to Cofactor AI.

    After refocusing FHI on its consumer mission, we have a sourcing agreement
    to introduce interested professionals (who may be a fit) to Cofactor AI.
    This view shows one unprocessed ``InterestedProfessional`` at a time with all
    known details, research links, and an AI-drafted (editable) intro email.
    Staff either send the (edited) email -- which CCs the professional contact
    address and records the send -- or skip the record. Either action advances to
    the next unprocessed record. Nothing is sent automatically.
    """

    template_name = "partner_intro.html"

    def _render_record(
        self,
        request,
        pro: InterestedProfessional,
        *,
        draft: Optional[str] = None,
        subject: Optional[str] = None,
        skip_reason: str = "",
        error: Optional[str] = None,
        status: int = 200,
    ) -> HttpResponse:
        """Render the processing page for a single record.

        ``draft`` is generated via AI only when not supplied so that re-renders
        after a validation/send error preserve the staff member's edits.
        """
        if draft is None:
            draft = generate_intro_email(pro)
        links = build_search_links(pro)
        context = {
            "title": "Partner Introduction",
            "pro": pro,
            "email_body": draft,
            "subject": subject or PARTNER_INTRO_SUBJECT,
            "cc_email": get_professional_cc_email(),
            "google_search_url": links["google"],
            "linkedin_search_url": links["linkedin"],
            "skip_reason": skip_reason,
            "error": error,
            "remaining_count": remaining_interested_professionals_count(),
        }
        return render(request, self.template_name, context, status=status)

    def get(self, request) -> HttpResponse:
        pro = get_next_interested_professional()
        if pro is None:
            return render(
                request,
                self.template_name,
                {"title": "Partner Introduction", "pro": None, "remaining_count": 0},
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
            return redirect("partner_intro_process")

        if pro.partner_intro_attempted or pro.partner_intro_skipped:
            # Already processed (stale tab, back button, or double submit). Don't
            # re-send or overwrite; just advance.
            return redirect("partner_intro_process")

        if action == "skip":
            skip_reason = (request.POST.get("skip_reason") or "").strip()
            # Resolve every signup sharing this email so duplicates don't return.
            mark_email_skipped(pro.email, skip_reason)
            logger.info(
                f"Staff user {request.user.username} skipped partner intro for "
                f"InterestedProfessional {pro.id} ({mask_email_for_logging(pro.email)})"
            )
            return redirect("partner_intro_process")

        if action == "send":
            body = (request.POST.get("email_body") or "").strip()
            subject = (
                request.POST.get("subject") or ""
            ).strip() or PARTNER_INTRO_SUBJECT
            skip_reason = (request.POST.get("skip_reason") or "").strip()
            if not body:
                return self._render_record(
                    request,
                    pro,
                    draft=request.POST.get("email_body", ""),
                    subject=subject,
                    skip_reason=skip_reason,
                    error="Email body cannot be empty.",
                    status=400,
                )
            if not is_sendable_email(pro.email):
                return self._render_record(
                    request,
                    pro,
                    draft=body,
                    subject=subject,
                    skip_reason=skip_reason,
                    error=f"{pro.email} is not a sendable address; cannot send.",
                    status=400,
                )
            try:
                send_partner_intro_email(pro, subject=subject, body=body)
            except Exception as e:
                logger.opt(exception=True).error(
                    f"Failed to send partner intro to "
                    f"{mask_email_for_logging(pro.email)}: {e}"
                )
                # Do NOT mark attempted on failure; let staff retry the record.
                return self._render_record(
                    request,
                    pro,
                    draft=body,
                    subject=subject,
                    skip_reason=skip_reason,
                    error=f"Failed to send email: {e}",
                    status=500,
                )
            # Record the send on every signup sharing this email so duplicate
            # records are resolved together and never resurface in the queue.
            mark_email_sent(pro.email, body)
            logger.info(
                f"Staff user {request.user.username} sent partner intro to "
                f"InterestedProfessional {pro.id} ({mask_email_for_logging(pro.email)})"
            )
            return redirect("partner_intro_process")

        # Unknown / missing action -- re-render the current record.
        return self._render_record(request, pro, error="Unknown action.", status=400)

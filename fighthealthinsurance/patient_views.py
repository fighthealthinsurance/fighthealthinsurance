"""
Patient Dashboard Views

Views for logged-in patients to manage their appeals, call logs, and evidence.
This provides a "pure FHI patient view" distinct from professional/provider interfaces.
"""

from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import FileResponse, Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, TemplateView, UpdateView

from loguru import logger

from fighthealthinsurance.forms import (
    InsuranceCallLogForm,
    PatientEvidenceForm,
)
from fighthealthinsurance.models import (
    Appeal,
    Denial,
    InsuranceCallLog,
    PatientEvidence,
)
from fhi_users.models import PatientUser


class PatientRequiredMixin(LoginRequiredMixin):
    """
    Mixin that ensures the user is logged in and has a PatientUser record.
    Redirects to login if not authenticated, or to an error page if not a patient.
    """

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        # Check if user has a PatientUser record
        try:
            self.patient_user = PatientUser.objects.get(
                user=request.user, active=True
            )
        except PatientUser.DoesNotExist:
            # User is logged in but doesn't have a patient account
            # They might be a professional or need to create a patient profile
            logger.info(
                f"User {request.user.id} tried to access patient dashboard without patient record"
            )
            # For now, create a patient user automatically for logged-in users
            self.patient_user, created = PatientUser.objects.get_or_create(
                user=request.user,
                defaults={"active": True},
            )
            if created:
                logger.info(f"Created PatientUser for user {request.user.id}")

        return super().dispatch(request, *args, **kwargs)


class PatientDashboardView(PatientRequiredMixin, TemplateView):
    """
    Main dashboard view for patients showing their appeals, call logs, and evidence.
    """

    template_name = "patient_dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user

        # Get patient's appeals
        appeals = Appeal.filter_to_allowed_appeals(user).select_related(
            "for_denial"
        ).order_by("-creation_date")[:20]

        # Get patient's call logs
        call_logs = InsuranceCallLog.filter_to_allowed_call_logs(user).order_by(
            "-call_date"
        )[:20]

        # Get patient's evidence
        evidence = PatientEvidence.filter_to_allowed_evidence(user).order_by(
            "-created_at"
        )[:20]

        # Get upcoming follow-ups (next 14 days)
        today = date.today()
        two_weeks = today + timedelta(days=14)
        upcoming_followups = InsuranceCallLog.filter_to_allowed_call_logs(
            user
        ).filter(
            follow_up_date__gte=today,
            follow_up_date__lte=two_weeks,
        ).order_by("follow_up_date")[:5]

        context.update({
            "appeals": appeals,
            "appeals_count": appeals.count(),
            "call_logs": call_logs,
            "call_logs_count": InsuranceCallLog.filter_to_allowed_call_logs(user).count(),
            "evidence": evidence,
            "evidence_count": PatientEvidence.filter_to_allowed_evidence(user).count(),
            "upcoming_followups": upcoming_followups,
        })

        return context


class CallLogCreateView(PatientRequiredMixin, CreateView):
    """View for creating a new call log entry."""

    model = InsuranceCallLog
    form_class = InsuranceCallLogForm
    template_name = "patient_call_log_form.html"
    success_url = reverse_lazy("patient-dashboard")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["editing"] = False
        return context

    def form_valid(self, form):
        # Associate the call log with the patient
        form.instance.patient_user = self.patient_user
        return super().form_valid(form)


class CallLogEditView(PatientRequiredMixin, UpdateView):
    """View for editing an existing call log entry."""

    model = InsuranceCallLog
    form_class = InsuranceCallLogForm
    template_name = "patient_call_log_form.html"
    success_url = reverse_lazy("patient-dashboard")
    slug_field = "uuid"
    slug_url_kwarg = "uuid"

    def get_queryset(self):
        # Only allow editing own call logs
        return InsuranceCallLog.filter_to_allowed_call_logs(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["editing"] = True
        return context

    def post(self, request, *args, **kwargs):
        # Handle delete button
        if "delete" in request.POST:
            self.object = self.get_object()
            self.object.delete()
            return HttpResponseRedirect(self.success_url)
        return super().post(request, *args, **kwargs)


class EvidenceCreateView(PatientRequiredMixin, CreateView):
    """View for adding new evidence."""

    model = PatientEvidence
    form_class = PatientEvidenceForm
    template_name = "patient_evidence_form.html"
    success_url = reverse_lazy("patient-dashboard")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["editing"] = False
        return context

    def form_valid(self, form):
        # Associate the evidence with the patient
        form.instance.patient_user = self.patient_user

        # Handle file upload
        if "file" in self.request.FILES:
            uploaded_file = self.request.FILES["file"]
            form.instance.filename = uploaded_file.name
            form.instance.mime_type = (
                uploaded_file.content_type or "application/octet-stream"
            )

        return super().form_valid(form)


class EvidenceEditView(PatientRequiredMixin, UpdateView):
    """View for editing existing evidence."""

    model = PatientEvidence
    form_class = PatientEvidenceForm
    template_name = "patient_evidence_form.html"
    success_url = reverse_lazy("patient-dashboard")
    slug_field = "uuid"
    slug_url_kwarg = "uuid"

    def get_queryset(self):
        # Only allow editing own evidence
        return PatientEvidence.filter_to_allowed_evidence(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["editing"] = True
        context["object"] = self.object
        return context

    def form_valid(self, form):
        # Handle file upload
        if "file" in self.request.FILES:
            uploaded_file = self.request.FILES["file"]
            form.instance.filename = uploaded_file.name
            form.instance.mime_type = (
                uploaded_file.content_type or "application/octet-stream"
            )

        return super().form_valid(form)

    def post(self, request, *args, **kwargs):
        # Handle delete button
        if "delete" in request.POST:
            self.object = self.get_object()
            self.object.delete()
            return HttpResponseRedirect(self.success_url)
        return super().post(request, *args, **kwargs)


class EvidenceDownloadView(PatientRequiredMixin, View):
    """View for downloading evidence files."""

    def get(self, request, uuid):
        # Get the evidence, ensuring user has access
        evidence = get_object_or_404(
            PatientEvidence.filter_to_allowed_evidence(request.user),
            uuid=uuid,
        )

        if not evidence.file:
            raise Http404("No file attached to this evidence")

        # Return the file
        response = FileResponse(
            evidence.file,
            content_type=evidence.mime_type or "application/octet-stream",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="{evidence.filename or "download"}"'
        )
        return response

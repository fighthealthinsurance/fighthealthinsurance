"""
Patient Dashboard Views

Views for logged-in patients to manage their appeals, call logs, and evidence.
This provides a "pure FHI patient view" distinct from professional/provider interfaces.
"""

import typing
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files.uploadedfile import UploadedFile
from django.http import (
    FileResponse,
    Http404,
    HttpResponse,
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import CreateView, DeleteView, TemplateView, UpdateView

from django_encrypted_filefield.crypt import Cryptographer
from fhi_users.models import PatientUser
from fighthealthinsurance.forms import InsuranceCallLogForm, PatientEvidenceForm
from fighthealthinsurance.models import (
    Appeal,
    Denial,
    InsuranceCallLog,
    PatientEvidence,
)
from loguru import logger

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class PatientRequiredMixin(LoginRequiredMixin):
    """
    Mixin that ensures the user is logged in and has a PatientUser record.
    Redirects to login if not authenticated, or to an error page if not a patient.
    """

    def dispatch(self, request, *args, **kwargs):
        """
        Ensure user is authenticated and has a PatientUser record.

        Automatically creates a PatientUser record for authenticated users who don't have one yet.

        Args:
            request: HTTP request object
            *args: Variable positional arguments
            **kwargs: Variable keyword arguments

        Returns:
            HttpResponse: The response from the parent dispatch or login redirect

        """
        if not request.user.is_authenticated:
            return self.handle_no_permission()

        # Check if user has a PatientUser record
        try:
            self.patient_user = PatientUser.objects.get(user=request.user, active=True)
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
        """
        Build context data for patient dashboard template.

        Retrieves and aggregates patient's appeals, call logs, evidence, and upcoming
        follow-ups. Limits queryset to 20 most recent items for performance.

        Args:
            **kwargs: Additional context data from parent class

        Returns:
            dict: Template context containing appeals, call_logs, evidence, counts, and upcoming_followups

        """
        context = super().get_context_data(**kwargs)
        # User is guaranteed to be authenticated by PatientRequiredMixin
        user: User = self.request.user  # type: ignore

        # Get patient's appeals
        appeals = (
            Appeal.filter_to_allowed_appeals(user)
            .select_related("for_denial")
            .order_by("-creation_date")[:20]
        )

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
        upcoming_followups = (
            InsuranceCallLog.filter_to_allowed_call_logs(user)
            .filter(
                follow_up_date__gte=today,
                follow_up_date__lte=two_weeks,
            )
            .order_by("follow_up_date")[:5]
        )

        context.update(
            {
                "appeals": appeals,
                "appeals_count": appeals.count(),
                "call_logs": call_logs,
                "call_logs_count": InsuranceCallLog.filter_to_allowed_call_logs(
                    user
                ).count(),
                "evidence": evidence,
                "evidence_count": PatientEvidence.filter_to_allowed_evidence(
                    user
                ).count(),
                "upcoming_followups": upcoming_followups,
            }
        )

        return context


class CallLogCreateView(PatientRequiredMixin, CreateView):
    """View for creating a new call log entry."""

    model = InsuranceCallLog
    form_class = InsuranceCallLogForm
    template_name = "patient_call_log_form.html"
    success_url = reverse_lazy("patient-dashboard")

    def get_context_data(self, **kwargs):
        """Add editing flag to template context."""
        context = super().get_context_data(**kwargs)
        context["editing"] = False
        return context

    def form_valid(self, form):
        """Associate call log with patient user before saving."""
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
        """Filter queryset to only include call logs owned by current user."""
        # Only allow editing own call logs
        user: User = self.request.user  # type: ignore
        return InsuranceCallLog.filter_to_allowed_call_logs(user)

    def get_context_data(self, **kwargs):
        """Add editing flag to template context."""
        context = super().get_context_data(**kwargs)
        context["editing"] = True
        return context

    def post(self, request, *args, **kwargs):
        """Handle form submission or deletion if delete button was clicked."""
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
        """Add editing flag to template context."""
        context = super().get_context_data(**kwargs)
        context["editing"] = False
        return context

    def form_valid(self, form):
        """
        Associate evidence with patient and capture file metadata before saving.

        Extracts filename and MIME type from uploaded file and stores them
        in the evidence record for future downloads.

        Args:
            form: Valid EvidenceForm instance

        Returns:
            HttpResponse: Redirect to success URL

        """
        # Associate the evidence with the patient
        form.instance.patient_user = self.patient_user

        # Handle file upload
        if "file" in self.request.FILES:
            uploaded_file = self.request.FILES["file"]
            if isinstance(uploaded_file, UploadedFile):
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
        """Filter queryset to only include evidence owned by current user."""
        # Only allow editing own evidence
        user: User = self.request.user  # type: ignore
        return PatientEvidence.filter_to_allowed_evidence(user)

    def get_context_data(self, **kwargs):
        """Add editing flag and object to template context."""
        context = super().get_context_data(**kwargs)
        context["editing"] = True
        context["object"] = self.object
        return context

    def form_valid(self, form):
        """
        Update file metadata if new file is uploaded.

        Args:
            form: Valid EvidenceForm instance

        Returns:
            HttpResponse: Redirect to success URL

        """
        # Handle file upload
        if "file" in self.request.FILES:
            uploaded_file = self.request.FILES["file"]
            if isinstance(uploaded_file, UploadedFile):
                form.instance.filename = uploaded_file.name
                form.instance.mime_type = (
                    uploaded_file.content_type or "application/octet-stream"
                )

        return super().form_valid(form)

    def post(self, request, *args, **kwargs):
        """Handle form submission or deletion if delete button was clicked."""
        # Handle delete button
        if "delete" in request.POST:
            self.object = self.get_object()
            self.object.delete()
            return HttpResponseRedirect(self.success_url)
        return super().post(request, *args, **kwargs)


class EvidenceDownloadView(PatientRequiredMixin, View):
    """View for downloading evidence files with proper decryption."""

    def get(self, request, uuid):
        """
        Download evidence file with decryption.

        Retrieves encrypted evidence file, decrypts it, and returns as download.
        Only allows access to evidence owned by the current user.

        Args:
            request: HTTP request object
            uuid: UUID of the evidence to download

        Returns:
            HttpResponse: Decrypted file content with appropriate headers

        Raises:
            Http404: If evidence not found or has no file attached

        """
        # Get the evidence, ensuring user has access
        user: User = request.user  # type: ignore
        evidence = get_object_or_404(
            PatientEvidence.filter_to_allowed_evidence(user),
            uuid=uuid,
        )

        if not evidence.file:
            raise Http404("No file attached to this evidence")

        # Decrypt the file before returning
        file = evidence.file.open()
        decrypted_content = Cryptographer.decrypted(file.read())

        response = HttpResponse(
            decrypted_content,
            content_type=evidence.mime_type or "application/octet-stream",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="{evidence.filename or "download"}"'
        )
        return response

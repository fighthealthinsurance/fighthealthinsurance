"""Export views for patient dashboard data (call logs and evidence).

Provides CSV export functionality for call logs and evidence lists.
CSV format is more practical than PDF for data analysis and import into spreadsheets.
"""

import csv
from datetime import date, timedelta
from typing import Any

from django.http import HttpResponse
from django.views import View

from fighthealthinsurance.models import InsuranceCallLog, PatientEvidence
from fighthealthinsurance.patient_views import PatientRequiredMixin

if __name__ != "__main__":
    from django.contrib.auth import get_user_model

    User = get_user_model()


class CallLogExportView(PatientRequiredMixin, View):
    """Export call logs as CSV file."""

    def get(self, request):
        """Generate CSV export of all user's call logs.

        Returns:
            HttpResponse: CSV file download
        """
        user: User = request.user  # type: ignore

        # Get all call logs for user
        call_logs = (
            InsuranceCallLog.filter_to_allowed_call_logs(user)
            .order_by("-call_date")
        )

        # Create CSV response
        response = HttpResponse(
            content_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="call_logs_{date.today()}.csv"'
            },
        )

        writer = csv.writer(response)

        # Write header row
        writer.writerow(
            [
                "Date & Time",
                "Call Type",
                "Department",
                "Representative Name",
                "Representative ID",
                "Reference Number",
                "Case Number",
                "Reason for Call",
                "Key Statements",
                "Promises Made",
                "Call Notes",
                "Outcome",
                "Follow-up Date",
                "Follow-up Notes",
                "Call Duration (min)",
                "Wait Time (min)",
                "Include in Appeal",
                "Linked Appeal",
            ]
        )

        # Write data rows
        for log in call_logs:
            writer.writerow(
                [
                    log.call_date.strftime("%Y-%m-%d %H:%M:%S") if log.call_date else "",
                    log.get_call_type_display() if log.call_type else "",
                    log.department or "",
                    log.representative_name or "",
                    log.representative_id or "",
                    log.reference_number or "",
                    log.case_number or "",
                    log.reason_for_call or "",
                    log.key_statements or "",
                    log.promises_made or "",
                    log.call_notes or "",
                    log.get_outcome_display() if log.outcome else "",
                    log.follow_up_date.strftime("%Y-%m-%d") if log.follow_up_date else "",
                    log.follow_up_notes or "",
                    log.call_duration_minutes or "",
                    log.wait_time_minutes or "",
                    "Yes" if log.include_in_appeal else "No",
                    (
                        f"{log.appeal.for_denial.procedure} ({log.appeal.uuid})"
                        if log.appeal
                        else ""
                    ),
                ]
            )

        return response


class EvidenceExportView(PatientRequiredMixin, View):
    """Export evidence list as CSV file."""

    def get(self, request):
        """Generate CSV export of all user's evidence.

        Returns:
            HttpResponse: CSV file download
        """
        user: User = request.user  # type: ignore

        # Get all evidence for user
        evidence = (
            PatientEvidence.filter_to_allowed_evidence(user)
            .order_by("-created_at")
        )

        # Create CSV response
        response = HttpResponse(
            content_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="evidence_{date.today()}.csv"'
            },
        )

        writer = csv.writer(response)

        # Write header row
        writer.writerow(
            [
                "Title",
                "Evidence Type",
                "Description",
                "Date of Evidence",
                "Has File Attachment",
                "Text Content",
                "Source",
                "Created At",
                "Include in Appeal",
                "Linked Appeal",
            ]
        )

        # Write data rows
        for item in evidence:
            writer.writerow(
                [
                    item.title or "",
                    item.get_evidence_type_display() if item.evidence_type else "",
                    item.description or "",
                    item.date_of_evidence.strftime("%Y-%m-%d") if item.date_of_evidence else "",
                    "Yes" if item.file else "No",
                    item.text_content or "",
                    item.source or "",
                    item.created_at.strftime("%Y-%m-%d %H:%M:%S") if item.created_at else "",
                    "Yes" if item.include_in_appeal else "No",
                    (
                        f"{item.appeal.for_denial.procedure} ({item.appeal.uuid})"
                        if item.appeal
                        else ""
                    ),
                ]
            )

        return response

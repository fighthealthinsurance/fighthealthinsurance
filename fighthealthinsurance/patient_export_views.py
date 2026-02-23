"""Export views for patient dashboard data (call logs and evidence).

Provides CSV export functionality for call logs and evidence lists.
CSV format is more practical than PDF for data analysis and import into spreadsheets.
"""

import csv
from datetime import date
from django.http import HttpResponse
from django.views import View

from fighthealthinsurance.models import InsuranceCallLog, PatientEvidence
from fighthealthinsurance.patient_views import PatientRequiredMixin

# Characters that trigger formula interpretation in spreadsheet applications
_CSV_INJECTION_CHARS = ("=", "+", "-", "@", "\t", "\r", "\n")


def _sanitize_csv(value: str) -> str:
    """Prefix values starting with formula-triggering characters with a single quote.

    This prevents CSV injection (aka formula injection) where user-supplied values
    starting with =, +, -, @ etc. could be interpreted as formulas by Excel,
    Google Sheets, or LibreOffice Calc.
    """
    if value and value[0] in _CSV_INJECTION_CHARS:
        return "'" + value
    return value


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
            .select_related("appeal", "appeal__for_denial")
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
                "Call Date",
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
                    (
                        log.call_date.strftime("%Y-%m-%d %H:%M:%S")
                        if log.call_date
                        else ""
                    ),
                    log.get_call_type_display() if log.call_type else "",
                    _sanitize_csv(log.department or ""),
                    _sanitize_csv(log.representative_name or ""),
                    _sanitize_csv(log.representative_id or ""),
                    _sanitize_csv(log.reference_number or ""),
                    _sanitize_csv(log.case_number or ""),
                    _sanitize_csv(log.reason_for_call or ""),
                    _sanitize_csv(log.key_statements or ""),
                    _sanitize_csv(log.promises_made or ""),
                    _sanitize_csv(log.call_notes or ""),
                    log.get_outcome_display() if log.outcome else "",
                    (
                        log.follow_up_date.strftime("%Y-%m-%d")
                        if log.follow_up_date
                        else ""
                    ),
                    _sanitize_csv(log.follow_up_notes or ""),
                    log.call_duration_minutes or "",
                    log.wait_time_minutes or "",
                    "Yes" if log.include_in_appeal else "No",
                    (
                        _sanitize_csv(
                            f"{log.appeal.for_denial.procedure} ({log.appeal.uuid})"
                        )
                        if log.appeal and log.appeal.for_denial and log.appeal.for_denial.procedure
                        else (str(log.appeal.uuid) if log.appeal else "")
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
            .select_related("appeal", "appeal__for_denial")
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
                "UUID",
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
                    str(item.uuid),
                    _sanitize_csv(item.title or ""),
                    item.get_evidence_type_display() if item.evidence_type else "",
                    _sanitize_csv(item.description or ""),
                    (
                        item.date_of_evidence.strftime("%Y-%m-%d")
                        if item.date_of_evidence
                        else ""
                    ),
                    "Yes" if item.file else "No",
                    _sanitize_csv(item.text_content or ""),
                    _sanitize_csv(item.source or ""),
                    (
                        item.created_at.strftime("%Y-%m-%d %H:%M:%S")
                        if item.created_at
                        else ""
                    ),
                    "Yes" if item.include_in_appeal else "No",
                    (
                        _sanitize_csv(
                            f"{item.appeal.for_denial.procedure} ({item.appeal.uuid})"
                        )
                        if item.appeal and item.appeal.for_denial and item.appeal.for_denial.procedure
                        else (str(item.appeal.uuid) if item.appeal else "")
                    ),
                ]
            )

        return response

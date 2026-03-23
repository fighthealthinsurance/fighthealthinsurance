"""Tests for the patient dashboard feature.

Tests cover:
- Denial list endpoint (new)
- Call log CRUD with access control
- Patient evidence CRUD with access control
- Cross-patient data isolation
- Dashboard page view
"""

import typing
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance.models import (
    Appeal,
    Denial,
    InsuranceCallLog,
    PatientEvidence,
    PatientUser,
)

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class PatientDashboardSetupMixin:
    """Shared setup for patient dashboard tests."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Patient A
        self.user_a = User.objects.create_user(
            username="patient_a",
            password="testpass123",
            email="patient_a@example.com",
            first_name="Alice",
            last_name="Smith",
        )
        self.user_a.is_active = True
        self.user_a.save()
        self.patient_a = PatientUser.objects.create(user=self.user_a, active=True)

        # Patient B
        self.user_b = User.objects.create_user(
            username="patient_b",
            password="testpass456",
            email="patient_b@example.com",
            first_name="Bob",
            last_name="Jones",
        )
        self.user_b.is_active = True
        self.user_b.save()
        self.patient_b = PatientUser.objects.create(user=self.user_b, active=True)

        # Denial and appeal for patient A
        self.denial_a = Denial.objects.create(
            denial_text="Denied for patient A",
            insurance_company="Aetna",
            procedure="MRI",
            diagnosis="Back pain",
            patient_user=self.patient_a,
            hashed_email=Denial.get_hashed_email(self.user_a.email),
        )
        self.appeal_a = Appeal.objects.create(
            for_denial=self.denial_a,
            patient_user=self.patient_a,
            appeal_text="Appeal text for patient A",
            pending=True,
            pending_patient=True,
            hashed_email=Denial.get_hashed_email(self.user_a.email),
        )

        # Denial for patient B
        self.denial_b = Denial.objects.create(
            denial_text="Denied for patient B",
            insurance_company="BlueCross",
            patient_user=self.patient_b,
            hashed_email=Denial.get_hashed_email(self.user_b.email),
        )


class DenialListTest(PatientDashboardSetupMixin, APITestCase):
    """Tests for the denial list endpoint."""

    def test_patient_sees_own_denials(self):
        self.client.login(username="patient_a", password="testpass123")
        response = self.client.get("/ziggy/rest/denials/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["insurance_company"], "Aetna")

    def test_patient_cannot_see_other_patient_denials(self):
        self.client.login(username="patient_a", password="testpass123")
        response = self.client.get("/ziggy/rest/denials/")
        data = response.json()
        companies = [d["insurance_company"] for d in data]
        self.assertNotIn("BlueCross", companies)

    def test_denial_has_appeal_flag(self):
        self.client.login(username="patient_a", password="testpass123")
        response = self.client.get("/ziggy/rest/denials/")
        data = response.json()
        self.assertTrue(data[0]["has_appeal"])

    def test_denial_without_appeal(self):
        self.client.login(username="patient_b", password="testpass456")
        response = self.client.get("/ziggy/rest/denials/")
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertFalse(data[0]["has_appeal"])


class AppealListTest(PatientDashboardSetupMixin, APITestCase):
    """Tests for the existing appeal list endpoint with patient filtering."""

    def test_patient_sees_own_appeals(self):
        self.client.login(username="patient_a", password="testpass123")
        response = self.client.get("/ziggy/rest/appeals/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["status"], "pending patient")

    def test_patient_cannot_see_other_patient_appeals(self):
        self.client.login(username="patient_b", password="testpass456")
        response = self.client.get("/ziggy/rest/appeals/")
        data = response.json()
        self.assertEqual(len(data), 0)


class CallLogTest(PatientDashboardSetupMixin, APITestCase):
    """Tests for insurance call log CRUD."""

    def test_create_call_log(self):
        self.client.login(username="patient_a", password="testpass123")
        response = self.client.post(
            "/ziggy/rest/call_logs/",
            data={
                "call_date": "2026-03-20T10:00:00Z",
                "representative_name": "Jane Doe",
                "reference_number": "REF-123",
                "call_outcome": "Told to resubmit",
                "notes": "Was on hold for 45 minutes",
                "follow_up_needed": True,
                "follow_up_date": "2026-03-25",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertEqual(data["representative_name"], "Jane Doe")

    def test_create_call_log_linked_to_appeal(self):
        self.client.login(username="patient_a", password="testpass123")
        response = self.client.post(
            "/ziggy/rest/call_logs/",
            data={
                "call_date": "2026-03-20T10:00:00Z",
                "appeal": self.appeal_a.id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["appeal"], self.appeal_a.id)

    def test_list_call_logs(self):
        self.client.login(username="patient_a", password="testpass123")
        InsuranceCallLog.objects.create(
            patient_user=self.patient_a,
            call_date=timezone.now(),
            representative_name="Test Rep",
        )
        response = self.client.get("/ziggy/rest/call_logs/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_delete_call_log(self):
        self.client.login(username="patient_a", password="testpass123")
        log = InsuranceCallLog.objects.create(
            patient_user=self.patient_a,
            call_date=timezone.now(),
        )
        response = self.client.delete(f"/ziggy/rest/call_logs/{log.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(InsuranceCallLog.objects.filter(id=log.id).exists())

    def test_cross_patient_call_log_isolation(self):
        """Patient B cannot see Patient A's call logs."""
        InsuranceCallLog.objects.create(
            patient_user=self.patient_a,
            call_date=timezone.now(),
            representative_name="A's Rep",
        )
        self.client.login(username="patient_b", password="testpass456")
        response = self.client.get("/ziggy/rest/call_logs/")
        self.assertEqual(len(response.json()), 0)

    def test_cross_patient_call_log_delete_forbidden(self):
        """Patient B cannot delete Patient A's call logs."""
        log = InsuranceCallLog.objects.create(
            patient_user=self.patient_a,
            call_date=timezone.now(),
        )
        self.client.login(username="patient_b", password="testpass456")
        response = self.client.delete(f"/ziggy/rest/call_logs/{log.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_unauthenticated_gets_empty_call_logs(self):
        """Unauthenticated users get an empty list (no data leakage)."""
        InsuranceCallLog.objects.create(
            patient_user=self.patient_a,
            call_date=timezone.now(),
        )
        response = self.client.get("/ziggy/rest/call_logs/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)


class EvidenceTest(PatientDashboardSetupMixin, APITestCase):
    """Tests for patient evidence CRUD."""

    def _make_test_file(
        self,
        name="test.pdf",
        content=b"fake pdf content",
        content_type="application/pdf",
    ):
        return SimpleUploadedFile(name, content, content_type=content_type)

    def test_upload_evidence(self):
        self.client.login(username="patient_a", password="testpass123")
        test_file = self._make_test_file()
        response = self.client.post(
            "/ziggy/rest/patient_evidence/",
            data={
                "title": "My Medical Record",
                "description": "Doctor's notes",
                "file": test_file,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertEqual(data["title"], "My Medical Record")
        self.assertEqual(data["mime_type"], "application/pdf")

    def test_upload_evidence_linked_to_appeal(self):
        self.client.login(username="patient_a", password="testpass123")
        test_file = self._make_test_file()
        response = self.client.post(
            "/ziggy/rest/patient_evidence/",
            data={
                "title": "Supporting Doc",
                "file": test_file,
                "appeal": self.appeal_a.id,
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.json()["appeal"], self.appeal_a.id)

    def test_reject_oversized_file(self):
        self.client.login(username="patient_a", password="testpass123")
        # Create a file > 10MB
        big_content = b"x" * (10 * 1024 * 1024 + 1)
        test_file = SimpleUploadedFile("big.pdf", big_content, "application/pdf")
        response = self.client.post(
            "/ziggy/rest/patient_evidence/",
            data={"title": "Big File", "file": test_file},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_disallowed_mime_type(self):
        self.client.login(username="patient_a", password="testpass123")
        test_file = SimpleUploadedFile("script.sh", b"#!/bin/bash", "application/x-sh")
        response = self.client.post(
            "/ziggy/rest/patient_evidence/",
            data={"title": "Script", "file": test_file},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_evidence(self):
        self.client.login(username="patient_a", password="testpass123")
        PatientEvidence.objects.create(
            patient_user=self.patient_a,
            title="Test Doc",
            filename="test.pdf",
            mime_type="application/pdf",
        )
        response = self.client.get("/ziggy/rest/patient_evidence/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_delete_evidence(self):
        self.client.login(username="patient_a", password="testpass123")
        ev = PatientEvidence.objects.create(
            patient_user=self.patient_a,
            title="Test Doc",
            filename="test.pdf",
            mime_type="application/pdf",
        )
        response = self.client.delete(f"/ziggy/rest/patient_evidence/{ev.id}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(PatientEvidence.objects.filter(id=ev.id).exists())

    def test_cross_patient_evidence_isolation(self):
        """Patient B cannot see Patient A's evidence."""
        PatientEvidence.objects.create(
            patient_user=self.patient_a,
            title="A's Document",
            filename="test.pdf",
            mime_type="application/pdf",
        )
        self.client.login(username="patient_b", password="testpass456")
        response = self.client.get("/ziggy/rest/patient_evidence/")
        self.assertEqual(len(response.json()), 0)

    def test_cross_patient_evidence_delete_forbidden(self):
        """Patient B cannot delete Patient A's evidence."""
        ev = PatientEvidence.objects.create(
            patient_user=self.patient_a,
            title="A's Doc",
            filename="test.pdf",
            mime_type="application/pdf",
        )
        self.client.login(username="patient_b", password="testpass456")
        response = self.client.delete(f"/ziggy/rest/patient_evidence/{ev.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class DashboardViewTest(APITestCase):
    """Tests for the patient dashboard HTML page."""

    def test_dashboard_page_loads(self):
        response = self.client.get(reverse("patient_dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_dashboard_uses_correct_template(self):
        response = self.client.get(reverse("patient_dashboard"))
        self.assertTemplateUsed(response, "patient_dashboard.html")

    def test_dashboard_contains_root_element(self):
        response = self.client.get(reverse("patient_dashboard"))
        self.assertContains(response, 'id="patient-dashboard-root"')

    def test_dashboard_includes_bundle(self):
        response = self.client.get(reverse("patient_dashboard"))
        self.assertContains(response, "patient_dashboard.bundle.js")

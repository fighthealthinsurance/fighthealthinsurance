"""
Tests for the patient dashboard views and functionality.
Tests call log and evidence tracking features for logged-in patients.
"""

from datetime import date, datetime, timedelta
from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.utils import timezone

from fhi_users.models import PatientUser
from fighthealthinsurance.models import (
    Appeal,
    Denial,
    InsuranceCallLog,
    PatientEvidence,
)

User = get_user_model()


class PatientDashboardViewTests(TestCase):
    """Tests for the main patient dashboard view."""

    def setUp(self) -> None:
        self.client = Client()
        self.password = "TestPass123!"

        # Create a regular user
        self.user = User.objects.create_user(
            username="patient_test",
            password=self.password,
            email="patient@example.com",
            first_name="Test",
            last_name="Patient",
            is_active=True,
        )

        # Create a patient user record
        self.patient = PatientUser.objects.create(
            user=self.user,
            active=True,
        )

    def test_dashboard_requires_login(self) -> None:
        """Test that the dashboard requires authentication."""
        url = reverse("patient-dashboard")
        response = self.client.get(url)
        # Should redirect to login
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url.lower())

    def test_dashboard_accessible_when_logged_in(self) -> None:
        """Test that logged-in users can access the dashboard."""
        self.client.login(username=self.user.username, password=self.password)
        url = reverse("patient-dashboard")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "My Dashboard")

    def test_dashboard_shows_appeals(self) -> None:
        """Test that the dashboard displays the user's appeals."""
        self.client.login(username=self.user.username, password=self.password)

        # Create a denial and appeal for this patient
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email("patient@example.com"),
            denial_text="Test denial",
            patient_user=self.patient,
            procedure="Test Procedure",
            diagnosis="Test Diagnosis",
        )
        appeal = Appeal.objects.create(
            for_denial=denial,
            hashed_email=Denial.get_hashed_email("patient@example.com"),
            patient_user=self.patient,
            appeal_text="Test appeal text",
            patient_visible=True,
        )

        url = reverse("patient-dashboard")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Procedure")

    def test_dashboard_shows_call_logs(self) -> None:
        """Test that the dashboard displays call logs."""
        self.client.login(username=self.user.username, password=self.password)

        # Create a call log
        call_log = InsuranceCallLog.objects.create(
            patient_user=self.patient,
            call_date=timezone.now(),
            call_type="claim_status",
            reason_for_call="Check on claim status",
            outcome="pending",
        )

        url = reverse("patient-dashboard")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Check on claim status")

    def test_dashboard_shows_evidence(self) -> None:
        """Test that the dashboard displays patient evidence."""
        self.client.login(username=self.user.username, password=self.password)

        # Create evidence
        evidence = PatientEvidence.objects.create(
            patient_user=self.patient,
            evidence_type="document",
            title="Test EOB",
            description="Explanation of benefits from insurance",
        )

        url = reverse("patient-dashboard")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test EOB")

    def test_dashboard_creates_patient_user_if_missing(self) -> None:
        """Test that a PatientUser is created for logged-in users without one."""
        # Create a new user without a PatientUser record
        new_user = User.objects.create_user(
            username="new_patient",
            password=self.password,
            email="newpatient@example.com",
            is_active=True,
        )

        self.client.login(username=new_user.username, password=self.password)
        url = reverse("patient-dashboard")
        response = self.client.get(url)

        # Should succeed and create a PatientUser
        self.assertEqual(response.status_code, 200)
        self.assertTrue(PatientUser.objects.filter(user=new_user, active=True).exists())


class CallLogViewTests(TestCase):
    """Tests for call log create/edit/delete views."""

    def setUp(self) -> None:
        self.client = Client()
        self.password = "TestPass123!"

        self.user = User.objects.create_user(
            username="calllog_test",
            password=self.password,
            email="calllog@example.com",
            is_active=True,
        )
        self.patient = PatientUser.objects.create(
            user=self.user,
            active=True,
        )

    def test_call_log_create_requires_login(self) -> None:
        """Test that creating a call log requires authentication."""
        url = reverse("patient-call-log-create")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_call_log_create_form_renders(self) -> None:
        """Test that the call log creation form renders correctly."""
        self.client.login(username=self.user.username, password=self.password)
        url = reverse("patient-call-log-create")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Log Insurance Call")
        self.assertContains(response, "Reference Number")

    def test_call_log_create_submit(self) -> None:
        """Test creating a new call log."""
        self.client.login(username=self.user.username, password=self.password)
        url = reverse("patient-call-log-create")

        call_date = timezone.now().strftime("%Y-%m-%dT%H:%M")
        data = {
            "call_date": call_date,
            "call_type": "claim_status",
            "department": "Claims Department",
            "representative_name": "Jane Doe",
            "representative_id": "EMP12345",
            "reference_number": "REF-2024-001",
            "reason_for_call": "Checking on claim status",
            "key_statements": "Representative said claim is under review",
            "outcome": "pending",
        }

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)  # Redirect on success

        # Verify the call log was created
        self.assertTrue(
            InsuranceCallLog.objects.filter(
                patient_user=self.patient,
                reference_number="REF-2024-001",
            ).exists()
        )

    def test_call_log_edit_own(self) -> None:
        """Test editing own call log."""
        self.client.login(username=self.user.username, password=self.password)

        # Create a call log
        call_log = InsuranceCallLog.objects.create(
            patient_user=self.patient,
            call_date=timezone.now(),
            call_type="inquiry",
            reason_for_call="Initial reason",
            outcome="pending",
        )

        url = reverse("patient-call-log-edit", kwargs={"uuid": call_log.uuid})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Edit")

    def test_call_log_edit_other_user_denied(self) -> None:
        """Test that users cannot edit other users' call logs."""
        # Create another user and their call log
        other_user = User.objects.create_user(
            username="other_user",
            password=self.password,
            email="other@example.com",
            is_active=True,
        )
        other_patient = PatientUser.objects.create(
            user=other_user,
            active=True,
        )
        other_call_log = InsuranceCallLog.objects.create(
            patient_user=other_patient,
            call_date=timezone.now(),
            call_type="inquiry",
            reason_for_call="Other user's call",
            outcome="pending",
        )

        # Try to access as our user
        self.client.login(username=self.user.username, password=self.password)
        url = reverse("patient-call-log-edit", kwargs={"uuid": other_call_log.uuid})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_call_log_delete(self) -> None:
        """Test deleting a call log."""
        self.client.login(username=self.user.username, password=self.password)

        call_log = InsuranceCallLog.objects.create(
            patient_user=self.patient,
            call_date=timezone.now(),
            call_type="inquiry",
            reason_for_call="To be deleted",
            outcome="pending",
        )

        url = reverse("patient-call-log-edit", kwargs={"uuid": call_log.uuid})
        response = self.client.post(url, {"delete": "true"})
        self.assertEqual(response.status_code, 302)

        # Verify deletion
        self.assertFalse(InsuranceCallLog.objects.filter(id=call_log.id).exists())


class EvidenceViewTests(TestCase):
    """Tests for evidence create/edit/delete/download views."""

    def setUp(self) -> None:
        self.client = Client()
        self.password = "TestPass123!"

        self.user = User.objects.create_user(
            username="evidence_test",
            password=self.password,
            email="evidence@example.com",
            is_active=True,
        )
        self.patient = PatientUser.objects.create(
            user=self.user,
            active=True,
        )

    def test_evidence_create_requires_login(self) -> None:
        """Test that creating evidence requires authentication."""
        url = reverse("patient-evidence-create")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)

    def test_evidence_create_form_renders(self) -> None:
        """Test that the evidence creation form renders correctly."""
        self.client.login(username=self.user.username, password=self.password)
        url = reverse("patient-evidence-create")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Add Evidence")
        self.assertContains(response, "Type of Evidence")

    def test_evidence_create_submit(self) -> None:
        """Test creating new evidence."""
        self.client.login(username=self.user.username, password=self.password)
        url = reverse("patient-evidence-create")

        data = {
            "evidence_type": "eob",
            "title": "December EOB",
            "description": "Explanation of benefits for December services",
            "source": "Insurance portal",
            "text_content": "Some extracted text",
        }

        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)

        # Verify the evidence was created
        self.assertTrue(
            PatientEvidence.objects.filter(
                patient_user=self.patient,
                title="December EOB",
            ).exists()
        )

    def test_evidence_edit_own(self) -> None:
        """Test editing own evidence."""
        self.client.login(username=self.user.username, password=self.password)

        evidence = PatientEvidence.objects.create(
            patient_user=self.patient,
            evidence_type="document",
            title="Original Title",
        )

        url = reverse("patient-evidence-edit", kwargs={"uuid": evidence.uuid})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Edit Evidence")

    def test_evidence_edit_other_user_denied(self) -> None:
        """Test that users cannot edit other users' evidence."""
        other_user = User.objects.create_user(
            username="other_evidence_user",
            password=self.password,
            email="otherevidence@example.com",
            is_active=True,
        )
        other_patient = PatientUser.objects.create(
            user=other_user,
            active=True,
        )
        other_evidence = PatientEvidence.objects.create(
            patient_user=other_patient,
            evidence_type="document",
            title="Other user's evidence",
        )

        self.client.login(username=self.user.username, password=self.password)
        url = reverse("patient-evidence-edit", kwargs={"uuid": other_evidence.uuid})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_evidence_delete(self) -> None:
        """Test deleting evidence."""
        self.client.login(username=self.user.username, password=self.password)

        evidence = PatientEvidence.objects.create(
            patient_user=self.patient,
            evidence_type="notes",
            title="To be deleted",
        )

        url = reverse("patient-evidence-edit", kwargs={"uuid": evidence.uuid})
        response = self.client.post(url, {"delete": "true"})
        self.assertEqual(response.status_code, 302)

        self.assertFalse(PatientEvidence.objects.filter(id=evidence.id).exists())

    def test_evidence_download_no_file(self) -> None:
        """Test downloading evidence when no file is attached returns 404."""
        self.client.login(username=self.user.username, password=self.password)

        evidence = PatientEvidence.objects.create(
            patient_user=self.patient,
            evidence_type="notes",
            title="Notes only - no file",
        )

        url = reverse("patient-evidence-download", kwargs={"uuid": evidence.uuid})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


class InsuranceCallLogModelTests(TestCase):
    """Tests for the InsuranceCallLog model."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="model_test",
            password="TestPass123!",
            email="model@example.com",
            is_active=True,
        )
        self.patient = PatientUser.objects.create(
            user=self.user,
            active=True,
        )

    def test_call_log_format_for_appeal(self) -> None:
        """Test the format_for_appeal method."""
        call_log = InsuranceCallLog.objects.create(
            patient_user=self.patient,
            call_date=timezone.make_aware(datetime(2024, 12, 15, 10, 30)),
            call_type="claim_status",
            department="Claims",
            representative_name="John Smith",
            representative_id="EMP123",
            reference_number="REF-001",
            reason_for_call="Check claim status",
            key_statements="Said it would be approved",
            outcome="pending",
        )

        formatted = call_log.format_for_appeal()
        self.assertIn("December 15, 2024", formatted)
        self.assertIn("Claims", formatted)
        self.assertIn("John Smith", formatted)
        self.assertIn("REF-001", formatted)
        self.assertIn("Check claim status", formatted)

    def test_filter_to_allowed_call_logs_patient(self) -> None:
        """Test that patients can only see their own call logs."""
        call_log = InsuranceCallLog.objects.create(
            patient_user=self.patient,
            call_date=timezone.now(),
            call_type="inquiry",
            reason_for_call="My call",
            outcome="pending",
        )

        # Create another user's call log
        other_user = User.objects.create_user(
            username="other",
            password="pass",
            email="other@example.com",
        )
        other_patient = PatientUser.objects.create(user=other_user, active=True)
        InsuranceCallLog.objects.create(
            patient_user=other_patient,
            call_date=timezone.now(),
            call_type="inquiry",
            reason_for_call="Other call",
            outcome="pending",
        )

        # Filter for our user
        allowed = InsuranceCallLog.filter_to_allowed_call_logs(self.user)
        self.assertEqual(allowed.count(), 1)
        self.assertEqual(allowed.first().id, call_log.id)

    def test_filter_to_allowed_call_logs_staff(self) -> None:
        """Test that staff can see all call logs."""
        InsuranceCallLog.objects.create(
            patient_user=self.patient,
            call_date=timezone.now(),
            call_type="inquiry",
            reason_for_call="Call 1",
            outcome="pending",
        )
        other_user = User.objects.create_user(
            username="other2",
            password="pass",
            email="other2@example.com",
        )
        other_patient = PatientUser.objects.create(user=other_user, active=True)
        InsuranceCallLog.objects.create(
            patient_user=other_patient,
            call_date=timezone.now(),
            call_type="inquiry",
            reason_for_call="Call 2",
            outcome="pending",
        )

        # Create staff user
        staff_user = User.objects.create_user(
            username="staff",
            password="pass",
            email="staff@example.com",
            is_staff=True,
        )

        allowed = InsuranceCallLog.filter_to_allowed_call_logs(staff_user)
        self.assertEqual(allowed.count(), 2)


class PatientEvidenceModelTests(TestCase):
    """Tests for the PatientEvidence model."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="evidence_model_test",
            password="TestPass123!",
            email="evidencemodel@example.com",
            is_active=True,
        )
        self.patient = PatientUser.objects.create(
            user=self.user,
            active=True,
        )

    def test_evidence_str(self) -> None:
        """Test the string representation of evidence."""
        evidence = PatientEvidence.objects.create(
            patient_user=self.patient,
            evidence_type="eob",
            title="My EOB",
        )
        self.assertIn("Explanation of Benefits", str(evidence))
        self.assertIn("My EOB", str(evidence))

    def test_filter_to_allowed_evidence_patient(self) -> None:
        """Test that patients can only see their own evidence."""
        evidence = PatientEvidence.objects.create(
            patient_user=self.patient,
            evidence_type="document",
            title="My evidence",
        )

        other_user = User.objects.create_user(
            username="other3",
            password="pass",
            email="other3@example.com",
        )
        other_patient = PatientUser.objects.create(user=other_user, active=True)
        PatientEvidence.objects.create(
            patient_user=other_patient,
            evidence_type="document",
            title="Other evidence",
        )

        allowed = PatientEvidence.filter_to_allowed_evidence(self.user)
        self.assertEqual(allowed.count(), 1)
        self.assertEqual(allowed.first().id, evidence.id)


class DashboardNavigationTests(TestCase):
    """Tests for navigation and links in the patient dashboard."""

    def setUp(self) -> None:
        self.client = Client()
        self.password = "TestPass123!"
        self.user = User.objects.create_user(
            username="nav_test",
            password=self.password,
            email="nav@example.com",
            is_active=True,
        )
        PatientUser.objects.create(user=self.user, active=True)

    def test_nav_link_shows_for_logged_in_user(self) -> None:
        """Test that the dashboard link appears in nav for logged in users."""
        self.client.login(username=self.user.username, password=self.password)
        # Note: Using dashboard page instead of root since root is cached
        # and caching doesn't reflect user-specific content
        response = self.client.get(reverse("patient-dashboard"))
        self.assertContains(response, "My Dashboard")
        self.assertContains(response, reverse("patient-dashboard"))

    def test_nav_link_hidden_for_anonymous(self) -> None:
        """Test that the dashboard link is hidden for anonymous users."""
        response = self.client.get(reverse("root"))
        self.assertNotContains(response, "My Dashboard")

    def test_dashboard_has_add_call_log_link(self) -> None:
        """Test that the dashboard has a link to add call logs."""
        self.client.login(username=self.user.username, password=self.password)
        response = self.client.get(reverse("patient-dashboard"))
        self.assertContains(response, reverse("patient-call-log-create"))

    def test_dashboard_has_add_evidence_link(self) -> None:
        """Test that the dashboard has a link to add evidence."""
        self.client.login(username=self.user.username, password=self.password)
        response = self.client.get(reverse("patient-dashboard"))
        self.assertContains(response, reverse("patient-evidence-create"))

    def test_dashboard_has_new_appeal_link(self) -> None:
        """Test that the dashboard has a link to create a new appeal."""
        self.client.login(username=self.user.username, password=self.password)
        response = self.client.get(reverse("patient-dashboard"))
        self.assertContains(response, reverse("scan"))

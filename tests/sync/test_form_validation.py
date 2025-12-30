"""Tests for form validation."""

from django.test import TestCase

from fighthealthinsurance.forms import (
    DeleteDataForm,
    ShareAppealForm,
    BaseDenialForm,
    DenialForm,
    ProDenialForm,
    DenialRefForm,
    FaxForm,
    FaxResendForm,
    FollowUpForm,
)


class TestDeleteDataForm(TestCase):
    """Test DeleteDataForm validation."""

    def test_valid_form(self):
        """Valid email should pass."""
        form = DeleteDataForm(data={"email": "test@example.com"})
        self.assertTrue(form.is_valid())

    def test_missing_email(self):
        """Missing email should fail."""
        form = DeleteDataForm(data={})
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)


class TestShareAppealForm(TestCase):
    """Test ShareAppealForm validation."""

    def test_valid_form(self):
        """Valid data should pass."""
        form = ShareAppealForm(
            data={
                "denial_id": 123,
                "email": "test@example.com",
                "appeal_text": "This is my appeal text.",
            }
        )
        self.assertTrue(form.is_valid())

    def test_missing_denial_id(self):
        """Missing denial_id should fail."""
        form = ShareAppealForm(
            data={"email": "test@example.com", "appeal_text": "This is my appeal text."}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("denial_id", form.errors)

    def test_missing_appeal_text(self):
        """Missing appeal_text should fail."""
        form = ShareAppealForm(
            data={
                "denial_id": 123,
                "email": "test@example.com",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("appeal_text", form.errors)


class TestBaseDenialForm(TestCase):
    """Test BaseDenialForm validation."""

    def test_valid_form(self):
        """Valid data should pass."""
        form = BaseDenialForm(
            data={
                "pii": True,
                "tos": True,
                "privacy": True,
                "denial_text": "My insurance denied my claim for X.",
                "email": "patient@example.com",
            }
        )
        self.assertTrue(form.is_valid())

    def test_missing_required_checkboxes(self):
        """Missing required checkboxes should fail."""
        form = BaseDenialForm(
            data={
                "denial_text": "My insurance denied my claim.",
                "email": "patient@example.com",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("pii", form.errors)
        self.assertIn("tos", form.errors)
        self.assertIn("privacy", form.errors)

    def test_invalid_email(self):
        """Invalid email should fail."""
        form = BaseDenialForm(
            data={
                "pii": True,
                "tos": True,
                "privacy": True,
                "denial_text": "My insurance denied my claim.",
                "email": "not-an-email",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

    def test_missing_denial_text(self):
        """Missing denial_text should fail."""
        form = BaseDenialForm(
            data={
                "pii": True,
                "tos": True,
                "privacy": True,
                "email": "patient@example.com",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("denial_text", form.errors)


class TestDenialForm(TestCase):
    """Test DenialForm (extends BaseDenialForm)."""

    def test_inherits_validation(self):
        """Should inherit validation from BaseDenialForm."""
        form = DenialForm(
            data={
                "pii": True,
                "tos": True,
                "privacy": True,
                "denial_text": "My denial text.",
                "email": "test@example.com",
            }
        )
        self.assertTrue(form.is_valid())


class TestProDenialForm(TestCase):
    """Test ProDenialForm validation."""

    def test_valid_form(self):
        """Valid data should pass."""
        form = ProDenialForm(
            data={
                "pii": True,
                "tos": True,
                "privacy": True,
                "denial_text": "Patient denial text.",
                "email": "patient@example.com",
                "primary_professional": "Dr. Smith",
                "patient_id": "P12345",
            }
        )
        self.assertTrue(form.is_valid())

    def test_optional_fields(self):
        """Optional professional fields should not be required."""
        form = ProDenialForm(
            data={
                "pii": True,
                "tos": True,
                "privacy": True,
                "denial_text": "Patient denial text.",
                "email": "patient@example.com",
            }
        )
        self.assertTrue(form.is_valid())


class TestDenialRefForm(TestCase):
    """Test DenialRefForm validation."""

    def test_valid_form(self):
        """Valid data should pass."""
        form = DenialRefForm(
            data={
                "denial_id": 456,
                "email": "test@example.com",
                "semi_sekret": "abc123secret",
            }
        )
        self.assertTrue(form.is_valid())

    def test_missing_denial_id(self):
        """Missing denial_id should fail."""
        form = DenialRefForm(
            data={
                "email": "test@example.com",
                "semi_sekret": "abc123secret",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("denial_id", form.errors)

    def test_missing_semi_sekret(self):
        """Missing semi_sekret should fail."""
        form = DenialRefForm(
            data={
                "denial_id": 456,
                "email": "test@example.com",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("semi_sekret", form.errors)


class TestFaxForm(TestCase):
    """Test FaxForm validation."""

    def test_valid_form(self):
        """Valid data should pass."""
        form = FaxForm(
            data={
                "denial_id": 789,
                "email": "test@example.com",
                "semi_sekret": "secret123",
                "name": "Jane Doe",
                "insurance_company": "Aetna",
                "fax_phone": "1-800-555-1234",
                "completed_appeal_text": "Dear Insurance Company, I am appealing...",
            }
        )
        self.assertTrue(form.is_valid())

    def test_missing_fax_phone(self):
        """Missing fax phone should fail."""
        form = FaxForm(
            data={
                "denial_id": 789,
                "email": "test@example.com",
                "semi_sekret": "secret123",
                "name": "Jane Doe",
                "insurance_company": "Aetna",
                "completed_appeal_text": "Dear Insurance Company...",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("fax_phone", form.errors)

    def test_missing_name(self):
        """Missing name should fail."""
        form = FaxForm(
            data={
                "denial_id": 789,
                "email": "test@example.com",
                "semi_sekret": "secret123",
                "insurance_company": "Aetna",
                "fax_phone": "1-800-555-1234",
                "completed_appeal_text": "Dear Insurance Company...",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)


class TestFaxResendForm(TestCase):
    """Test FaxResendForm validation."""

    def test_valid_form(self):
        """Valid data should pass."""
        import uuid

        form = FaxResendForm(
            data={
                "fax_phone": "1-800-555-9999",
                "uuid": str(uuid.uuid4()),
                "hashed_email": "abc123hashed",
            }
        )
        self.assertTrue(form.is_valid())

    def test_invalid_uuid(self):
        """Invalid UUID should fail."""
        form = FaxResendForm(
            data={
                "fax_phone": "1-800-555-9999",
                "uuid": "not-a-valid-uuid",
                "hashed_email": "abc123hashed",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("uuid", form.errors)


class TestFollowUpForm(TestCase):
    """Test FollowUpForm validation."""

    def test_valid_form(self):
        """Valid data should pass."""
        import uuid

        form = FollowUpForm(
            data={
                "uuid": str(uuid.uuid4()),
                "follow_up_semi_sekret": "sekret123",
                "hashed_email": "hashed123",
                "appeal_result": "Yes",
            }
        )
        self.assertTrue(form.is_valid())

    def test_missing_required_fields(self):
        """Missing required fields should fail."""
        form = FollowUpForm(data={})
        self.assertFalse(form.is_valid())
        self.assertIn("uuid", form.errors)
        self.assertIn("follow_up_semi_sekret", form.errors)
        self.assertIn("hashed_email", form.errors)

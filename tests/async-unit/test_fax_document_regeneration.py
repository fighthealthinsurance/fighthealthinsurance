"""
Unit tests for FaxesToSend document regeneration functionality.

Tests the get_fax_document() method and _regenerate_document_from_appeal_data() helper,
ensuring documents can be retrieved from various sources or regenerated when missing.
"""

import os
import tempfile
from unittest.mock import Mock, patch, MagicMock

import pytest
from django.core.files.base import ContentFile
from django_encrypted_filefield.crypt import Cryptographer

from fighthealthinsurance.models import Denial, FaxesToSend, Appeal
from fhi_users.models import ProfessionalUser


class TestFaxDocumentRetrieval:
    """Tests for get_fax_document() method with existing documents."""

    @pytest.fixture
    def sample_pdf_bytes(self):
        """Sample PDF content for testing."""
        return b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<\n/Type /Catalog\n/Pages 2 0 R\n>>\nendobj\n"

    @pytest.fixture
    def fax_with_encrypted_document(self, db, sample_pdf_bytes):
        """FaxesToSend with encrypted document."""
        denial = Denial.objects.create(
            hashed_email="test@example.com",
            denial_text="Test denial",
            insurance_company="Test Insurance",
            claim_id="TEST123",
        )

        fax = FaxesToSend.objects.create(
            hashed_email="test@example.com",
            email="test@example.com",
            paid=True,
            appeal_text="Test appeal text",
            denial_id=denial,
        )

        # Save document (EncryptedFileField handles encryption automatically)
        fax.combined_document_enc.save(
            "test_fax.pdf", ContentFile(sample_pdf_bytes), save=True
        )

        return fax

    @pytest.fixture
    def fax_with_unencrypted_document(self, db, sample_pdf_bytes):
        """FaxesToSend with unencrypted document."""
        denial = Denial.objects.create(
            hashed_email="test@example.com",
            denial_text="Test denial",
            insurance_company="Test Insurance",
            claim_id="TEST123",
        )

        fax = FaxesToSend.objects.create(
            hashed_email="test@example.com",
            email="test@example.com",
            paid=True,
            appeal_text="Test appeal text",
            denial_id=denial,
        )

        # Save unencrypted document
        fax.combined_document.save(
            "test_fax.pdf", ContentFile(sample_pdf_bytes), save=True
        )

        return fax

    @pytest.mark.django_db
    def test_get_fax_document_encrypted_exists(
        self, fax_with_encrypted_document, sample_pdf_bytes
    ):
        """Test getting document when encrypted version exists."""
        result = fax_with_encrypted_document.get_fax_document(return_encrypted=False)

        assert result == sample_pdf_bytes
        assert isinstance(result, bytes)

    @pytest.mark.django_db
    def test_get_fax_document_unencrypted_exists(
        self, fax_with_unencrypted_document, sample_pdf_bytes
    ):
        """Test getting document when only unencrypted version exists."""
        result = fax_with_unencrypted_document.get_fax_document(return_encrypted=False)

        assert result == sample_pdf_bytes
        assert isinstance(result, bytes)

    @pytest.mark.django_db
    def test_get_fax_document_return_encrypted_true(
        self, fax_with_encrypted_document, sample_pdf_bytes
    ):
        """Test returning encrypted bytes when requested."""
        result = fax_with_encrypted_document.get_fax_document(return_encrypted=True)

        # Should return encrypted bytes
        assert isinstance(result, bytes)
        # Should be able to decrypt back to original
        decrypted = Cryptographer.decrypted(result)
        assert decrypted == sample_pdf_bytes

    @pytest.mark.django_db
    def test_get_fax_document_return_encrypted_from_unencrypted(
        self, fax_with_unencrypted_document, sample_pdf_bytes
    ):
        """Test returning encrypted bytes when only unencrypted source exists."""
        result = fax_with_unencrypted_document.get_fax_document(return_encrypted=True)

        # Should encrypt and return
        assert isinstance(result, bytes)
        # Should be able to decrypt back to original
        decrypted = Cryptographer.decrypted(result)
        assert decrypted == sample_pdf_bytes


class TestFaxDocumentFallbackToAppeal:
    """Tests for falling back to Appeal.document_enc."""

    @pytest.fixture
    def sample_pdf_bytes(self):
        """Sample PDF content for testing."""
        return b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<\n/Type /Catalog\n/Pages 2 0 R\n>>\nendobj\n"

    @pytest.fixture
    def fax_with_appeal_fallback(self, db, sample_pdf_bytes):
        """FaxesToSend without document but with Appeal that has document."""
        denial = Denial.objects.create(
            hashed_email="test@example.com",
            denial_text="Test denial",
            insurance_company="Test Insurance",
            claim_id="TEST123",
        )

        appeal = Appeal.objects.create(
            hashed_email="test@example.com",
            appeal_text="Test appeal text",
            for_denial=denial,
        )

        # Save document to Appeal (EncryptedFileField handles encryption automatically)
        appeal.document_enc.save(
            "test_appeal.pdf", ContentFile(sample_pdf_bytes), save=True
        )

        fax = FaxesToSend.objects.create(
            hashed_email="test@example.com",
            email="test@example.com",
            paid=True,
            appeal_text="Test appeal text",
            denial_id=denial,
            for_appeal=appeal,
        )

        return fax

    @pytest.mark.django_db
    def test_get_fax_document_fallback_to_appeal(
        self, fax_with_appeal_fallback, sample_pdf_bytes
    ):
        """Test fallback to Appeal.document_enc when FaxesToSend has no document."""
        result = fax_with_appeal_fallback.get_fax_document(return_encrypted=False)

        assert result == sample_pdf_bytes
        assert isinstance(result, bytes)


class TestFaxDocumentRegeneration:
    """Tests for document regeneration from appeal data."""

    @pytest.fixture
    def sample_pdf_bytes(self):
        """Sample PDF content for testing."""
        return b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<\n/Type /Catalog\n/Pages 2 0 R\n>>\nendobj\n"

    @pytest.fixture
    def fax_without_document(self, db):
        """FaxesToSend without any document."""
        denial = Denial.objects.create(
            hashed_email="test@example.com",
            denial_text="Test denial",
            insurance_company="Test Insurance",
            claim_id="TEST123",
            appeal_fax_number="5555551234",
        )

        fax = FaxesToSend.objects.create(
            hashed_email="test@example.com",
            email="test@example.com",
            name="Test Patient",
            paid=True,
            appeal_text="This is a test appeal letter requesting coverage.",
            denial_id=denial,
            destination="5555551234",
        )

        return fax

    @pytest.mark.django_db
    def test_regenerate_document_from_appeal_data(
        self, fax_without_document, sample_pdf_bytes
    ):
        """Test successful document regeneration from appeal data."""
        # Mock the _assemble_appeal_pdf method to return a temp file
        with patch(
            "fighthealthinsurance.common_view_logic.AppealAssemblyHelper._assemble_appeal_pdf"
        ) as mock_assemble:
            # Create a temporary file with sample PDF content
            with tempfile.NamedTemporaryFile(
                mode="w+b", suffix=".pdf", delete=False
            ) as temp_file:
                temp_file.write(sample_pdf_bytes)
                temp_file.flush()
                temp_path = temp_file.name

            mock_assemble.return_value = temp_path

            try:
                result = fax_without_document.get_fax_document(return_encrypted=False)

                # Verify the PDF was regenerated
                assert isinstance(result, bytes)
                assert result == sample_pdf_bytes

                # Verify the document was saved to the database
                fax_without_document.refresh_from_db()
                assert fax_without_document.combined_document_enc
                assert fax_without_document.combined_document_enc.name

                # Verify mock was called with correct parameters
                mock_assemble.assert_called_once()
                call_kwargs = mock_assemble.call_args[1]
                assert call_kwargs["patient_name"] == "Test Patient"
                assert call_kwargs["insurance_company"] == "Test Insurance"
                assert call_kwargs["claim_id"] == "TEST123"
                assert (
                    call_kwargs["completed_appeal_text"]
                    == "This is a test appeal letter requesting coverage."
                )

            finally:
                # Clean up temp file if it still exists
                if os.path.exists(temp_path):
                    os.unlink(temp_path)

    @pytest.mark.django_db
    def test_regenerate_saves_encrypted_document(
        self, fax_without_document, sample_pdf_bytes
    ):
        """Test that regenerated document is saved encrypted."""
        with patch(
            "fighthealthinsurance.common_view_logic.AppealAssemblyHelper._assemble_appeal_pdf"
        ) as mock_assemble:
            with tempfile.NamedTemporaryFile(
                mode="w+b", suffix=".pdf", delete=False
            ) as temp_file:
                temp_file.write(sample_pdf_bytes)
                temp_file.flush()
                temp_path = temp_file.name

            mock_assemble.return_value = temp_path

            try:
                fax_without_document.get_fax_document(return_encrypted=False)

                # Verify encrypted document is saved
                fax_without_document.refresh_from_db()
                stored_encrypted = fax_without_document.combined_document_enc.read()

                # Should be able to decrypt it
                decrypted = Cryptographer.decrypted(stored_encrypted)
                assert decrypted == sample_pdf_bytes

            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)

    @pytest.mark.django_db
    def test_regenerate_on_decryption_failure(self, db, sample_pdf_bytes):
        """Test that decryption failure triggers regeneration."""
        denial = Denial.objects.create(
            hashed_email="test@example.com",
            denial_text="Test denial",
            insurance_company="Test Insurance",
            claim_id="TEST123",
            appeal_fax_number="5555551234",
        )

        fax = FaxesToSend.objects.create(
            hashed_email="test@example.com",
            email="test@example.com",
            name="Test Patient",
            paid=True,
            appeal_text="This is a test appeal letter requesting coverage.",
            denial_id=denial,
            destination="5555551234",
        )

        # Save some encrypted data
        fax.combined_document_enc.save(
            "test.pdf", ContentFile(sample_pdf_bytes), save=True
        )

        # Mock Cryptographer.decrypted to fail (simulating corrupted data)
        with patch(
            "fighthealthinsurance.models.Cryptographer.decrypted"
        ) as mock_decrypt:
            mock_decrypt.side_effect = Exception("Decryption failed: corrupted data")

            # Mock the _assemble_appeal_pdf to return sample PDF
            with patch(
                "fighthealthinsurance.common_view_logic.AppealAssemblyHelper._assemble_appeal_pdf"
            ) as mock_assemble:
                with tempfile.NamedTemporaryFile(
                    mode="w+b", suffix=".pdf", delete=False
                ) as temp_file:
                    temp_file.write(sample_pdf_bytes)
                    temp_file.flush()
                    temp_path = temp_file.name

                mock_assemble.return_value = temp_path

                try:
                    # Should detect decryption failure and regenerate
                    result = fax.get_fax_document(return_encrypted=False)

                    # Verify the PDF was regenerated (not the corrupted data)
                    assert isinstance(result, bytes)
                    assert result == sample_pdf_bytes

                    # Verify decryption was attempted
                    assert mock_decrypt.called

                    # Verify regeneration was called due to decryption failure
                    mock_assemble.assert_called_once()

                finally:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)

    @pytest.mark.django_db
    def test_regenerate_missing_appeal_text(self, db):
        """Test that regeneration fails gracefully when appeal_text is missing."""
        denial = Denial.objects.create(
            hashed_email="test@example.com",
            denial_text="Test denial",
            insurance_company="Test Insurance",
        )

        fax = FaxesToSend.objects.create(
            hashed_email="test@example.com",
            email="test@example.com",
            paid=True,
            appeal_text="",  # Empty appeal text
            denial_id=denial,
        )

        with pytest.raises(Exception) as exc_info:
            fax.get_fax_document(return_encrypted=False)

        assert "missing appeal_text" in str(exc_info.value)

    @pytest.mark.django_db
    def test_regenerate_missing_denial_id(self, db):
        """Test that regeneration fails gracefully when denial_id is missing."""
        fax = FaxesToSend.objects.create(
            hashed_email="test@example.com",
            email="test@example.com",
            paid=True,
            appeal_text="Test appeal text",
            denial_id=None,  # Missing denial
        )

        with pytest.raises(Exception) as exc_info:
            fax.get_fax_document(return_encrypted=False)

        assert "missing denial_id" in str(exc_info.value)

    @pytest.mark.django_db
    def test_regenerate_with_professional_info(self, db, sample_pdf_bytes):
        """Test regeneration includes professional information when available."""
        from django.contrib.auth import get_user_model

        User = get_user_model()

        user = User.objects.create_user(
            username="testprofessional",
            email="professional@example.com",
            first_name="Dr. John",
            last_name="Smith",
        )

        professional = ProfessionalUser.objects.create(
            user=user,
            active=True,
            fax_number="5555559999",
            display_name="Dr. John Smith, MD",
        )

        denial = Denial.objects.create(
            hashed_email="test@example.com",
            denial_text="Test denial",
            insurance_company="Test Insurance",
            claim_id="TEST123",
            primary_professional=professional,
        )

        fax = FaxesToSend.objects.create(
            hashed_email="test@example.com",
            email="test@example.com",
            name="Test Patient",
            paid=True,
            appeal_text="Test appeal text",
            denial_id=denial,
        )

        with patch(
            "fighthealthinsurance.common_view_logic.AppealAssemblyHelper._assemble_appeal_pdf"
        ) as mock_assemble:
            with tempfile.NamedTemporaryFile(
                mode="w+b", suffix=".pdf", delete=False
            ) as temp_file:
                temp_file.write(sample_pdf_bytes)
                temp_file.flush()
                temp_path = temp_file.name

            mock_assemble.return_value = temp_path

            try:
                fax.get_fax_document(return_encrypted=False)

                # Verify professional info was included
                call_kwargs = mock_assemble.call_args[1]
                assert call_kwargs["professional_name"] == "Dr. John Smith, MD"
                assert call_kwargs["professional_fax_number"] == "5555559999"

            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)

    @pytest.mark.django_db
    def test_regenerate_with_pubmed_ids(self, db, sample_pdf_bytes):
        """Test regeneration includes PubMed IDs when available."""
        denial = Denial.objects.create(
            hashed_email="test@example.com",
            denial_text="Test denial",
            insurance_company="Test Insurance",
        )

        fax = FaxesToSend.objects.create(
            hashed_email="test@example.com",
            email="test@example.com",
            paid=True,
            appeal_text="Test appeal text",
            denial_id=denial,
            pmids=["12345678", "87654321"],  # PubMed IDs
        )

        with patch(
            "fighthealthinsurance.common_view_logic.AppealAssemblyHelper._assemble_appeal_pdf"
        ) as mock_assemble:
            with tempfile.NamedTemporaryFile(
                mode="w+b", suffix=".pdf", delete=False
            ) as temp_file:
                temp_file.write(sample_pdf_bytes)
                temp_file.flush()
                temp_path = temp_file.name

            mock_assemble.return_value = temp_path

            try:
                fax.get_fax_document(return_encrypted=False)

                # Verify PubMed IDs were included
                call_kwargs = mock_assemble.call_args[1]
                assert call_kwargs["pubmed_ids_parsed"] == ["12345678", "87654321"]

            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)


class TestBackwardCompatibility:
    """Tests to ensure refactored methods maintain backward compatibility."""

    @pytest.fixture
    def sample_pdf_bytes(self):
        """Sample PDF content for testing."""
        return b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<\n/Type /Catalog\n/Pages 2 0 R\n>>\nendobj\n"

    @pytest.fixture
    def fax_with_encrypted_document(self, db, sample_pdf_bytes):
        """FaxesToSend with encrypted document."""
        denial = Denial.objects.create(
            hashed_email="test@example.com",
            denial_text="Test denial",
            insurance_company="Test Insurance",
        )

        fax = FaxesToSend.objects.create(
            hashed_email="test@example.com",
            email="test@example.com",
            paid=True,
            appeal_text="Test appeal text",
            denial_id=denial,
        )

        # Save document (EncryptedFileField handles encryption automatically)
        fax.combined_document_enc.save(
            "test_fax.pdf", ContentFile(sample_pdf_bytes), save=True
        )

        return fax

    @pytest.mark.django_db
    def test_get_contents_backward_compatible(
        self, fax_with_encrypted_document, sample_pdf_bytes
    ):
        """Test that _get_contents() still works after refactoring."""
        result = fax_with_encrypted_document._get_contents()

        assert result == sample_pdf_bytes
        assert isinstance(result, bytes)

    @pytest.mark.django_db
    def test_get_temporary_document_path_backward_compatible(
        self, fax_with_encrypted_document, sample_pdf_bytes
    ):
        """Test that get_temporary_document_path() still works after refactoring."""
        temp_path = fax_with_encrypted_document.get_temporary_document_path()

        try:
            # Verify temp file exists and contains correct data
            assert os.path.exists(temp_path)
            assert temp_path.endswith(".pdf")

            with open(temp_path, "rb") as f:
                contents = f.read()

            assert contents == sample_pdf_bytes

        finally:
            # Clean up temp file
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    @pytest.mark.django_db
    def test_get_temporary_document_path_with_regeneration(self, db, sample_pdf_bytes):
        """Test that get_temporary_document_path() regenerates when document missing."""
        denial = Denial.objects.create(
            hashed_email="test@example.com",
            denial_text="Test denial",
            insurance_company="Test Insurance",
        )

        fax = FaxesToSend.objects.create(
            hashed_email="test@example.com",
            email="test@example.com",
            paid=True,
            appeal_text="Test appeal text",
            denial_id=denial,
            name="Test Patient",
        )

        with patch(
            "fighthealthinsurance.common_view_logic.AppealAssemblyHelper._assemble_appeal_pdf"
        ) as mock_assemble:
            with tempfile.NamedTemporaryFile(
                mode="w+b", suffix=".pdf", delete=False
            ) as temp_file:
                temp_file.write(sample_pdf_bytes)
                temp_file.flush()
                generated_path = temp_file.name

            mock_assemble.return_value = generated_path

            try:
                temp_path = fax.get_temporary_document_path()

                # Verify temp file was created with regenerated content
                assert os.path.exists(temp_path)

                with open(temp_path, "rb") as f:
                    contents = f.read()

                assert contents == sample_pdf_bytes

                # Clean up
                if os.path.exists(temp_path):
                    os.unlink(temp_path)

            finally:
                if os.path.exists(generated_path):
                    os.unlink(generated_path)

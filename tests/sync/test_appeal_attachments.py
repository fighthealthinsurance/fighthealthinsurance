from django.test import TestCase, override_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from fighthealthinsurance.models import *
from django.urls import reverse
import json
from unittest import mock

from fighthealthinsurance import rest_serializers


@override_settings(FIGHT_PAPERWORK_ENABLED=True)
class AppealAttachmentTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        # Note: we need to use APIClient here not just regular client since we use the rest APIs
        # to setup a number of the users.
        self.client = APIClient()
        # Create the initial provider user has domain access
        professional_create_url = reverse("professional_user-list")
        self.domain = "newdomain"
        self.user_password = "newLongerPasswordMagicCheetoCheeto123"
        data = {
            "user_signup_info": {
                "username": "newprouser_domain_admin@example.com",
                "password": self.user_password,
                "email": "newprouser_domain_admin@example.com",
                "first_name": "New",
                "last_name": "User",
                "domain_name": self.domain,
                "visible_phone_number": "1234567891",
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": True,
            "skip_stripe": True,
            "user_domain": {
                "name": self.domain,
                "visible_phone_number": "1234567891",
                "internal_phone_number": "0987654322",
                "display_name": "New Domain",
                "country": "USA",
                "state": "CA",
                "city": "New City",
                "address1": "456 New St",
                "zipcode": "67890",
            },
        }
        response = self.client.post(professional_create_url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))
        # Create the initial patient user in same domainm (should have access)
        create_patient_url = reverse("patient_user-list")
        initial_patient_data = {
            "username": "newuserp1",
            "password": self.user_password,
            "email": "intiial_patient@example.com",
            "provider_phone_number": "1234567890",
            "country": "USA",
            "state": "CA",
            "city": "Test City",
            "address1": "123 Test St",
            "address2": "",
            "zipcode": "12345",
            "domain_name": self.domain,
        }
        response = self.client.post(
            create_patient_url, initial_patient_data, format="json"
        )
        # Activate the primary patient
        ipu = PatientUser.objects.get(
            user=User.objects.get(email="intiial_patient@example.com")
        )
        ipu.active = True
        ipu.user.is_active = True
        ipu.user.save()
        ipu.save()
        self.assertIn(response.status_code, range(200, 300))
        # Activate the domain admin
        self._professional_user_domain_admin = User.objects.get(
            email="newprouser_domain_admin@example.com"
        )
        self._professional_user_domain_admin.is_active = True
        self._professional_user_domain_admin.save()
        self.professional_user_domain_admin = ProfessionalUser.objects.get(
            user=self._professional_user_domain_admin
        )
        self.professional_user_domain_admin.active = True
        self.professional_user_domain_admin.save()
        self.professional_user_domain_admin_relation = (
            ProfessionalDomainRelation.objects.get(
                professional=self.professional_user_domain_admin,
                domain__name=self.domain,
            )
        )
        self.professional_user_domain_admin_relation.pending_domain_relation = False
        self.professional_user_domain_admin_relation.save()
        # Setup the patients
        self._primary_patient_user = User.objects.get(
            email="intiial_patient@example.com"
        )
        self.primary_patient_user = PatientUser.objects.get(
            user=self._primary_patient_user
        )
        self.primary_patient_user.active = True
        self.primary_patient_user.save()
        # Create the appeals manually
        self.appeal = Appeal.objects.create(
            appeal_text="This is a test appeal.",
            document_enc=SimpleUploadedFile("farts.pdf", b"Test PDF content"),
            patient_user=self.primary_patient_user,
            primary_professional=self.professional_user_domain_admin,
            domain=UserDomain.objects.get(name=self.domain),
        )

        self.test_file = SimpleUploadedFile(
            "test.pdf", b"file_content", content_type="application/pdf"
        )
        self.client.force_login(self._professional_user_domain_admin)

    def test_upload_attachment(self):
        response = self.client.post(
            reverse("appeal_attachments-list"),
            {"appeal_id": self.appeal.id, "file": self.test_file},
            format="multipart",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            AppealAttachment.objects.filter(
                appeal=self.appeal, filename="test.pdf"
            ).exists()
        )

    def test_list_attachments(self):
        attachment = AppealAttachment.objects.create(
            appeal=self.appeal,
            file=self.test_file,
            filename="test.pdf",
            mime_type="application/pdf",
        )

        response = self.client.get(
            reverse("appeal_attachments-list") + f"?appeal_id={self.appeal.id}"
        )
        self.assertEqual(response.status_code, 200)
        loaded = json.loads(response.content)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["filename"], "test.pdf")

    def test_delete_attachment(self):
        attachment = AppealAttachment.objects.create(
            appeal=self.appeal,
            file=self.test_file,
            filename="test.pdf",
            mime_type="application/pdf",
        )

        response = self.client.delete(
            reverse("appeal_attachments-detail", kwargs={"pk": attachment.id})
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(AppealAttachment.objects.filter(id=attachment.id).exists())

    def test_upload_over_the_size_limit_is_refused(self):
        with mock.patch.object(rest_serializers, "MAX_ATTACHMENT_BYTES", 4):
            response = self.client.post(
                reverse("appeal_attachments-list"),
                {"appeal_id": self.appeal.id, "file": self.test_file},
                format="multipart",
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(AppealAttachment.objects.filter(appeal=self.appeal).exists())

    def test_download_over_the_size_limit_is_refused_before_decrypting(self):
        upload = self.client.post(
            reverse("appeal_attachments-list"),
            {"appeal_id": self.appeal.id, "file": self.test_file},
            format="multipart",
        )
        self.assertEqual(upload.status_code, 201)
        with (
            mock.patch("fighthealthinsurance.rest_views.attachment_read_limit", return_value=4),
            mock.patch("fighthealthinsurance.rest_views.Cryptographer.decrypted") as decrypted,
        ):
            response = self.client.get(
                reverse("appeal_attachments-detail", kwargs={"pk": upload.json()["id"]})
            )
        self.assertEqual(response.status_code, 413)
        decrypted.assert_not_called()

    def test_owner_download_returns_decrypted_file(self):
        upload = self.client.post(
            reverse("appeal_attachments-list"),
            {"appeal_id": self.appeal.id, "file": self.test_file},
            format="multipart",
        )
        self.assertEqual(upload.status_code, 201)
        attachment_id = upload.json()["id"]

        response = self.client.get(
            reverse("appeal_attachments-detail", kwargs={"pk": attachment_id})
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"file_content")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn('filename="test.pdf"', response["Content-Disposition"])

    def test_other_professional_cannot_download_attachment(self):
        attachment = AppealAttachment.objects.create(
            appeal=self.appeal,
            file=self.test_file,
            filename="test.pdf",
            mime_type="application/pdf",
        )
        outsider = User.objects.create_user(
            username="rebecca.crumpler@example.com",
            email="rebecca.crumpler@example.com",
            password=self.user_password,
        )
        ProfessionalUser.objects.create(user=outsider, active=True)
        self.client.force_login(outsider)

        response = self.client.get(
            reverse("appeal_attachments-detail", kwargs={"pk": attachment.id})
        )

        self.assertEqual(response.status_code, 404)

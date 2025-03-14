import uuid
import json
from django.urls.converters import register_converter
from django.test import TestCase
from rest_framework.test import APIClient
from django.urls import reverse
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile

from fighthealthinsurance.models import Appeal
from fhi_users.models import (
    UserDomain,
    PatientUser,
    ProfessionalUser,
    ProfessionalDomainRelation,
)

# ------------------------------------------------------------------
# Custom converter to accept our custom formatted UUIDs.
# This converter accepts any non-slash string and, if necessary,
# strips a prefix (e.g. "cmnw8e-") and converts the remaining 32-digit
# hex string into a proper UUID.
# ------------------------------------------------------------------
class CustomUUIDConverter:
    regex = '[^/]+'  # accept any string with no slash

    def to_python(self, value):
        try:
            # First, try to interpret the whole value as a UUID.
            return uuid.UUID(value)
        except ValueError:
            # If that fails, check if there is a prefix separated by a dash.
            if '-' in value:
                prefix, potential_uuid = value.split('-', 1)
                # If the potential_uuid is 32 hex characters without dashes,
                # insert dashes in the proper positions.
                if len(potential_uuid) == 32:
                    try:
                        formatted = f"{potential_uuid[0:8]}-{potential_uuid[8:12]}-{potential_uuid[12:16]}-{potential_uuid[16:20]}-{potential_uuid[20:32]}"
                        return uuid.UUID(formatted)
                    except ValueError:
                        pass
                # Otherwise, try converting the part after the dash directly.
                try:
                    return uuid.UUID(potential_uuid)
                except ValueError:
                    pass
            raise ValueError(f"{value} is not a valid UUID")

    def to_url(self, value):
        return str(value)

# Register the custom converter for "uuid".
register_converter(CustomUUIDConverter, 'uuid')

# ------------------------------------------------------------------
# Test cases for appeal file view.
# ------------------------------------------------------------------
class AppealFileViewTest(TestCase):
    def setUp(self):
        # Use APIClient for REST API calls.
        self.client = APIClient()

        # Create the initial provider user with domain access.
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

        # Create a second provider user in the same domain (should not have access).
        data = {
            "user_signup_info": {
                "username": "newprouser_unrelated@example.com",
                "password": self.user_password,
                "email": "newprouser_unrelated@example.com",
                "first_name": "New",
                "last_name": "User",
                "domain_name": self.domain,
                "visible_phone_number": "1234567891",
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": False,
        }
        response = self.client.post(professional_create_url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))

        # Create a third provider user in the same domain (this one creates the appeal so should have access).
        data = {
            "user_signup_info": {
                "username": "newprouser_creator@example.com",
                "password": self.user_password,
                "email": "newprouser_creator@example.com",
                "first_name": "New",
                "last_name": "User",
                "domain_name": self.domain,
                "visible_phone_number": "1234567891",
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": False,
        }
        response = self.client.post(professional_create_url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))

        # Create the initial patient user in the same domain (should have access).
        create_patient_url = reverse("patient_user-list")
        initial_patient_data = {
            "username": "initiial_patient@example.com",
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
        response = self.client.post(create_patient_url, initial_patient_data, format="json")
        self.assertIn(response.status_code, range(200, 300))

        # Activate the primary patient.
        ipu = PatientUser.objects.get(user=User.objects.get(email="intiial_patient@example.com"))
        ipu.active = True
        ipu.user.is_active = True
        ipu.user.save()
        ipu.save()

        # Create a second patient user in the same domain (should not have access).
        second_patient_data = {
            "username": "secondary_patient@example.com",
            "password": self.user_password,
            "email": "secondary_patient@example.com",
            "provider_phone_number": "1234567890",
            "country": "USA",
            "state": "CA",
            "city": "Test City",
            "address1": "123 Test St",
            "address2": "",
            "zipcode": "12345",
            "domain_name": self.domain,
        }
        response = self.client.post(create_patient_url, second_patient_data, format="json")
        self.assertIn(response.status_code, range(200, 300))

        # Activate the secondary patient.
        spu = PatientUser.objects.get(user=User.objects.get(email="secondary_patient@example.com"))
        spu.active = True
        spu.user.is_active = True
        spu.user.save()
        spu.save()

        # Get the provider user who will create the appeal.
        self._professional_user = User.objects.get(email="newprouser_creator@example.com")
        self._professional_user.is_active = True
        self._professional_user.save()
        self.professional_user = ProfessionalUser.objects.get(user=self._professional_user)
        self.professional_user.active = True
        self.professional_user.save()
        self.professional_user_domain_relation = ProfessionalDomainRelation.objects.get(
            professional=self.professional_user, domain__name=self.domain
        )
        self.professional_user_domain_relation.pending = False
        self.professional_user_domain_relation.save()

        # Activate the domain admin.
        self._professional_user_domain_admin = User.objects.get(email="newprouser_domain_admin@example.com")
        self._professional_user_domain_admin.is_active = True
        self._professional_user_domain_admin.save()
        self.professional_user_domain_admin = ProfessionalUser.objects.get(user=self._professional_user_domain_admin)
        self.professional_user_domain_admin.active = True
        self.professional_user_domain_admin.save()
        self.professional_user_domain_admin_relation = ProfessionalDomainRelation.objects.get(
            professional=self.professional_user_domain_admin, domain__name=self.domain
        )
        self.professional_user_domain_admin_relation.pending = False
        self.professional_user_domain_admin_relation.save()

        # Activate the unrelated professional user.
        self._professional_user_unrelated = User.objects.get(email="newprouser_unrelated@example.com")
        self._professional_user_unrelated.is_active = True
        self._professional_user_unrelated.save()
        self.professional_user_unrelated = ProfessionalUser.objects.get(user=self._professional_user_unrelated)
        self.professional_user_unrelated.active = True
        self.professional_user_unrelated.save()
        # (Assuming a corresponding DomainRelation exists; adjust as needed.)

        # Setup the patients.
        self._primary_patient_user = User.objects.get(email="intiial_patient@example.com")
        self._secondary_patient_user = User.objects.get(email="secondary_patient@example.com")
        self.primary_patient_user = PatientUser.objects.get(user=self._primary_patient_user)
        self.primary_patient_user.active = True
        self.primary_patient_user.save()
        self.secondary_patient_user = PatientUser.objects.get(user=self._secondary_patient_user)

        # Create appeals.
        self.appeal = Appeal.objects.create(
            appeal_text="This is a test appeal.",
            document_enc=SimpleUploadedFile("farts.pdf", b"Test PDF content"),
            patient_user=self.primary_patient_user,
            primary_professional=self.professional_user,
            domain=UserDomain.objects.get(name=self.domain),
        )
        self.secondary_appeal = Appeal.objects.create(
            appeal_text="This is a test appeal.",
            document_enc=SimpleUploadedFile("farts.pdf", b"Test PDF content"),
            patient_user=self.secondary_patient_user,
            primary_professional=self.professional_user,
            domain=UserDomain.objects.get(name=self.domain),
        )

    def do_login(self, username, password, client=None):
        url = reverse("rest_login-login")
        data = {
            "username": username,
            "password": password,
            "domain": self.domain,
            "phone": "",
        }
        if client is None:
            client = self.client
        response = client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))

    def test_appeal_file_view_unauthenticated(self):
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 401)

    def test_appeal_file_view_authenticated_admin(self):
        # Check for the domain admin.
        self.do_login(username="newprouser_domain_admin@example.com", password=self.user_password)
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_appeal_file_view_authenticated_combined(self):
        # Check for patient access.
        self.do_login(username="initiial_patient@example.com", password=self.user_password)
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        # Check for provider access.
        self.do_login(username="newprouser_creator@example.com", password=self.user_password)
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_appeal_file_view_authenticated_incorrect(self):
        # Test with a different patient.
        self.do_login(username="secondary_patient@example.com", password=self.user_password)
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 404)
        # Test with a non-domain admin provider.
        self.do_login(username="newprouser_unrelated@example.com", password=self.user_password)
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 404)

    def test_appeal_file_view_wrong_http_method(self):
        # Test wrong HTTP methods.
        self.do_login(username="initiial_patient@example.com", password=self.user_password)
        for method in ["post", "put", "patch", "delete"]:
            response = getattr(self.client, method)(
                reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
            )
            self.assertEqual(response.status_code, 405)  # Method Not Allowed

    def test_appeal_file_view_suspended_professional(self):
        # Test access when professional is suspended.
        self.do_login(username="newprouser_creator@example.com", password=self.user_password)
        self.professional_user.active = False
        self.professional_user.save()
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 404)

    def test_appeal_file_view_concurrent_access(self):
        # Test concurrent access from the same user in different sessions.
        self.do_login(username="initiial_patient@example.com", password=self.user_password)
        client2 = APIClient()
        self.do_login(username="initiial_patient@example.com", password=self.user_password, client=client2)
        response1 = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        response2 = client2.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response1.status_code, 200)
        self.assertEqual(response2.status_code, 200)

    def test_appeal_file_view_large_file(self):
        # Test handling of large files (e.g., 5MB).
        large_content = b"x" * (5 * 1024 * 1024)
        large_appeal = Appeal.objects.create(
            appeal_text="Large file test",
            document_enc=SimpleUploadedFile("large.pdf", large_content),
            patient_user=self.primary_patient_user,
            primary_professional=self.professional_user,
            domain=UserDomain.objects.get(name=self.domain),
        )
        self.do_login(username="initiial_patient@example.com", password=self.user_password)
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": large_appeal.uuid})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.content), len(large_content))

    def test_appeal_file_view_permission_changes(self):
        # Test access after permission changes.
        self.do_login(username="initiial_patient@example.com", password=self.user_password)
        response1 = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response1.status_code, 200)
        # Remove permissions.
        self.primary_patient_user.active = False
        self.primary_patient_user.save()
        response2 = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response2.status_code, 404)

    def test_appeal_file_view_corrupted_file(self):
        # Test handling of corrupted files.
        corrupted_appeal = Appeal.objects.create(
            appeal_text="Corrupted file test",
            document_enc=SimpleUploadedFile("corrupted.pdf", b"Invalid PDF content"),
            patient_user=self.primary_patient_user,
            primary_professional=self.professional_user,
            domain=UserDomain.objects.get(name=self.domain),
        )
        self.do_login(username="initiial_patient@example.com", password=self.user_password)
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": corrupted_appeal.uuid})
        )
        self.assertEqual(response.status_code, 200)

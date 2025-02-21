import time
from django.test import TestCase
from rest_framework.test import APIClient
from django.urls import reverse
from django.contrib.auth.models import User
from fighthealthinsurance.models import Appeal
from fhi_users.models import (
    UserDomain,
    PatientUser,
    ProfessionalUser,
    ProfessionalDomainRelation,
)
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from rest_framework import status
from fhi_users.models import ExtraUserProperties


class AppealFileViewTest(TestCase):
    def setUp(self):
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
        # Create a second provider user in same domain (should not have access)
        data = {
            "user_signup_info": {
                "username": "newprouser_unrelated",
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
        # Create a third provider user in same domain (this one creates the appeal so should have access)
        data = {
            "user_signup_info": {
                "username": "newprouser_creator",
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
        # Create a second patient user in the same domain (should not have access)
        second_patient_data = {
            "username": "newuserp2",
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
        response = self.client.post(
            create_patient_url, second_patient_data, format="json"
        )
        self.assertIn(response.status_code, range(200, 300))
        # Activate the secondary patient
        spu = PatientUser.objects.get(
            user=User.objects.get(email="secondary_patient@example.com")
        )
        spu.active = True
        spu.user.is_active = True
        spu.user.save()
        spu.save()
        # Get the pro user
        self._professional_user = User.objects.get(
            email="newprouser_creator@example.com"
        )
        self._professional_user.is_active = True
        self._professional_user.save()
        self.professional_user = ProfessionalUser.objects.get(
            user=self._professional_user
        )
        self.professional_user.active = True
        self.professional_user.save()
        self.professional_user_domain_relation = ProfessionalDomainRelation.objects.get(
            professional=self.professional_user, domain__name=self.domain
        )
        self.professional_user_domain_relation.pending = False
        self.professional_user_domain_relation.save()

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
        self.professional_user_domain_admin_relation.pending = False
        self.professional_user_domain_admin_relation.save()

        # Activate the unrelated professional user
        self._professional_user_unrelated = User.objects.get(
            email="newprouser_unrelated@example.com"
        )
        self._professional_user_unrelated.is_active = True
        self._professional_user_unrelated.save()
        self.professional_user_unrelated = ProfessionalUser.objects.get(
            user=self._professional_user_unrelated
        )
        self.professional_user_unrelated.active = True
        self.professional_user_unrelated.save()
        self.professional_user_unrelated = ProfessionalDomainRelation.objects.get(
            professional=self.professional_user_unrelated, domain__name=self.domain
        )
        self.professional_user_domain_relation.pending = False
        self.professional_user_domain_relation.save()

        # Setup the patients
        self._primary_patient_user = User.objects.get(
            email="intiial_patient@example.com"
        )
        self._secondary_patient_user = User.objects.get(
            email="secondary_patient@example.com"
        )
        self.primary_patient_user = PatientUser.objects.get(
            user=self._primary_patient_user
        )
        self.primary_patient_user.active = True
        self.primary_patient_user.save()
        self.secondary_patient_user = PatientUser.objects.get(
            user=self._secondary_patient_user
        )
        # Create the appeals manually
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
        # Check for the domain admin
        self.do_login(
            username="newprouser_domain_admin@example.com",
            password=self.user_password,
        )
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_appeal_file_view_authenticated_combined(self):
        # Check for self
        self.do_login(username="newuserp1", password=self.user_password)
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        # Check for provider
        self.do_login(
            username="newprouser_creator",
            password=self.user_password,
        )
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_appeal_file_view_authenticated_incorrect(self):
        # Check for different patient
        self.do_login(username="newuserp2", password=self.user_password)
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 404)
        # For different non-domain admin provider
        self.do_login(
            username="newprouser_unrelated",
            password=self.user_password,
        )
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 404)

    def test_appeal_file_view_wrong_http_method(self):
        # Test wrong HTTP methods
        self.do_login(username="newuserp1", password=self.user_password)
        for method in ["post", "put", "patch", "delete"]:
            response = getattr(self.client, method)(
                reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
            )
            self.assertEqual(response.status_code, 405)  # Method Not Allowed

    def test_appeal_file_view_suspended_professional(self):
        # Test access when professional is suspended
        self.do_login(
            username="newprouser_creator",
            password=self.user_password,
        )
        self.professional_user.active = False
        self.professional_user.save()
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 404)

    def test_appeal_file_view_concurrent_access(self):
        # Test concurrent access from same user different sessions
        self.do_login(username="newuserp1", password=self.user_password)
        client2 = APIClient()
        self.do_login(username="newuserp1", password=self.user_password, client=client2)

        response1 = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        response2 = client2.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )

        self.assertEqual(response1.status_code, 200)
        self.assertEqual(response2.status_code, 200)

    def test_appeal_file_view_large_file(self):
        # Test handling of large files (e.g., 5MB)
        large_content = b"x" * (5 * 1024 * 1024)
        large_appeal = Appeal.objects.create(
            appeal_text="Large file test",
            document_enc=SimpleUploadedFile("large.pdf", large_content),
            patient_user=self.primary_patient_user,
            primary_professional=self.professional_user,
            domain=UserDomain.objects.get(name=self.domain),
        )

        self.do_login(username="newuserp1", password=self.user_password)
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": large_appeal.uuid})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.content), len(large_content))

    def test_appeal_file_view_permission_changes(self):
        # Test access after permission changes
        self.do_login(username="newuserp1", password=self.user_password)

        # Initial access
        response1 = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response1.status_code, 200)

        # Remove permissions
        self.primary_patient_user.active = False
        self.primary_patient_user.save()

        # Try access after permission removal
        response2 = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response2.status_code, 404)

    def test_appeal_file_view_corrupted_file(self):
        # Test handling of corrupted files
        corrupted_appeal = Appeal.objects.create(
            appeal_text="Corrupted file test",
            document_enc=SimpleUploadedFile("corrupted.pdf", b"Invalid PDF content"),
            patient_user=self.primary_patient_user,
            primary_professional=self.professional_user,
            domain=UserDomain.objects.get(name=self.domain),
        )

        self.do_login(username="newuserp1", password=self.user_password)
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": corrupted_appeal.uuid})
        )
        self.assertEqual(response.status_code, 200)

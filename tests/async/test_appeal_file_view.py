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


class AppealFileViewTest(TestCase):
    def setUp(self):
        # Note: we need to use APIClient here not just regular client since we use the rest APIs
        # to setup a number of the users.
        self.client = APIClient()
        # Create the initial provider user has domain access
        professional_create_url = reverse("professional_user-list")
        self.domain = "newdomain"
        data = {
            "user_signup_info": {
                "username": "newprouser_domain_admin",
                "password": "newLongerPasswordMagicCheetoCheeto123",
                "email": "newprouser_domain_admin@example.com",
                "first_name": "New",
                "last_name": "User",
                "domain_name": self.domain,
                "visible_phone_number": "1234567891",
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": True,
            "user_domain": {
                "name": "newdomain",
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
                "password": "newLongerPasswordMagicCheetoCheeto123",
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
                "password": "newLongerPasswordMagicCheetoCheeto123",
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
        create_patient_url = reverse("create_patient_user-list")
        initial_patient_data = {
            "username": "newuserp1",
            "password": "newLongerPasswordMagicCheetoCheeto123",
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
        ipu = PatientUser.objects.get(
            user=User.objects.get(email="intiial_patient@example.com")
        )
        ipu.active = True
        ipu.save()
        self.assertIn(response.status_code, range(200, 300))
        # Create a second patient user in the same domain (should not have access)
        second_patient_data = {
            "username": "newuserp2",
            "password": "newLongerPasswordMagicCheetoCheeto123",
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
        # Activate the pro user
        self._professional_user = User.objects.get(
            email="newprouser_creator@example.com"
        )
        self.professional_user = ProfessionalUser.objects.get(
            user=self._professional_user
        )
        self.professional_user.active = False
        self.professional_user.save()

        # Activate the domain admin
        self._professional_user_domain_admin = User.objects.get(
            email="newprouser_domain_admin@example.com"
        )
        self.professional_user_domain_admin = ProfessionalUser.objects.get(
            user=self._professional_user_domain_admin
        )
        self.professional_user_domain_admin.active = True
        self.professional_user_domain_admin.save()
        self.professional_user_domain_relation = ProfessionalDomainRelation.objects.get(
            professional=self.professional_user_domain_admin, domain__name=self.domain
        )
        self.professional_user_domain_relation.pending = False
        self.professional_user_domain_relation.save()

        # Activate the unrelated professional user
        self._professional_user_unrelated = User.objects.get(
            email="newprouser_unrelated@example.com"
        )
        self.professional_user_unrelated = ProfessionalUser.objects.get(
            user=self._professional_user_unrelated
        )
        self.professional_user_unrelated.active = False
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

    def do_login(self, username, password):
        url = reverse("rest_login-login")
        data = {
            "username": username,
            "password": password,
            "domain": self.domain,
            "phone": "",
        }
        response = self.client.post(url, data, format="json")
        self.assertIn(response.status_code, range(200, 300))

    def test_appeal_file_view_unauthenticated(self):
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 401)

    def test_appeal_file_view_authenticated_admin(self):
        # Check for the domain admin
        self.do_login(
            username="newprouser_domain_admin",
            password="newLongerPasswordMagicCheetoCheeto123",
        )
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_appeal_file_view_authenticated(self):
        # Check for self
        self.do_login(
            username="newuserp1", password="newLongerPasswordMagicCheetoCheeto123"
        )
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        # Check for provider
        self.do_login(
            username="newprouser_creator",
            password="newLongerPasswordMagicCheetoCheeto123",
        )
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        # Secondary patient is not activated so they should not be able to see their appeal yet.
        self.do_login(
            username="newuserp2", password="newLongerPasswordMagicCheetoCheeto123"
        )
        response = self.client.get(
            reverse(
                "appeal_file_view", kwargs={"appeal_uuid": self.secondary_appeal.uuid}
            )
        )
        self.assertEqual(response.status_code, 404)

    def test_appeal_file_view_authenticated_incorrect(self):
        # Check for different patient
        self.do_login(
            username="newuserp2", password="newLongerPasswordMagicCheetoCheeto123"
        )
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )
        self.assertEqual(response.status_code, 404)
        # For different non-domain admin provider
        self.do_login(
            username="newprouser_unrelated",
            password="newLongerPasswordMagicCheetoCheeto123",
        )
        response = self.client.get(
            reverse("appeal_file_view", kwargs={"appeal_uuid": self.appeal.uuid})
        )

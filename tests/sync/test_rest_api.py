"""Test the rest API functionality"""

from asgiref.sync import sync_to_async, async_to_sync

import pytest
from channels.testing import WebsocketCommunicator

import typing

import hashlib
import os
import time
import sys
import json

from django.urls import reverse
from django.contrib.auth import get_user_model


from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance.models import (
    Denial,
    UserDomain,
    ExtraUserProperties,
    ProfessionalUser,
    Appeal,
    PatientUser,
    SecondaryAppealProfessionalRelation,
)
from fighthealthinsurance.websockets import (
    StreamingEntityBackend,
    StreamingAppealsBackend,
)

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class Delete(APITestCase):
    """Test just the delete API."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def test_url_root(self):
        url = reverse("dataremoval-list")
        email = "timbit@fighthealthinsurance.com"
        hashed_email = hashlib.sha512(email.encode("utf-8")).hexdigest()
        # Create the object
        Denial.objects.create(denial_text="test", hashed_email=hashed_email).save()
        denials_for_user_count = Denial.objects.filter(
            hashed_email=hashed_email
        ).count()
        assert denials_for_user_count > 0
        # Delete it
        response = self.client.delete(
            url, json.dumps({"email": email}), content_type="application/json"
        )
        self.assertTrue(status.is_success(response.status_code))
        # Make sure we did that
        denials_for_user_count = Denial.objects.filter(
            hashed_email=hashed_email
        ).count()
        assert denials_for_user_count == 0


class DenialLongEmployerName(APITestCase):
    """Test denial with long employer name."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )
        self.user = User.objects.create_user(
            username=f"testuser🐼{self.domain.id}",
            password="testpass",
            email="test@example.com",
        )
        self.username = f"testuser🐼{self.domain.id}"
        self.password = "testpass"
        self.prouser = ProfessionalUser.objects.create(
            user=self.user, active=True, npi_number="1234567890"
        )
        self.user.is_active = True
        self.user.save()
        ExtraUserProperties.objects.create(user=self.user, email_verified=True)

    def test_long_employer_name(self):
        # Now we need to log in
        login_result = self.client.login(username=self.username, password=self.password)
        denial_text = "Group Name: "
        for a in range(0, 300):
            denial_text += str(a)
        denial_text += "INC "
        url = reverse("denials-list")
        email = "timbit@fighthealthinsurance.com"
        hashed_email = hashlib.sha512(email.encode("utf-8")).hexdigest()
        denials_for_user_count = Denial.objects.filter(
            hashed_email=hashed_email
        ).count()
        assert denials_for_user_count == 0
        # Create a denial
        response = self.client.post(
            url,
            json.dumps(
                {
                    "email": email,
                    "denial_text": denial_text,
                    "pii": "true",
                    "tos": "true",
                    "privacy": "true",
                    "store_raw_email": "true",  # Store the raw e-mail for the follow-up form
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))
        denials_for_user_count = Denial.objects.filter(
            hashed_email=hashed_email,
        ).count()
        assert denials_for_user_count == 1


class DenialEndToEnd(APITestCase):
    """Test end to end, we need to load the initial fixtures so we have denial types."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )
        self.user = User.objects.create_user(
            username=f"testuser🐼{self.domain.id}",
            password="testpass",
            email="test@example.com",
        )
        self.username = f"testuser🐼{self.domain.id}"
        self.password = "testpass"
        self.prouser = ProfessionalUser.objects.create(
            user=self.user, active=True, npi_number="1234567890"
        )
        self.user.is_active = True
        self.user.save()
        ExtraUserProperties.objects.create(user=self.user, email_verified=True)

    @pytest.mark.asyncio
    async def test_denial_end_to_end(self):
        login_result = await sync_to_async(self.client.login)(
            username=self.username, password=self.password
        )
        self.assertTrue(login_result)
        url = reverse("denials-list")
        email = "timbit@fighthealthinsurance.com"
        hashed_email = Denial.get_hashed_email(email)
        denials_for_user_count = await Denial.objects.filter(
            hashed_email=hashed_email
        ).acount()
        assert denials_for_user_count == 0
        # Create a denial
        response = await sync_to_async(self.client.post)(
            url,
            json.dumps(
                {
                    "email": email,
                    "denial_text": "test",
                    "pii": "true",
                    "tos": "true",
                    "privacy": "true",
                    "store_raw_email": "true",  # Store the raw e-mail for the follow-up form
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))
        parsed = response.json()
        denial_id = parsed["denial_id"]
        print(f"Using '{denial_id}'")
        semi_sekret = parsed["semi_sekret"]
        # Make sure we added a denial for this user
        denials_for_user_count = await Denial.objects.filter(
            hashed_email=hashed_email,
        ).acount()
        assert denials_for_user_count > 0
        # Make sure we can get the denial
        denial = await Denial.objects.filter(
            hashed_email=hashed_email, denial_id=denial_id
        ).aget()
        print(f"We should find {denial}")
        # Now we need to poke entity extraction, this part is async
        seb_communicator = WebsocketCommunicator(
            StreamingEntityBackend.as_asgi(), "/testws/"
        )
        connected, subprotocol = await seb_communicator.connect()
        assert connected
        await seb_communicator.send_json_to(
            {
                "email": email,
                "semi_sekret": semi_sekret,
                "denial_id": denial_id,
            }
        )
        # We should receive at least one frame.
        response = await seb_communicator.receive_from()
        # Now consume all of the rest of them until done.
        try:
            while True:
                response = await seb_communicator.receive_from()
        except:
            pass
        finally:
            await seb_communicator.disconnect()
        # Ok now lets get the additional info
        find_next_steps_url = reverse("nextsteps-list")
        find_next_steps_response = await sync_to_async(self.client.post)(
            find_next_steps_url,
            json.dumps(
                {
                    "email": email,
                    "semi_sekret": semi_sekret,
                    "denial_id": denial_id,
                    "denial_type": [1, 2],
                    "diagnosis": "high risk homosexual behaviour",
                }
            ),
            content_type="application/json",
        )
        find_next_steps_parsed = find_next_steps_response.json()
        # Make sure we got back a reasonable set of questions.
        assert len(find_next_steps_parsed["combined_form"]) == 5
        assert list(find_next_steps_parsed["combined_form"][0].keys()) == [
            "name",
            "field_type",
            "label",
            "visible",
            "required",
            "help_text",
            "initial",
            "type",
        ]
        # Now we need to poke at the appeal creator
        # Now we need to poke entity extraction, this part is async
        a_communicator = WebsocketCommunicator(
            StreamingAppealsBackend.as_asgi(), "/testws/"
        )
        connected, subprotocol = await a_communicator.connect()
        assert connected
        await a_communicator.send_json_to(
            {
                "email": email,
                "semi_sekret": semi_sekret,
                "medical_reason": "preventive",
                "age": "30",
                "in_network": True,
                "denial_id": denial_id,
            }
        )
        responses = []
        # We should receive at least one frame.
        responses.append(await a_communicator.receive_from(timeout=60))
        # Now consume all of the rest of them until done.
        try:
            while True:
                responses.append(await a_communicator.receive_from(timeout=60))
        except Exception as e:
            print(f"Error {e}")
            pass
        finally:
            await a_communicator.disconnect()
        print(f"Received responses {responses}")
        responses = list(filter(lambda x: len(x) > 4, responses))
        # It's a streaming response with one per new line
        appeal = json.loads(responses[0])
        assert appeal["content"].startswith("Dear")
        # Now lets go ahead and provide follow up
        denial = await Denial.objects.aget(denial_id=denial_id)
        followup_url = reverse("followups-list")
        followup_response = await sync_to_async(self.client.post)(
            followup_url,
            json.dumps(
                {
                    "denial_id": denial_id,
                    "uuid": str(denial.uuid),
                    "hashed_email": denial.hashed_email,
                    "user_comments": "test",
                    "appeal_result": "Yes",
                    "follow_up_again": True,
                    "follow_up_semi_sekret": denial.follow_up_semi_sekret,
                }
            ),
            content_type="application/json",
        )
        print(followup_response)
        self.assertTrue(status.is_success(followup_response.status_code))


class NotifyPatientTest(APITestCase):
    """Test the notify_patient API endpoint."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Create domain
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )

        # Create professional user
        self.pro_user = User.objects.create_user(
            username=f"prouser🐼{self.domain.id}",
            password="testpass",
            email="pro@example.com",
        )
        self.pro_username = f"prouser🐼{self.domain.id}"
        self.pro_password = "testpass"
        self.professional = ProfessionalUser.objects.create(
            user=self.pro_user, active=True, npi_number="1234567890"
        )
        self.pro_user.is_active = True
        self.pro_user.save()
        ExtraUserProperties.objects.create(user=self.pro_user, email_verified=True)

        # Create patient user
        self.patient_user = User.objects.create_user(
            username="patientuser",
            password="patientpass",
            email="patient@example.com",
            first_name="Test",
            last_name="Patient"
        )
        self.patient_user.is_active = True
        self.patient_user.save()
        self.patient = PatientUser.objects.create(
            user=self.patient_user
        )

        # Create a denial
        self.denial = Denial.objects.create(
            denial_text="Test denial",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient,
            hashed_email=Denial.get_hashed_email(self.patient_user.email),
        )

        # Create an appeal
        self.appeal = Appeal.objects.create(
            for_denial=self.denial,
            pending=True,
            patient_user=self.patient,
            primary_professional=self.professional,
            creating_professional=self.professional,
        )

        # Set up session
        self.client.login(username=self.pro_username, password=self.pro_password)
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

    def test_notify_patient(self):
        url = reverse("appeals-notify-patient")

        # Test with professional name included
        response = self.client.post(
            url,
            json.dumps({"id": self.appeal.id, "include_professional": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("message", response.json())
        self.assertEqual(response.json()["message"], "Notification sent")

        # Test without professional name
        response = self.client.post(
            url,
            json.dumps({"id": self.appeal.id, "include_professional": False}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("message", response.json())
        self.assertEqual(response.json()["message"], "Notification sent")

    def test_notify_patient_inactive_user(self):
        # Set patient user to inactive to test invitation flow
        self.patient_user.is_active = False
        self.patient_user.save()

        url = reverse("appeals-notify-patient")
        response = self.client.post(
            url,
            json.dumps({"id": self.appeal.id, "professional_name": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("message", response.json())
        self.assertEqual(response.json()["message"], "Notification sent")


class SendFaxTest(APITestCase):
    """Test the send_fax API endpoint."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Create domain
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )

        # Create professional user
        self.pro_user = User.objects.create_user(
            username=f"prouser🐼{self.domain.id}",
            password="testpass",
            email="pro@example.com",
        )
        self.pro_username = f"prouser🐼{self.domain.id}"
        self.pro_password = "testpass"
        self.professional = ProfessionalUser.objects.create(
            user=self.pro_user, active=True, npi_number="1234567890"
        )
        self.pro_user.is_active = True
        self.pro_user.save()
        ExtraUserProperties.objects.create(user=self.pro_user, email_verified=True)

        # Create patient user
        self.patient_user = User.objects.create_user(
            username="patientuser",
            password="patientpass",
            email="patient@example.com",
            first_name="Test",
            last_name="Patient",
        )
        self.patient_user.is_active = True
        self.patient_user.save()
        self.patient = PatientUser.objects.create(
            user=self.patient_user,
            active=True,
        )

        # Create a denial with appeal text
        self.denial = Denial.objects.create(
            denial_text="Test denial",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient,
            hashed_email=Denial.get_hashed_email(self.patient_user.email),
            appeal_fax_number="5551234567",
            patient_visible=True,
        )

        # Create an appeal with text
        self.appeal = Appeal.objects.create(
            for_denial=self.denial,
            pending=True,
            patient_user=self.patient,
            primary_professional=self.professional,
            creating_professional=self.professional,
            appeal_text="!This is a test appeal letter",
            patient_visible=True,
        )

        # Set up session for professional
        self.client.login(username=self.pro_username, password=self.pro_password)
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

    def test_send_fax_as_professional(self):
        url = reverse("appeals-send-fax")

        response = self.client.post(
            url,
            json.dumps({"appeal_id": self.appeal.id, "fax_number": "5559876543"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        # Verify the fax number was updated
        updated_appeal = Appeal.objects.get(id=self.appeal.id)
        self.assertEqual(updated_appeal.pending, False)
        self.assertEqual(updated_appeal.pending_patient, False)
        self.assertEqual(updated_appeal.pending_professional, False)

    def test_send_fax_aspatient_no_permissions(self):
        # Login as patient
        self.client.logout()
        self.client.login(username="patientuser", password="patientpass")
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

        # Set the appeal to require professional finishing
        self.denial.professional_to_finish = True
        self.denial.save()

        url = reverse("appeals-send-fax")

        response = self.client.post(
            url,
            json.dumps({"appeal_id": self.appeal.id, "fax_number": "5559876543"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Pending", response.json()["message"])


        # Verify the pending flags were updated correctly
        self.appeal.refresh_from_db()
        self.assertEqual(self.appeal.pending, True)
        self.assertEqual(self.appeal.pending_patient, False)
        self.assertEqual(self.appeal.pending_professional, True)

    def test_send_fax_aspatient_with_permissions(self):
        # Login as patient
        self.client.logout()
        self.client.login(username="patientuser", password="patientpass")
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

        # Set the appeal to allow the patient to finish
        self.denial.professional_to_finish = False
        self.denial.save()

        url = reverse("appeals-send-fax")

        response = self.client.post(
            url,
            json.dumps({"appeal_id": self.appeal.id, "fax_number": "5559876543"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.appeal.refresh_from_db()
        self.assertEqual(self.appeal.pending, False)






class InviteProviderTest(APITestCase):
    """Test the invite_provider API endpoint."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Create domain
        self.domain = UserDomain.objects.create(
            name="testdomain",
            visible_phone_number="1234567890",
            internal_phone_number="0987654321",
            active=True,
            display_name="Test Domain",
            business_name="Test Business",
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )

        # Create primary professional user
        self.primary_pro_user = User.objects.create_user(
            username=f"primary_pro🐼{self.domain.id}",
            password="testpass",
            email="primary@example.com",
        )
        self.primary_pro_username = f"primary_pro🐼{self.domain.id}"
        self.primary_pro_password = "testpass"
        self.primary_professional = ProfessionalUser.objects.create(
            user=self.primary_pro_user, active=True, npi_number="1234567890"
        )
        self.primary_pro_user.is_active = True
        self.primary_pro_user.save()
        ExtraUserProperties.objects.create(
            user=self.primary_pro_user, email_verified=True
        )

        # Create secondary professional user
        self.secondary_pro_user = User.objects.create_user(
            username=f"secondary_pro🐼{self.domain.id}",
            password="testpass",
            email="secondary@example.com",
        )
        self.secondary_professional = ProfessionalUser.objects.create(
            user=self.secondary_pro_user, active=True, npi_number="0987654321"
        )
        self.secondary_pro_user.is_active = True
        self.secondary_pro_user.save()

        # Create patient user
        self.patient_user = User.objects.create_user(
            username="patientuser",
            password="patientpass",
            email="patient@example.com",
            first_name="Test",
            last_name="Patient",
        )
        self.patient_user.is_active = True
        self.patient_user.save()
        self.patient = PatientUser.objects.create(
            user=self.patient_user,
        )

        # Create a denial
        self.denial = Denial.objects.create(
            denial_text="Test denial",
            primary_professional=self.primary_professional,
            creating_professional=self.primary_professional,
            patient_user=self.patient,
            hashed_email=Denial.get_hashed_email(self.patient_user.email),
        )

        # Create an appeal
        self.appeal = Appeal.objects.create(
            for_denial=self.denial,
            pending=True,
            patient_user=self.patient,
            primary_professional=self.primary_professional,
            creating_professional=self.primary_professional,
        )

        # Set up session
        self.client.login(
            username=self.primary_pro_username, password=self.primary_pro_password
        )
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

    def test_invite_existing_provider_by_id(self):
        url = reverse("appeals-invite-provider")

        response = self.client.post(
            url,
            json.dumps({"professional_id": self.secondary_professional.id, "appeal_id": self.appeal.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("message", response.json())
        self.assertEqual(response.json()["message"], "Provider invited successfully")

        # Verify the relation was created
        relation = SecondaryAppealProfessionalRelation.objects.filter(
            appeal=self.appeal, professional=self.secondary_professional
        ).exists()
        self.assertTrue(relation)

    def test_invite_existing_provider_by_email(self):
        url = reverse("appeals-invite-provider")

        response = self.client.post(
            url,
            json.dumps({"email": "secondary@example.com", "appeal_id": self.appeal.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("message", response.json())
        self.assertEqual(response.json()["message"], "Provider invited successfully")

        # Verify the relation was created
        relation = SecondaryAppealProfessionalRelation.objects.filter(
            appeal=self.appeal, professional=self.secondary_professional
        ).exists()
        self.assertTrue(relation)

    def test_invite_new_provider_by_email(self):
        url = reverse("appeals-invite-provider")
        new_provider_email = "new_provider@example.com"

        response = self.client.post(
            url,
            json.dumps({"email": new_provider_email, "appeal_id": self.appeal.id}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("message", response.json())
        self.assertEqual(response.json()["message"], "Provider invited successfully")

        # No relation should be created since the provider doesn't exist yet
        relation = SecondaryAppealProfessionalRelation.objects.filter(
            appeal=self.appeal
        ).exists()
        self.assertFalse(relation)

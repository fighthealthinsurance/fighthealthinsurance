"""Test the rest API functionality"""

from asgiref.sync import sync_to_async
from unittest.mock import patch

import asyncio

import pytest
from channels.testing import WebsocketCommunicator

import typing

import hashlib
import json

from django.urls import reverse
from django.contrib.auth import get_user_model
from django.utils import timezone
from dateutil.relativedelta import relativedelta


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
from fhi_users.models import (
    PatientDomainRelation,
    ProfessionalDomainRelation,
)

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.http import JsonResponse
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


from typing import Dict, Any
from django.http import JsonResponse


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

    @pytest.mark.asyncio
    # Testing end to end for professional user
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
        parsed: Dict[str, Any] = response.json()
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

        # Now we need to poke entity extraction, this part is async.
        # Mock fire_and_forget_in_new_threadpool to avoid background threads
        # that race with test teardown.
        async def _noop_fire_and_forget(task):
            task.close()  # Prevent "coroutine was never awaited" warning

        with patch(
            "fighthealthinsurance.common_view_logic.fire_and_forget_in_new_threadpool",
            side_effect=_noop_fire_and_forget,
        ):
            seb_communicator = WebsocketCommunicator(
                StreamingEntityBackend.as_asgi(), "/testws/"
            )
            connected, _ = await seb_communicator.connect()
            assert connected
            await seb_communicator.send_json_to(
                {
                    "email": email,
                    "semi_sekret": semi_sekret,
                    "denial_id": denial_id,
                }
            )
            # We should receive at least one frame.
            response = await seb_communicator.receive_from(timeout=30)
            # Now consume all of the rest of them until done.
            try:
                while True:
                    response = await seb_communicator.receive_from(timeout=30)
            except (asyncio.TimeoutError, AssertionError):
                # TimeoutError: no more data within timeout
                # AssertionError: websocket.close received (connection closed by server)
                pass
        # Set health history before next steps
        health_history_url = reverse("healthhistory-list")
        health_history_response = await sync_to_async(self.client.post)(
            health_history_url,
            json.dumps(
                {
                    "denial_id": denial_id,
                    "email": email,
                    "semi_sekret": semi_sekret,
                    "health_history": "Sample health history",
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(health_history_response.status_code))
        # Ok now lets get the additional info
        find_next_steps_url = reverse("nextsteps-list")
        find_next_steps_response: JsonResponse = await sync_to_async(self.client.post)(
            find_next_steps_url,
            json.dumps(
                {
                    "email": email,
                    "semi_sekret": semi_sekret,
                    "denial_id": denial_id,
                    "denial_type": [1, 2],
                    "diagnosis": "high risk homosexual behaviour",
                    "include_provided_health_history_in_appeal": True,
                }
            ),
            content_type="application/json",
        )
        find_next_steps_parsed: Dict[str, Any] = find_next_steps_response.json()
        # Make sure we got back a reasonable set of questions. Reduced to 4 since in_network is handled separately for professionals
        assert len(find_next_steps_parsed["combined_form"]) >= 4
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
        # Verify include_provided_health_history is set on the denial
        denial = await Denial.objects.aget(denial_id=denial_id)
        assert denial.include_provided_health_history_in_appeal is True
        # Now we need to poke at the appeal creator
        # Now we need to poke entity extraction, this part is async
        a_communicator = WebsocketCommunicator(
            StreamingAppealsBackend.as_asgi(), "/testws/"
        )
        connected, _ = await a_communicator.connect()
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
        response = await a_communicator.receive_from(timeout=300)
        print(f"Received response {response}")
        responses.append(response)
        # Now consume all of the rest of them until done.
        try:
            while True:
                response = await a_communicator.receive_from(timeout=150)
                print(f"Received response {response}")
                responses.append(response)
        except Exception as e:
            print(f"Error {e}")
            pass
        print(f"Received responses {responses}")
        # Find just the appeals ones, quick hack look for the "content" string to avoid full parse.
        responses = list(
            filter(lambda x: '"content"' in x, filter(lambda x: len(x) > 4, responses))
        )
        # It's a streaming response with one per new line
        appeal = json.loads(responses[0])
        assert appeal["content"].lstrip().startswith("Dear")
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

    @pytest.mark.asyncio
    async def test_appeal_generation_status_messages(self):
        """Test that appeal generation WebSocket sends status messages."""
        # Setup: Create a denial through the API
        login_result = await sync_to_async(self.client.login)(
            username=self.username, password=self.password
        )
        self.assertTrue(login_result)
        url = reverse("denials-list")
        email = "test@example.com"
        response = await sync_to_async(self.client.post)(
            url,
            json.dumps(
                {
                    "denial_text": "Your claim has been denied because the requested treatment is not medically necessary.",
                    "denial_type": "1",
                    "plan_id": "",
                    "claim_id": "",
                    "state": "CA",
                    "email": email,
                    "pii": True,
                    "tos": True,
                    "privacy": True,
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))
        data = response.json()
        denial_id = data["denial_id"]
        semi_sekret = data["semi_sekret"]

        # Run entity extraction first
        seb_communicator = WebsocketCommunicator(
            StreamingEntityBackend.as_asgi(), "/testws/"
        )
        connected, _ = await seb_communicator.connect()
        assert connected
        await seb_communicator.send_json_to(
            {
                "email": email,
                "semi_sekret": semi_sekret,
                "denial_id": denial_id,
            }
        )
        # Consume entity extraction responses
        try:
            while True:
                response = await seb_communicator.receive_from(timeout=30)
        except Exception:
            pass

        # Now test appeal generation with status messages
        a_communicator = WebsocketCommunicator(
            StreamingAppealsBackend.as_asgi(), "/testws/"
        )
        connected, _ = await a_communicator.connect()
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

        # Collect all responses
        responses = []
        status_messages = []
        appeal_contents = []

        try:
            while True:
                response = await a_communicator.receive_from(timeout=150)
                try:
                    # Skip empty keep alive lines
                    if len(response) < 2:
                        continue
                    responses.append(response)
                    parsed = json.loads(response)
                    if parsed.get("type") == "status":
                        status_messages.append(parsed.get("message"))
                    elif "content" in parsed:
                        appeal_contents.append(parsed["content"])
                    else:
                        print(f"Got a non-status message without content? {parsed}")
                except json.JSONDecodeError:
                    pass  # Skip non-JSON lines
        except Exception as e:
            print(f"Done receiving: {e}")
            pass

        # Verify we received status messages
        print(f"Status messages received: {status_messages}")
        print(f"Appeals received: {len(appeal_contents)}")

        # Assert we got at least some expected status messages
        assert len(status_messages) > 0, "Should have received status messages"

        # Check for expected status messages
        status_text = " ".join(status_messages).lower()
        assert any(
            keyword in status_text
            for keyword in ["starting", "loading", "generating", "gathering"]
        ), f"Expected status keywords not found in: {status_messages}"

        # Verify we still get appeal content
        assert (
            len(appeal_contents) >= 1
        ), f"Should have received at least one appeal in {responses}"
        assert (
            appeal_contents[0].lstrip().startswith("Dear")
        ), "Appeal should start with 'Dear'"


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
            last_name="Patient",
        )
        self.patient_user.is_active = True
        self.patient_user.save()
        self.patient = PatientUser.objects.create(user=self.patient_user)

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
            json.dumps(
                {
                    "professional_id": self.secondary_professional.id,
                    "appeal_id": self.appeal.id,
                }
            ),
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


class StatisticsTest(APITestCase):
    """Test the statistics API endpoints."""

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

        # Create ExtraUserProperties for professional
        ExtraUserProperties.objects.create(user=self.pro_user, email_verified=True)

        # Create professional domain relation
        ProfessionalDomainRelation.objects.create(
            professional=self.professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=True,
            pending_domain_relation=False,
        )

        # Create patient users
        self.patient_user1 = User.objects.create_user(
            username="patientuser1",
            password="patientpass",
            email="patient1@example.com",
            first_name="Test1",
            last_name="Patient",
        )
        self.patient_user1.is_active = True
        self.patient_user1.save()
        self.patient1 = PatientUser.objects.create(user=self.patient_user1, active=True)

        # Create ExtraUserProperties for patient1
        ExtraUserProperties.objects.create(user=self.patient_user1, email_verified=True)

        # Create patient domain relation
        PatientDomainRelation.objects.create(
            patient=self.patient1,
            domain=self.domain,
        )

        self.patient_user2 = User.objects.create_user(
            username="patientuser2",
            password="patientpass",
            email="patient2@example.com",
            first_name="Test2",
            last_name="Patient",
        )
        self.patient_user2.is_active = True
        self.patient_user2.save()
        self.patient2 = PatientUser.objects.create(user=self.patient_user2, active=True)

        # Create ExtraUserProperties for patient2
        ExtraUserProperties.objects.create(user=self.patient_user2, email_verified=True)

        # Create patient domain relation
        PatientDomainRelation.objects.create(
            patient=self.patient2,
            domain=self.domain,
        )

        # Set up session
        self.client.login(username=self.pro_username, password=self.pro_password)
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

        # Get current date and previous month
        self.now = timezone.now()
        self.previous_month = self.now - relativedelta(months=2)

        # Create denials and appeals for current month
        self.current_denial1 = Denial.objects.create(
            denial_text="Current test denial 1",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient1,
            domain=self.domain,
            hashed_email=Denial.get_hashed_email(self.patient_user1.email),
        )

        self.current_appeal1 = Appeal.objects.create(
            for_denial=self.current_denial1,
            pending=False,
            sent=True,
            patient_user=self.patient2,
            primary_professional=self.professional,
            creating_professional=self.professional,
            domain=self.domain,
            mod_date=self.now.date(),
            creation_date=self.now.date(),
            response_date=self.now,
        )

        self.current_denial2 = Denial.objects.create(
            denial_text="Current test denial 2",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient1,
            domain=self.domain,
            hashed_email=Denial.get_hashed_email(self.patient_user1.email),
        )

        self.current_appeal2 = Appeal.objects.create(
            for_denial=self.current_denial2,
            pending=True,
            sent=False,
            patient_user=self.patient1,
            primary_professional=self.professional,
            creating_professional=self.professional,
            domain=self.domain,
            mod_date=self.now.date(),
            creation_date=self.now.date(),
        )
        # Needs to be set after creation to avoid auto_now_add
        self.current_appeal2.creation_date = self.now.date() - relativedelta(days=10)
        self.current_appeal2.save()

        prev_month_date = (self.previous_month + relativedelta(days=5)).date()
        print(f"Creating old appeals around {prev_month_date}")

        # Create denials and appeals for previous month
        self.prev_denial1 = Denial.objects.create(
            denial_text="Previous test denial 1",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient2,
            domain=self.domain,
            hashed_email=Denial.get_hashed_email(self.patient_user2.email),
        )

        self.prev_appeal1 = Appeal.objects.create(
            for_denial=self.prev_denial1,
            pending=False,
            sent=True,
            patient_user=self.patient2,
            primary_professional=self.professional,
            creating_professional=self.professional,
            domain=self.domain,
            mod_date=prev_month_date,
            response_date=self.previous_month + relativedelta(days=10),
        )
        # Needs to be set after creation to avoid auto_now_add
        self.prev_appeal1.creation_date = prev_month_date
        self.prev_appeal1.save()

        self.prev_denial2 = Denial.objects.create(
            denial_text="Previous test denial 2",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient2,
            domain=self.domain,
            hashed_email=Denial.get_hashed_email(self.patient_user2.email),
        )

        self.prev_appeal2 = Appeal.objects.create(
            for_denial=self.prev_denial2,
            pending=True,
            sent=False,
            patient_user=self.patient2,
            primary_professional=self.professional,
            creating_professional=self.professional,
            domain=self.domain,
            mod_date=prev_month_date,
        )
        # Needs to be set after creation to avoid auto_now_add
        self.prev_appeal2.creation_date = prev_month_date
        self.prev_appeal2.save()

    def test_relative_statistics_endpoint(self):
        """Test the relative statistics endpoint with default Month over Month (MoM) comparison."""
        url = reverse("appeals-stats")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Verify all required fields exist
        required_fields = [
            "current_total_appeals",
            "current_success_rate",
            "current_estimated_payment_value",
            "current_total_patients",
            "previous_total_appeals",
            "previous_success_rate",
            "previous_estimated_payment_value",
            "previous_total_patients",
            "period_start",
            "period_end",
        ]

        for field in required_fields:
            self.assertIn(field, data)

        # Verify correct counts
        self.assertEqual(data["current_total_appeals"], 2)
        self.assertEqual(data["previous_total_appeals"], 2)

        # Verify patient counts - should now be total patients in domain
        self.assertEqual(data["current_total_patients"], 2)
        self.assertEqual(data["previous_total_patients"], 1)

    def test_absolute_statistics_endpoint(self):
        """Test the absolute statistics endpoint."""
        url = reverse("appeals-absolute-stats")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()

        # Verify all required fields exist
        required_fields = [
            "total_appeals",
            "success_rate",
            "estimated_payment_value",
            "total_patients",
        ]

        for field in required_fields:
            self.assertIn(field, data)

        # Verify counts - should be all appeals (current + previous = 4)
        self.assertEqual(data["total_appeals"], 4)

        # Verify success rate (no visible responses)
        self.assertEqual(data["success_rate"], 0.0)

        # Verify estimated payment is None until we implement it.
        self.assertEqual(data["estimated_payment_value"], None)

        # Verify patient count (should be all patients in domain = 2)
        self.assertEqual(data["total_patients"], 2)

        # Mark an appeal as replied to that is visible to the user
        self.current_appeal2.response_date = self.now
        self.current_appeal2.success = True
        self.current_appeal2.save()
        response = self.client.get(url)
        data = response.json()

        # Verify success rate (no visible responses)
        self.assertEqual(int(data["success_rate"]), 33)


class GetFullDetailsTest(APITestCase):
    """Test the get_full_details action of AppealViewSet."""

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
            first_name="Test",
            last_name="Provider",
        )
        self.pro_username = f"prouser🐼{self.domain.id}"
        self.pro_password = "testpass"
        self.professional = ProfessionalUser.objects.create(
            user=self.pro_user, active=True, npi_number="1234567890"
        )
        self.pro_user.is_active = True
        self.pro_user.save()
        ExtraUserProperties.objects.create(user=self.pro_user, email_verified=True)

        # Create professional domain relation
        ProfessionalDomainRelation.objects.create(
            professional=self.professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=True,
            pending_domain_relation=False,
        )

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
        self.patient = PatientUser.objects.create(user=self.patient_user, active=True)

        # Create a denial
        self.denial = Denial.objects.create(
            denial_text="Test denial text",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient,
            hashed_email=Denial.get_hashed_email(self.patient_user.email),
            insurance_company="Test Insurance Co",
            procedure="Test Procedure",
            diagnosis="Test Diagnosis",
            domain=self.domain,
        )

        # Create an appeal
        self.appeal = Appeal.objects.create(
            for_denial=self.denial,
            pending=True,
            patient_user=self.patient,
            primary_professional=self.professional,
            creating_professional=self.professional,
            domain=self.domain,
            appeal_text="This is a test appeal letter",
        )

        # Set up session
        self.client.login(username=self.pro_username, password=self.pro_password)
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

    def test_get_full_details(self):
        """Test retrieving full details of an appeal."""
        url = reverse("appeals-get-full-details")
        response = self.client.get(f"{url}?pk={self.appeal.id}")

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify appeal data
        data = response.json()
        self.assertEqual(data["id"], self.appeal.id)
        self.assertEqual(data["appeal_text"], "This is a test appeal letter")
        self.assertEqual(data["pending"], True)

        # Verify denial data is included
        self.assertIsNotNone(data["denial"])
        self.assertEqual(data["denial"]["denial_text"], "Test denial text")
        self.assertEqual(data["denial"]["insurance_company"], "Test Insurance Co")
        self.assertEqual(data["denial"]["procedure"], "Test Procedure")
        self.assertEqual(data["denial"]["diagnosis"], "Test Diagnosis")

        # Verify domain data is included
        self.assertIsNotNone(data["in_userdomain"])
        self.assertEqual(data["in_userdomain"]["name"], "testdomain")
        self.assertEqual(data["in_userdomain"]["display_name"], "Test Domain")

        # Verify professional data is included
        self.assertIsNotNone(data["primary_professional"])
        self.assertTrue("id" in data["primary_professional"])
        self.assertEqual(data["primary_professional"]["npi_number"], "1234567890")
        self.assertEqual(data["primary_professional"]["fullname"], "Test Provider")

        # Verify patient data is included
        self.assertIsNotNone(data["patient"])
        self.assertTrue("id" in data["patient"])

    def test_get_full_details_unauthorized_user(self):
        """Test retrieving full details with an unauthorized user."""
        # Create another professional and patient not associated with this appeal
        other_pro_user = User.objects.create_user(
            username=f"otherprouser🐼{self.domain.id}",
            password="testpass",
            email="otherpro@example.com",
        )
        other_pro_user.is_active = True
        other_pro_user.save()
        other_professional = ProfessionalUser.objects.create(
            user=other_pro_user, active=True
        )

        # Create a different domain
        other_domain = UserDomain.objects.create(
            name="otherdomain",
            visible_phone_number="9876543210",
            active=True,
        )

        # Associate the other professional with the other domain
        ProfessionalDomainRelation.objects.create(
            professional=other_professional,
            domain=other_domain,
            active_domain_relation=True,
            admin=False,
            pending_domain_relation=False,
        )

        # Login as the other professional
        self.client.logout()
        self.client.login(
            username=f"otherprouser🐼{self.domain.id}", password="testpass"
        )
        session = self.client.session
        session["domain_id"] = str(other_domain.id)
        session.save()

        # Attempt to access the appeal
        url = reverse("appeals-get-full-details")
        response = self.client.get(f"{url}?pk={self.appeal.id}")

        # Should return 404 because this user doesn't have access to this appeal
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


from typing import Any, Dict


class DenialCreateWithExistingId(APITestCase):
    """Test creating a denial with an existing denial id."""

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
            username=f"testuser{self.domain.id}",
            password="testpass",
            email="test@example.com",
        )
        self.username = f"testuser{self.domain.id}"
        self.password = "testpass"
        self.prouser = ProfessionalUser.objects.create(
            user=self.user, active=True, npi_number="1234567890"
        )
        self.user.is_active = True
        self.user.save()
        ExtraUserProperties.objects.create(user=self.user, email_verified=True)

    def test_create_with_existing_denial_id(self):
        login_result = self.client.login(username=self.username, password=self.password)
        self.assertTrue(login_result)
        # Create a denial
        url = reverse("denials-list")
        email = "timbit@fighthealthinsurance.com"
        hashed_email = hashlib.sha512(email.encode("utf-8")).hexdigest()
        denial_text = "Test denial text"
        response = self.client.post(
            url,
            json.dumps(
                {
                    "email": email,
                    "denial_text": denial_text,
                    "pii": "true",
                    "tos": "true",
                    "privacy": "true",
                    "store_raw_email": "true",
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))
        parsed: Dict[str, Any] = response.json()
        denial_id = parsed["denial_id"]
        # Create another denial with the same denial id
        response = self.client.post(
            url,
            json.dumps(
                {
                    "email": email,
                    "denial_text": denial_text,
                    "pii": "true",
                    "tos": "true",
                    "privacy": "true",
                    "store_raw_email": "true",
                    "denial_id": denial_id,
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))
        parsed = response.json()
        self.assertEqual(parsed["denial_id"], denial_id)


class DuplicateUserDomainTest(APITestCase):
    """Test that a duplicate UserDomain request returns a non-200 response."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        # Create initial test domain
        self.domain_name = "testdomain"
        self.domain = UserDomain.objects.create(
            name=self.domain_name,
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

    def test_duplicate_domain_creation(self):
        """Test that creating a domain with an existing name returns a non-200 response."""
        url = reverse("professional_user-list")

        # Data for creating a new professional with the same domain name
        data = {
            "user_signup_info": {
                "username": "newprouser",
                "password": "newLongerPasswordMagicCheetoCheeto123",
                "email": "newpro@example.com",
                "first_name": "New",
                "last_name": "User",
                "domain_name": self.domain_name,  # Same domain name as existing
                "visible_phone_number": "9876543210",  # Different phone number
                "continue_url": "http://example.com/continue",
            },
            "make_new_domain": True,
            "user_domain": {
                "name": self.domain_name,  # Same domain name as existing
                "visible_phone_number": "9876543210",
                "internal_phone_number": "0123456789",
                "display_name": "Duplicate Domain Test",
                "country": "USA",
                "state": "NY",
                "city": "New City",
                "address1": "456 Other St",
                "zipcode": "54321",
            },
        }

        response = self.client.post(
            url,
            json.dumps(data),
            content_type="application/json",
        )

        # Verify response is not a 200 OK
        self.assertNotEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotEqual(response.status_code, status.HTTP_201_CREATED)

        # Check error message in response
        response_data = response.json()
        self.assertIn("Domain", response_data["error"])

        # Verify no new domain was created with the same name
        domains_with_same_name = UserDomain.objects.filter(
            name=self.domain_name
        ).count()
        self.assertEqual(domains_with_same_name, 1)


class UpdateDenialTest(APITestCase):
    """Test that updating a denial via the create endpoint with denial_id mutates
    the existing denial rather than creating a new one (issue #302)."""

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
        self.client.login(username=self.username, password=self.password)
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

    def _create_denial(self, email, denial_text):
        """Helper: create a denial and return the response JSON."""
        url = reverse("denials-list")
        response = self.client.post(
            url,
            json.dumps(
                {
                    "email": email,
                    "denial_text": denial_text,
                    "pii": "true",
                    "tos": "true",
                    "privacy": "true",
                    "store_raw_email": "true",
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))
        return response.json()

    def test_update_denial_text_mutates_existing(self):
        """Updating denial_text via denial_id should change the existing denial, not create a new one."""
        email = "update-test@fighthealthinsurance.com"
        original_text = "Original denial text for testing"
        updated_text = "Updated denial text with new information"

        # Create a denial
        created = self._create_denial(email, original_text)
        denial_id = created["denial_id"]
        denial_count_before = Denial.objects.count()

        # Update the same denial
        url = reverse("denials-list")
        response = self.client.post(
            url,
            json.dumps(
                {
                    "email": email,
                    "denial_text": updated_text,
                    "pii": "true",
                    "tos": "true",
                    "privacy": "true",
                    "store_raw_email": "true",
                    "denial_id": denial_id,
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))
        parsed = response.json()

        # Same denial_id should be returned
        self.assertEqual(parsed["denial_id"], denial_id)

        # No new denial should have been created
        self.assertEqual(Denial.objects.count(), denial_count_before)

        # The denial text should actually be updated
        denial = Denial.objects.get(denial_id=int(denial_id))
        self.assertEqual(denial.denial_text, updated_text)

    def test_update_denial_preserves_creating_professional(self):
        """Updating a denial should preserve the creating professional."""
        email = "pro-preserve@fighthealthinsurance.com"
        created = self._create_denial(email, "Test denial")
        denial_id = created["denial_id"]

        # Update with new text
        url = reverse("denials-list")
        response = self.client.post(
            url,
            json.dumps(
                {
                    "email": email,
                    "denial_text": "Updated text",
                    "pii": "true",
                    "tos": "true",
                    "privacy": "true",
                    "denial_id": denial_id,
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))

        denial = Denial.objects.get(denial_id=int(denial_id))
        self.assertEqual(denial.creating_professional, self.prouser)

    def test_update_denial_does_not_duplicate_appeal(self):
        """Updating a denial that already has an appeal should not create a second appeal."""
        email = "no-dup-appeal@fighthealthinsurance.com"
        created = self._create_denial(email, "Test denial")
        denial_id = created["denial_id"]
        denial = Denial.objects.get(denial_id=int(denial_id))
        appeal_count = Appeal.objects.filter(for_denial=denial).count()
        self.assertEqual(appeal_count, 1)

        # Update the denial
        url = reverse("denials-list")
        response = self.client.post(
            url,
            json.dumps(
                {
                    "email": email,
                    "denial_text": "Updated denial",
                    "pii": "true",
                    "tos": "true",
                    "privacy": "true",
                    "denial_id": denial_id,
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))

        # Still only one appeal for this denial
        appeal_count_after = Appeal.objects.filter(for_denial=denial).count()
        self.assertEqual(appeal_count_after, 1)

    def test_retrieve_denial(self):
        """Should be able to retrieve a denial by its pk."""
        email = "retrieve-test@fighthealthinsurance.com"
        created = self._create_denial(email, "Test denial for retrieval")
        denial_id = created["denial_id"]
        denial = Denial.objects.get(denial_id=int(denial_id))

        url = reverse("denials-detail", args=[denial.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["denial_id"], denial_id)

    def test_select_articles_for_denial(self):
        """Selecting PubMed articles should update the denial's pubmed_ids_json."""
        email = "articles-test@fighthealthinsurance.com"
        created = self._create_denial(email, "Test denial for articles")
        denial_id = int(created["denial_id"])

        # Initially no articles
        denial = Denial.objects.get(denial_id=denial_id)
        self.assertFalse(denial.pubmed_ids_json)

        # Select articles
        url = reverse("denials-select-articles")
        pmids = ["12345678", "87654321"]
        response = self.client.post(
            url,
            json.dumps({"denial_id": denial_id, "pmids": pmids}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the denial was mutated
        denial.refresh_from_db()
        self.assertEqual(denial.pubmed_ids_json, pmids)

    def test_select_articles_replaces_previous(self):
        """Selecting new articles should replace, not append to, the previous selection."""
        email = "replace-articles@fighthealthinsurance.com"
        created = self._create_denial(email, "Test denial")
        denial_id = int(created["denial_id"])

        url = reverse("denials-select-articles")

        # First selection
        self.client.post(
            url,
            json.dumps({"denial_id": denial_id, "pmids": ["111", "222"]}),
            content_type="application/json",
        )
        denial = Denial.objects.get(denial_id=denial_id)
        self.assertEqual(denial.pubmed_ids_json, ["111", "222"])

        # Second selection replaces
        self.client.post(
            url,
            json.dumps({"denial_id": denial_id, "pmids": ["333"]}),
            content_type="application/json",
        )
        denial.refresh_from_db()
        self.assertEqual(denial.pubmed_ids_json, ["333"])


class UpdateAppealTest(APITestCase):
    """Test that updating an appeal via REST endpoints mutates the existing
    appeal rather than creating a new one (issue #302)."""

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
        self.patient = PatientUser.objects.create(user=self.patient_user)

        # Create a denial
        self.denial = Denial.objects.create(
            denial_text="Test denial for appeal update",
            primary_professional=self.professional,
            creating_professional=self.professional,
            patient_user=self.patient,
            hashed_email=Denial.get_hashed_email(self.patient_user.email),
        )

        # Create a pending appeal
        self.appeal = Appeal.objects.create(
            for_denial=self.denial,
            pending=True,
            appeal_text="Original appeal text",
            patient_user=self.patient,
            primary_professional=self.professional,
            creating_professional=self.professional,
        )

        # Login as professional
        self.client.login(username=self.pro_username, password=self.pro_password)
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

    def test_assemble_appeal_updates_existing(self):
        """assemble_appeal should update the existing pending appeal, not create a new one."""
        appeal_count_before = Appeal.objects.filter(for_denial=self.denial).count()
        self.assertEqual(appeal_count_before, 1)

        url = reverse("appeals-assemble-appeal")
        response = self.client.post(
            url,
            json.dumps(
                {
                    "denial_id": str(self.denial.denial_id),
                    "completed_appeal_text": "Updated appeal text with new arguments",
                    "insurance_company": "Test Insurance Co",
                    "fax_phone": "5551234567",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        parsed = response.json()

        # Should return the same appeal id
        self.assertEqual(parsed["appeal_id"], self.appeal.id)

        # Should NOT have created a new appeal
        appeal_count_after = Appeal.objects.filter(for_denial=self.denial).count()
        self.assertEqual(appeal_count_after, 1)

        # The existing appeal should have been mutated
        self.appeal.refresh_from_db()
        self.assertEqual(self.appeal.appeal_text, "Updated appeal text with new arguments")

    def test_assemble_appeal_twice_updates_same_appeal(self):
        """Calling assemble_appeal twice should update the same appeal, not create a second one."""
        url = reverse("appeals-assemble-appeal")

        # First assembly
        response1 = self.client.post(
            url,
            json.dumps(
                {
                    "denial_id": str(self.denial.denial_id),
                    "completed_appeal_text": "First version of appeal text",
                    "insurance_company": "Test Insurance",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response1.status_code, status.HTTP_201_CREATED)
        appeal_id_1 = response1.json()["appeal_id"]

        appeal_count_after_first = Appeal.objects.filter(for_denial=self.denial).count()
        self.assertEqual(appeal_count_after_first, 1)

        # Second assembly — should update, not create
        response2 = self.client.post(
            url,
            json.dumps(
                {
                    "denial_id": str(self.denial.denial_id),
                    "completed_appeal_text": "Second version of appeal text",
                    "insurance_company": "Test Insurance",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response2.status_code, status.HTTP_201_CREATED)
        appeal_id_2 = response2.json()["appeal_id"]

        # Same appeal ID both times
        self.assertEqual(appeal_id_1, appeal_id_2)

        # Still only one appeal
        appeal_count_after_second = Appeal.objects.filter(for_denial=self.denial).count()
        self.assertEqual(appeal_count_after_second, 1)

        # Text should reflect the second call
        self.appeal.refresh_from_db()
        self.assertEqual(self.appeal.appeal_text, "Second version of appeal text")

    def test_select_articles_for_appeal(self):
        """Selecting PubMed articles should update the appeal's pubmed_ids_json."""
        self.assertFalse(self.appeal.pubmed_ids_json)

        url = reverse("appeals-select-articles")
        pmids = ["11111111", "22222222"]
        response = self.client.post(
            url,
            json.dumps({"appeal_id": self.appeal.id, "pmids": pmids}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify the appeal was mutated
        self.appeal.refresh_from_db()
        self.assertEqual(self.appeal.pubmed_ids_json, pmids)

    def test_select_articles_replaces_previous_on_appeal(self):
        """Selecting new articles should replace the previous selection on the appeal."""
        url = reverse("appeals-select-articles")

        # First selection
        self.client.post(
            url,
            json.dumps({"appeal_id": self.appeal.id, "pmids": ["aaa", "bbb"]}),
            content_type="application/json",
        )
        self.appeal.refresh_from_db()
        self.assertEqual(self.appeal.pubmed_ids_json, ["aaa", "bbb"])

        # Second selection replaces
        self.client.post(
            url,
            json.dumps({"appeal_id": self.appeal.id, "pmids": ["ccc"]}),
            content_type="application/json",
        )
        self.appeal.refresh_from_db()
        self.assertEqual(self.appeal.pubmed_ids_json, ["ccc"])

    def test_retrieve_appeal(self):
        """Should be able to retrieve an appeal by its pk."""
        url = reverse("appeals-detail", args=[self.appeal.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["id"], self.appeal.pk)

    def test_unauthorized_user_cannot_update_appeal(self):
        """A different user should not be able to update someone else's appeal."""
        # Create a separate professional user
        other_user = User.objects.create_user(
            username=f"otheruser🐼{self.domain.id}",
            password="otherpass",
            email="other@example.com",
        )
        other_user.is_active = True
        other_user.save()
        ProfessionalUser.objects.create(
            user=other_user, active=True, npi_number="9999999999"
        )
        ExtraUserProperties.objects.create(user=other_user, email_verified=True)

        # Create a separate domain for the other user
        other_domain = UserDomain.objects.create(
            name="otherdomain",
            visible_phone_number="5555555555",
            internal_phone_number="5555555556",
            active=True,
            display_name="Other Domain",
            business_name="Other Business",
            country="USA",
            state="NY",
            city="Other City",
            address1="456 Other St",
            zipcode="54321",
        )

        # Login as the other user
        self.client.login(
            username=f"otheruser🐼{self.domain.id}", password="otherpass"
        )
        session = self.client.session
        session["domain_id"] = str(other_domain.id)
        session.save()

        # Try to select articles on an appeal they don't own
        url = reverse("appeals-select-articles")
        response = self.client.post(
            url,
            json.dumps({"appeal_id": self.appeal.id, "pmids": ["hacked"]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # Verify the appeal was NOT mutated
        self.appeal.refresh_from_db()
        self.assertNotEqual(self.appeal.pubmed_ids_json, ["hacked"])

    def test_unauthorized_user_cannot_update_denial(self):
        """A different user should not be able to update someone else's denial articles."""
        # Create a separate professional user
        other_user = User.objects.create_user(
            username=f"otheruserD🐼{self.domain.id}",
            password="otherpass",
            email="otherD@example.com",
        )
        other_user.is_active = True
        other_user.save()
        ProfessionalUser.objects.create(
            user=other_user, active=True, npi_number="8888888888"
        )
        ExtraUserProperties.objects.create(user=other_user, email_verified=True)

        other_domain = UserDomain.objects.create(
            name="otherdomainD",
            visible_phone_number="4444444444",
            internal_phone_number="4444444445",
            active=True,
            display_name="Other Domain D",
            business_name="Other Business D",
            country="USA",
            state="TX",
            city="Other City D",
            address1="789 Other St",
            zipcode="99999",
        )

        self.client.login(
            username=f"otheruserD🐼{self.domain.id}", password="otherpass"
        )
        session = self.client.session
        session["domain_id"] = str(other_domain.id)
        session.save()

        # Try to select articles on a denial they don't own
        url = reverse("denials-select-articles")
        response = self.client.post(
            url,
            json.dumps(
                {"denial_id": self.denial.denial_id, "pmids": ["hacked"]}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        # Verify the denial was NOT mutated
        self.denial.refresh_from_db()
        self.assertNotEqual(self.denial.pubmed_ids_json, ["hacked"])

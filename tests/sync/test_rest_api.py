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
        assert appeal.startswith("Dear")
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

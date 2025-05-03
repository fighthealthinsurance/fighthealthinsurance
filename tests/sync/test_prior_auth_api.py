"""Test the prior authorization API functionality"""

import json
import pytest
import uuid
from channels.testing import WebsocketCommunicator

from django.urls import reverse
from django.contrib.auth import get_user_model
from django.utils import timezone

from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance.models import (
    PriorAuthRequest,
    ProposedPriorAuth,
    ProfessionalUser,
    UserDomain,
    ExtraUserProperties,
)
from fighthealthinsurance.websockets import PriorAuthConsumer
from fhi_users.models import ProfessionalDomainRelation

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class PriorAuthAPITest(APITestCase):
    """Test the prior authorization API endpoints."""

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

        # Create domain relation
        ProfessionalDomainRelation.objects.create(
            professional=self.professional,
            domain=self.domain,
            active_domain_relation=True,
            admin=True,
            pending_domain_relation=False,
        )

        # Set up session
        self.client.login(username=self.pro_username, password=self.pro_password)
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

    def test_create_prior_auth_request(self):
        """Test creating a prior authorization request."""
        url = reverse("prior-auth-list")
        data = {
            "diagnosis": "Type 2 Diabetes",
            "treatment": "Continuous Glucose Monitor",
            "insurance_company": "Blue Cross Blue Shield",
            "patient_health_history": "Patient has had Type 2 Diabetes for 5 years",
        }

        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("id", response.data)
        self.assertIn("token", response.data)
        self.assertEqual(response.data["status"], "questions_asked")

        # Verify request was created in database
        prior_auth_id = response.data["id"]
        prior_auth = PriorAuthRequest.objects.get(id=prior_auth_id)
        self.assertEqual(prior_auth.diagnosis, data["diagnosis"])
        self.assertEqual(prior_auth.treatment, data["treatment"])
        self.assertEqual(prior_auth.professional_user, self.professional)

    def test_submit_answers(self):
        """Test submitting answers to a prior authorization request."""
        # Create a prior auth request first
        prior_auth = PriorAuthRequest.objects.create(
            professional_user=self.professional,
            domain=self.domain,
            diagnosis="Sleep Apnea",
            treatment="CPAP Machine",
            insurance_company="Aetna",
            status="questions_asked",
            questions=[
                ["How long has the patient had this condition?", ""],
                ["Has the patient tried any alternative treatments?", ""],
            ],
        )

        url = reverse("prior-auth-answers", args=[str(prior_auth.id)])
        data = {
            "token": str(prior_auth.token),
            "answers": {
                "0": "Patient has had sleep apnea for 2 years",
                "1": "Patient has tried positional therapy without success",
            },
        }

        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify answers were saved and status updated
        prior_auth.refresh_from_db()
        self.assertEqual(prior_auth.status, "questions_answered")
        self.assertEqual(prior_auth.answers, data["answers"])

    def test_select_proposal(self):
        """Test selecting a prior authorization proposal."""
        # Create a prior auth request
        prior_auth = PriorAuthRequest.objects.create(
            professional_user=self.professional,
            domain=self.domain,
            diagnosis="Migraine",
            treatment="CGRP Inhibitors",
            insurance_company="UnitedHealthcare",
            status="prior_auth_requested",
        )

        # Create some proposals
        proposal1 = ProposedPriorAuth.objects.create(
            prior_auth_request=prior_auth, text="Proposal 1 text here"
        )

        proposal2 = ProposedPriorAuth.objects.create(
            prior_auth_request=prior_auth, text="Proposal 2 text here"
        )

        url = reverse("prior-auth-select", args=[str(prior_auth.id)])
        data = {
            "token": str(prior_auth.token),
            "proposed_id": str(proposal2.proposed_id),
        }

        response = self.client.post(
            url, json.dumps(data), content_type="application/json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify proposal was selected and status updated
        prior_auth.refresh_from_db()
        self.assertEqual(prior_auth.status, "completed")

        proposal1.refresh_from_db()
        proposal2.refresh_from_db()
        self.assertFalse(proposal1.selected)
        self.assertTrue(proposal2.selected)

    def test_list_prior_auth_requests(self):
        """Test listing prior authorization requests."""
        # Create a few prior auth requests
        PriorAuthRequest.objects.create(
            professional_user=self.professional,
            domain=self.domain,
            diagnosis="Condition 1",
            treatment="Treatment 1",
            insurance_company="Insurance 1",
            status="completed",
        )

        PriorAuthRequest.objects.create(
            professional_user=self.professional,
            domain=self.domain,
            diagnosis="Condition 2",
            treatment="Treatment 2",
            insurance_company="Insurance 2",
            status="questions_asked",
        )

        url = reverse("prior-auth-list")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_filter_by_status(self):
        """Test filtering prior authorization requests by status."""
        # Create a few prior auth requests with different statuses
        PriorAuthRequest.objects.create(
            professional_user=self.professional,
            domain=self.domain,
            diagnosis="Condition 1",
            treatment="Treatment 1",
            insurance_company="Insurance 1",
            status="completed",
        )

        PriorAuthRequest.objects.create(
            professional_user=self.professional,
            domain=self.domain,
            diagnosis="Condition 2",
            treatment="Treatment 2",
            insurance_company="Insurance 2",
            status="questions_asked",
        )

        url = f"{reverse('prior-auth-list')}?status=completed"
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["status"], "completed")


class PriorAuthWebSocketTest(APITestCase):
    """Test the WebSocket endpoints for prior authorization."""

    async def test_prior_auth_websocket(self):
        """Test that the prior auth WebSocket connection works and generates proposals."""
        # Create a user and a prior auth request with answers
        user = await sync_to_async(User.objects.create_user)(
            username="testuser", password="testpass", email="test@example.com"
        )
        professional = await sync_to_async(ProfessionalUser.objects.create)(
            user=user, active=True, npi_number="1234567890"
        )

        prior_auth = await sync_to_async(PriorAuthRequest.objects.create)(
            professional_user=professional,
            diagnosis="Rheumatoid Arthritis",
            treatment="Biologic Therapy",
            insurance_company="Cigna",
            status="questions_answered",
            questions=[
                ["How long has the patient had this condition?", ""],
                ["Have they tried other DMARDs?", ""],
            ],
            answers={
                "0": "Patient diagnosed 3 years ago",
                "1": "Yes, tried methotrexate with inadequate response",
            },
        )

        # Connect to the WebSocket
        communicator = WebsocketCommunicator(
            PriorAuthConsumer.as_asgi(), "/ws/prior-auth/"
        )
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        # Send the token and ID
        await communicator.send_json_to(
            {"token": str(prior_auth.token), "id": str(prior_auth.id)}
        )

        # Expect a status message
        response = await communicator.receive_json_from()
        self.assertEqual(response.get("status"), "generating")

        # Wait for at least one proposal
        received_proposal = False
        for _ in range(5):  # Try up to 5 times
            try:
                response = await communicator.receive_json_from(timeout=10)
                if "proposed_id" in response and "text" in response:
                    received_proposal = True
                    break
            except:
                pass

        self.assertTrue(received_proposal)

        # Disconnect
        await communicator.disconnect()

        # Verify prior auth status was updated
        prior_auth = await sync_to_async(PriorAuthRequest.objects.get)(id=prior_auth.id)
        self.assertEqual(prior_auth.status, "prior_auth_requested")

        # Verify proposals were created
        proposal_count = await sync_to_async(ProposedPriorAuth.objects.filter)(
            prior_auth_request=prior_auth
        ).count()
        self.assertGreater(proposal_count, 0)

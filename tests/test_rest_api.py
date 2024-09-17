"""Test the rest API functionality"""

import hashlib
import os
import time
import sys
import json

from django.urls import reverse

from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance.models import *


class Delete(APITestCase):
    """Test just the delete API."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def test_url_root(self):
        url = reverse("api_delete")
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


class DenialEndToEnd(APITestCase):
    """Test end to end, we need to load the initial fixtures so we have denial types."""

    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def test_denial_end_to_end(self):
        url = reverse("api_denialcreator")
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
                    "denial_text": "test",
                    "pii": "true",
                    "tos": "true",
                    "privacy": "true",
                }
            ),
            content_type="application/json",
        )
        self.assertTrue(status.is_success(response.status_code))
        parsed = response.json()
        denial_id = parsed["denial_id"]
        semi_sekret = parsed["semi_sekret"]
        # Make sure we added a denial for this user
        denials_for_user_count = Denial.objects.filter(
            hashed_email=hashed_email
        ).count()
        assert denials_for_user_count > 0
        # Ok now lets get the additional info
        find_next_steps_url = reverse("api_findnextsteps")
        find_next_steps_response = self.client.post(
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
        appeals_gen_url = reverse("api_appeals_json_backend")
        appeals_gen_response = self.client.post(
            appeals_gen_url,
            json.dumps(
                {
                    "email": email,
                    "semi_sekret": semi_sekret,
                    "medical_reason": "preventive",
                    "age": "30",
                    "in_network": True,
                    "denial_id": denial_id,
                }
            ),
            content_type="application/json",
        )
        # It's a streaming response with one per new line
        text = b"".join(appeals_gen_response.streaming_content).split(b"\n")
        appeals = json.loads(text[0])
        assert appeals.startswith("Dear")

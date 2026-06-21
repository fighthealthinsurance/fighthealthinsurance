"""REST endpoint + DB-backed cache tests for the call script generator (#568).

The helper-level tests in tests/async-unit/test_call_script_generator.py mock
both the LLM and the ORM. These tests use a real database (so cache hits and
auth isolation are exercised end-to-end) and only mock the LLM, which is the
sole external dependency.
"""

import hashlib
import json
import typing
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from fighthealthinsurance.models import (
    CallScript,
    Denial,
    ExtraUserProperties,
    GenericCallScript,
    ProfessionalUser,
    UserDomain,
)

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


FAKE_SCRIPT = (
    "1. (Greet) Hello, this is [PATIENT NAME] calling about claim [CLAIM ID].\n"
    "2. (Ask) Which medical policy was applied to this denial?\n"
    "3. (Ask) Was AI or automation involved in the decision?\n"
    "4. (Close) Please send written confirmation of these answers."
)


def _make_domain_and_user(suffix: str = "a") -> tuple:
    """Create a self-contained domain + professional user for an isolated test."""
    # Phone numbers are unique columns, so derive them from the suffix to
    # allow multiple domains to coexist in a single auth-isolation test.
    h = abs(hash(suffix)) % 1_000_000
    domain = UserDomain.objects.create(
        name=f"testdom-{suffix}",
        visible_phone_number=f"100{h:07d}",
        internal_phone_number=f"200{h:07d}",
        active=True,
        display_name=f"Test Domain {suffix}",
        business_name=f"Test Business {suffix}",
        country="USA",
        state="CA",
        city="Test City",
        address1="123 Test St",
        zipcode="12345",
    )
    user = User.objects.create_user(
        username=f"testuser-{suffix}",
        password="testpass",
        email=f"test-{suffix}@example.com",
    )
    user.is_active = True
    user.save()
    pro = ProfessionalUser.objects.create(
        user=user, active=True, npi_number=f"123456789{suffix[0]}"
    )
    ExtraUserProperties.objects.create(user=user, email_verified=True)
    return domain, user, pro


def _make_denial(creating_pro: ProfessionalUser, **overrides) -> Denial:
    defaults = dict(
        hashed_email=hashlib.sha512(b"x@y.com").hexdigest(),
        denial_text="Sample denial letter body.",
        denial_type_text="medical necessity",
        insurance_company="Aetna",
        creating_professional=creating_pro,
        primary_professional=creating_pro,
    )
    defaults.update(overrides)
    return Denial.objects.create(**defaults)


def _post_json(client, url: str, payload: dict):
    return client.post(url, json.dumps(payload), content_type="application/json")


class CallScriptGenerateEndpointTests(APITestCase):
    """POST /ziggy/rest/call-scripts/generate/ behavior."""

    def setUp(self):
        self.domain, self.user, self.pro = _make_domain_and_user("gen")
        self.denial = _make_denial(self.pro)
        self.url = reverse("call-scripts-generate")
        self.client.login(username=self.user.username, password="testpass")

    @patch(
        "fighthealthinsurance.call_script_helper.infer_with_fallback",
        new_callable=AsyncMock,
    )
    def test_generate_returns_script_and_persists(self, mock_infer):
        mock_infer.return_value = FAKE_SCRIPT

        response = _post_json(
            self.client,
            self.url,
            {"denial_id": str(self.denial.denial_id), "goal": "info_gathering"},
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        body = response.json()
        # Response shape matches CallScriptResponseSerializer.
        for field in (
            "script_id",
            "denial_id",
            "goal",
            "insurer_name",
            "denial_reason",
            "script_text",
            "script_html",
            "created_at",
        ):
            self.assertIn(field, body)
        self.assertEqual(body["goal"], "info_gathering")
        self.assertEqual(body["denial_id"], self.denial.denial_id)
        self.assertIn("Aetna", body["insurer_name"])
        # Placeholder-substituted script text was returned and persisted.
        self.assertIn("Which medical policy", body["script_text"])
        self.assertIn("<html", body["script_html"].lower())

        self.assertEqual(CallScript.objects.filter(for_denial=self.denial).count(), 1)
        self.assertEqual(GenericCallScript.objects.count(), 1)
        call_script = CallScript.objects.get(for_denial=self.denial)
        self.assertNotIn(b"Which medical policy", call_script.encrypted_script_text)
        self.assertNotIn(
            b"medical necessity", call_script.encrypted_denial_reason.lower()
        )
        self.assertIn("Which medical policy", call_script.script_text)
        self.assertEqual(call_script.denial_reason, "medical necessity")

    @patch(
        "fighthealthinsurance.call_script_helper.infer_with_fallback",
        new_callable=AsyncMock,
    )
    def test_second_call_with_same_inputs_hits_generic_cache(self, mock_infer):
        mock_infer.return_value = FAKE_SCRIPT
        payload = {"denial_id": str(self.denial.denial_id), "goal": "info_gathering"}

        _post_json(self.client, self.url, payload)
        _post_json(self.client, self.url, payload)

        # The LLM was called exactly once even though we generated twice --
        # the second call must come from the GenericCallScript cache.
        self.assertEqual(mock_infer.await_count, 1)
        # And the per-denial artifact was created both times (one per call).
        self.assertEqual(CallScript.objects.filter(for_denial=self.denial).count(), 2)
        self.assertEqual(GenericCallScript.objects.count(), 1)

    @patch(
        "fighthealthinsurance.call_script_helper.infer_with_fallback",
        new_callable=AsyncMock,
    )
    def test_llm_failure_returns_503(self, mock_infer):
        mock_infer.return_value = None
        response = _post_json(
            self.client,
            self.url,
            {"denial_id": str(self.denial.denial_id), "goal": "info_gathering"},
        )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(CallScript.objects.count(), 0)

    def test_invalid_goal_is_rejected_by_serializer(self):
        response = _post_json(
            self.client,
            self.url,
            {"denial_id": str(self.denial.denial_id), "goal": "bogus"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch(
        "fighthealthinsurance.call_script_helper.infer_with_fallback",
        new_callable=AsyncMock,
    )
    def test_override_bypasses_generic_cache(self, mock_infer):
        # When the caller supplies a free-text override (which may contain
        # PHI lifted verbatim from a denial letter) we must not poison the
        # global GenericCallScript cache with it. The CallScript artifact is
        # still persisted per-denial.
        mock_infer.return_value = FAKE_SCRIPT
        response = _post_json(
            self.client,
            self.url,
            {
                "denial_id": str(self.denial.denial_id),
                "goal": "info_gathering",
                "denial_reason": "John Doe, claim 99 denied because of XYZ",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(GenericCallScript.objects.count(), 0)
        self.assertEqual(CallScript.objects.count(), 1)

    def test_oversized_override_rejected_at_validation(self):
        # Regression for the persistence-500 path: serializer must cap inputs
        # to match the underlying GenericCallScript / CallScript columns.
        response = _post_json(
            self.client,
            self.url,
            {
                "denial_id": str(self.denial.denial_id),
                "goal": "info_gathering",
                "insurer_name": "x" * 1000,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class CallScriptAuthIsolationTests(APITestCase):
    """A user must not be able to read or generate scripts on someone else's denial."""

    def setUp(self):
        self.domain_a, self.user_a, self.pro_a = _make_domain_and_user("aa")
        self.domain_b, self.user_b, self.pro_b = _make_domain_and_user("bb")
        # Denial is owned by user A.
        self.denial = _make_denial(self.pro_a)
        self.generate_url = reverse("call-scripts-generate")

    @patch(
        "fighthealthinsurance.call_script_helper.infer_with_fallback",
        new_callable=AsyncMock,
    )
    def test_other_user_cannot_generate_against_foreign_denial(self, mock_infer):
        mock_infer.return_value = FAKE_SCRIPT
        self.client.login(username=self.user_b.username, password="testpass")

        response = _post_json(
            self.client,
            self.generate_url,
            {"denial_id": str(self.denial.denial_id), "goal": "info_gathering"},
        )
        # filter_to_allowed_denials returns empty for user B -> get_object_or_404.
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        mock_infer.assert_not_called()
        self.assertEqual(CallScript.objects.count(), 0)

    @patch(
        "fighthealthinsurance.call_script_helper.infer_with_fallback",
        new_callable=AsyncMock,
    )
    def test_other_user_cannot_retrieve_foreign_script(self, mock_infer):
        mock_infer.return_value = FAKE_SCRIPT
        # User A generates a script.
        self.client.login(username=self.user_a.username, password="testpass")
        gen = _post_json(
            self.client,
            self.generate_url,
            {"denial_id": str(self.denial.denial_id), "goal": "info_gathering"},
        )
        self.assertEqual(gen.status_code, status.HTTP_201_CREATED)
        script_id = gen.json()["script_id"]

        # User B tries to read it.
        self.client.logout()
        self.client.login(username=self.user_b.username, password="testpass")
        retrieve_url = reverse("call-scripts-detail", kwargs={"pk": script_id})
        response = self.client.get(retrieve_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class CallScriptRetrieveTests(APITestCase):
    """GET /ziggy/rest/call-scripts/{id}/ re-renders printable HTML."""

    def setUp(self):
        self.domain, self.user, self.pro = _make_domain_and_user("ret")
        self.denial = _make_denial(self.pro)
        self.client.login(username=self.user.username, password="testpass")

    @patch(
        "fighthealthinsurance.call_script_helper.infer_with_fallback",
        new_callable=AsyncMock,
    )
    def test_retrieve_returns_same_script(self, mock_infer):
        mock_infer.return_value = FAKE_SCRIPT
        gen = _post_json(
            self.client,
            reverse("call-scripts-generate"),
            {"denial_id": str(self.denial.denial_id), "goal": "escalation"},
        )
        script_id = gen.json()["script_id"]

        response = self.client.get(
            reverse("call-scripts-detail", kwargs={"pk": script_id})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["script_id"], script_id)
        self.assertEqual(body["goal"], "escalation")
        self.assertIn("Which medical policy", body["script_text"])
        self.assertIn("<html", body["script_html"].lower())

    def test_malformed_uuid_returns_404(self):
        response = self.client.get(
            reverse("call-scripts-detail", kwargs={"pk": "not-a-uuid"})
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch(
        "fighthealthinsurance.call_script_helper.infer_with_fallback",
        new_callable=AsyncMock,
    )
    def test_printable_html_uses_date_of_service_text_fallback(self, mock_infer):
        mock_infer.return_value = FAKE_SCRIPT
        denial = _make_denial(
            self.pro, date_of_service="", date_of_service_text="January 15, 2026"
        )
        gen = _post_json(
            self.client,
            reverse("call-scripts-generate"),
            {"denial_id": str(denial.denial_id), "goal": "info_gathering"},
        )
        self.assertEqual(gen.status_code, status.HTTP_201_CREATED)
        self.assertIn("January 15, 2026", gen.json()["script_html"])

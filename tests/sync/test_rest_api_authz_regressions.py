import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from fhi_users.models import (
    ExtraUserProperties,
    PatientDomainRelation,
    PatientUser,
    ProfessionalDomainRelation,
    ProfessionalUser,
    UserDomain,
)
from fighthealthinsurance.models import (
    Appeal,
    Denial,
    OngoingChat,
    PriorAuthRequest,
    SecondaryAppealProfessionalRelation,
)


User = get_user_model()


async def _noop_generate_questions(_viewset, prior_auth):
    prior_auth.status = "questions_asked"
    await prior_auth.asave()
    return prior_auth


class RestAPIAuthorizationRegressionTest(APITestCase):
    fixtures = ["./fighthealthinsurance/fixtures/initial.yaml"]

    def setUp(self):
        self.domain = self._make_domain("domain-a", "1234567890")
        self.other_domain = self._make_domain("domain-b", "2234567890")
        self.professional = self._make_professional(
            "pro-a", "pro-a@example.com", self.domain
        )
        self.in_domain_professional = self._make_professional(
            "pro-a-peer", "pro-a-peer@example.com", self.domain
        )
        self.out_domain_professional = self._make_professional(
            "pro-b", "pro-b@example.com", self.other_domain
        )
        self.patient = self._make_patient(
            "patient-a", "patient-a@example.com", self.domain
        )
        self.out_domain_patient = self._make_patient(
            "patient-b", "patient-b@example.com", self.other_domain
        )

        self.client.login(username=f"pro-a🐼{self.domain.id}", password="testpass")
        session = self.client.session
        session["domain_id"] = str(self.domain.id)
        session.save()

        self.denial = Denial.objects.create(
            denial_text="Test denial",
            hashed_email=Denial.get_hashed_email(self.patient.user.email),
            creating_professional=self.professional,
            primary_professional=self.professional,
            patient_user=self.patient,
            domain=self.domain,
        )
        self.appeal = Appeal.objects.create(
            for_denial=self.denial,
            creating_professional=self.professional,
            primary_professional=self.professional,
            patient_user=self.patient,
            domain=self.domain,
            pending=True,
        )

    def _make_domain(self, name, phone):
        return UserDomain.objects.create(
            name=name,
            visible_phone_number=phone,
            internal_phone_number=f"9{phone}",
            active=True,
            display_name=name,
            business_name=name,
            country="USA",
            state="CA",
            city="Test City",
            address1="123 Test St",
            zipcode="12345",
        )

    def _make_professional(self, username, email, domain):
        user = User.objects.create_user(
            username=f"{username}🐼{domain.id}",
            password="testpass",
            email=email,
        )
        user.is_active = True
        user.save()
        ExtraUserProperties.objects.create(user=user, email_verified=True)
        professional = ProfessionalUser.objects.create(
            user=user, active=True, npi_number=str(1000000000 + User.objects.count())
        )
        ProfessionalDomainRelation.objects.create(
            professional=professional,
            domain=domain,
            admin=True,
            pending_domain_relation=False,
        )
        return professional

    def _make_patient(self, username, email, domain):
        user = User.objects.create_user(
            username=f"{username}🐼{domain.id}",
            password="testpass",
            email=email,
            first_name="Test",
            last_name="Patient",
        )
        user.is_active = True
        user.save()
        ExtraUserProperties.objects.create(user=user, email_verified=True)
        patient = PatientUser.objects.create(user=user, active=True)
        PatientDomainRelation.objects.create(patient=patient, domain=domain)
        return patient

    def _denial_payload(self, **overrides):
        payload = {
            "email": self.patient.user.email,
            "denial_text": "The claim was denied.",
            "zip": "12345",
            "pii": True,
            "tos": True,
            "privacy": True,
            "use_external_models": False,
        }
        payload.update(overrides)
        return payload

    def _error_body(self, message):
        return {"status": "error", "message": message, "error": message}

    def test_denial_create_rejects_out_of_domain_primary_professional(self):
        response = self.client.post(
            reverse("denials-list"),
            json.dumps(
                self._denial_payload(
                    primary_professional=str(self.out_domain_professional.id)
                )
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            response.json(),
            self._error_body("Professional not found in your domain"),
        )

    def test_denial_create_rejects_out_of_domain_patient(self):
        response = self.client.post(
            reverse("denials-list"),
            json.dumps(
                self._denial_payload(patient_id=str(self.out_domain_patient.id))
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            response.json(), self._error_body("Patient not found in your domain")
        )

    def test_denial_create_accepts_in_domain_primary_professional(self):
        response = self.client.post(
            reverse("denials-list"),
            json.dumps(
                self._denial_payload(
                    primary_professional=str(self.in_domain_professional.id)
                )
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        denial = Denial.objects.get(denial_id=response.json()["denial_id"])
        self.assertEqual(denial.primary_professional, self.in_domain_professional)

    def test_invite_provider_rejects_out_of_domain_professional(self):
        response = self.client.post(
            reverse("appeals-invite-provider"),
            json.dumps(
                {
                    "appeal_id": self.appeal.id,
                    "professional_id": self.out_domain_professional.id,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            response.json(),
            self._error_body("Professional not found in your domain"),
        )
        self.assertFalse(
            SecondaryAppealProfessionalRelation.objects.filter(
                appeal=self.appeal, professional=self.out_domain_professional
            ).exists()
        )

    def test_prior_auth_create_rejects_out_of_domain_created_for_professional(self):
        response = self.client.post(
            reverse("prior-auth-list"),
            json.dumps(
                {
                    "diagnosis": "Diabetes",
                    "treatment": "CGM",
                    "insurance_company": "Aetna",
                    "created_for_professional_user_id": self.out_domain_professional.id,
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            response.json(),
            self._error_body("Professional not found in your domain"),
        )

    def test_chat_list_and_delete_exclude_other_domain_chats(self):
        in_domain_chat = OngoingChat.objects.create(
            professional_user=self.professional,
            domain=self.domain,
            chat_history=[{"role": "user", "content": "In domain"}],
        )
        out_domain_chat = OngoingChat.objects.create(
            professional_user=self.professional,
            domain=self.other_domain,
            chat_history=[{"role": "user", "content": "Out of domain"}],
        )

        list_response = self.client.get(reverse("chats-list"))
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {item["id"] for item in list_response.json()}, {str(in_domain_chat.id)}
        )

        delete_response = self.client.delete(
            reverse("chats-delete", args=[str(out_domain_chat.id)])
        )
        self.assertEqual(delete_response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(delete_response.json(), {"error": "Chat not found"})
        self.assertTrue(OngoingChat.objects.filter(id=out_domain_chat.id).exists())

    def test_assemble_appeal_missing_denial_returns_404(self):
        response = self.client.post(
            reverse("appeals-assemble-appeal"),
            json.dumps(
                {
                    "denial_id": "999999",
                    "completed_appeal_text": "Appeal text",
                    "insurance_company": "Aetna",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_send_fax_persists_fax_number_on_denial(self):
        self.appeal.appeal_text = "Completed appeal"
        self.appeal.save()

        with (
            patch(
                "fighthealthinsurance.rest_views.SendFaxHelper.stage_appeal_as_fax",
                return_value=SimpleNamespace(uuid="fax-id", hashed_email="hash"),
            ),
            patch(
                "fighthealthinsurance.rest_views.SendFaxHelper.remote_send_fax",
                return_value=True,
            ),
        ):
            response = self.client.post(
                reverse("appeals-send-fax"),
                json.dumps({"appeal_id": self.appeal.id, "fax_number": "555-987-6543"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.denial.refresh_from_db()
        self.assertEqual(self.denial.appeal_fax_number, "5559876543")

    def test_prior_auth_sort_by_allowlist_falls_back_for_invalid_field(self):
        older = PriorAuthRequest.objects.create(
            creator_professional_user=self.professional,
            domain=self.domain,
            diagnosis="Older",
            treatment="Treatment",
            insurance_company="Aetna",
        )
        newer = PriorAuthRequest.objects.create(
            creator_professional_user=self.professional,
            domain=self.domain,
            diagnosis="Newer",
            treatment="Treatment",
            insurance_company="Aetna",
        )
        PriorAuthRequest.objects.filter(id=older.id).update(
            created_at=timezone.now() - timezone.timedelta(days=1)
        )
        PriorAuthRequest.objects.filter(id=newer.id).update(created_at=timezone.now())

        response = self.client.get(
            f"{reverse('prior-auth-list')}?sort_by=creator_professional_user__user__email"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["id"], str(newer.id))
        self.assertEqual(response.data[1]["id"], str(older.id))

    def test_prior_auth_create_accepts_in_domain_created_for_professional(self):
        with patch(
            "fighthealthinsurance.rest_views.PriorAuthViewSet._generate_questions",
            new=_noop_generate_questions,
        ):
            response = self.client.post(
                reverse("prior-auth-list"),
                json.dumps(
                    {
                        "diagnosis": "Diabetes",
                        "treatment": "CGM",
                        "insurance_company": "Aetna",
                        "created_for_professional_user_id": (
                            self.in_domain_professional.id
                        ),
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        prior_auth = PriorAuthRequest.objects.get(id=uuid.UUID(response.data["id"]))
        self.assertEqual(
            prior_auth.created_for_professional_user, self.in_domain_professional
        )

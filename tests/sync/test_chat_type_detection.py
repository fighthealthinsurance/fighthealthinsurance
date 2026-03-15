"""Tests for chat type detection model behavior.

Tests verify that:
1. ChatType enum and chat_type field work correctly on OngoingChat
2. Model properties derive correct values from chat_type
3. Backward compatibility between chat_type and is_patient
4. resolve_chat_type() correctly determines chat type from ChatLeads/user state
"""

import uuid

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from fhi_users.models import ProfessionalUser
from fighthealthinsurance.models import ChatLeads, ChatType, OngoingChat
from fighthealthinsurance.websockets import resolve_chat_type

User = get_user_model()


class ChatTypeModelTest(TransactionTestCase):
    """Test the OngoingChat model's chat_type field and derived methods."""

    def test_patient_chat_type(self):
        user = User.objects.create_user(
            username="patient_test", password="testpass", email="patient@example.com"
        )
        chat = OngoingChat.objects.create(
            user=user,
            chat_type=ChatType.PATIENT,
            is_patient=True,
            chat_history=[],
            summary_for_next_call=[],
        )

        self.assertFalse(chat.is_professional_user())
        self.assertIn("patient", chat.summarize_user().lower())

    def test_professional_chat_type(self):
        user = User.objects.create_user(
            username="pro_test", password="testpass", email="pro@example.com"
        )
        professional = ProfessionalUser.objects.create(
            user=user, active=True, npi_number="1111111111"
        )
        chat = OngoingChat.objects.create(
            professional_user=professional,
            chat_type=ChatType.PROFESSIONAL,
            is_patient=False,
            chat_history=[],
            summary_for_next_call=[],
        )

        self.assertTrue(chat.is_professional_user())
        self.assertIn("professional", chat.summarize_user().lower())

    def test_trial_professional_chat_type(self):
        chat = OngoingChat.objects.create(
            session_key="test_trial_session_1234",
            chat_type=ChatType.TRIAL_PROFESSIONAL,
            is_patient=False,
            chat_history=[],
            summary_for_next_call=[],
        )

        self.assertTrue(chat.is_professional_user())
        self.assertIn("trial", chat.summarize_user().lower())

    def test_backward_compat_with_is_patient(self):
        patient_chat = OngoingChat.objects.create(
            session_key="compat_patient_sess",
            chat_type=ChatType.PATIENT,
            is_patient=True,
            chat_history=[],
            summary_for_next_call=[],
        )
        self.assertTrue(patient_chat.is_patient)
        self.assertEqual(patient_chat.chat_type, ChatType.PATIENT)

        trial_chat = OngoingChat.objects.create(
            session_key="compat_trial_sess",
            chat_type=ChatType.TRIAL_PROFESSIONAL,
            is_patient=False,
            chat_history=[],
            summary_for_next_call=[],
        )
        self.assertFalse(trial_chat.is_patient)
        self.assertEqual(trial_chat.chat_type, ChatType.TRIAL_PROFESSIONAL)

    def test_str_representation_by_chat_type(self):
        # Patient
        user = User.objects.create_user(
            username="str_patient", password="testpass", email="str_patient@example.com"
        )
        patient_chat = OngoingChat.objects.create(
            user=user,
            chat_type=ChatType.PATIENT,
            is_patient=True,
            chat_history=[],
            summary_for_next_call=[],
        )
        self.assertIn("patient", str(patient_chat).lower())

        # Trial professional
        trial_chat = OngoingChat.objects.create(
            session_key="str_trial_sess_1234",
            chat_type=ChatType.TRIAL_PROFESSIONAL,
            is_patient=False,
            chat_history=[],
            summary_for_next_call=[],
        )
        self.assertIn("trial professional", str(trial_chat).lower())

        # Professional
        pro_user = User.objects.create_user(
            username="str_pro", password="testpass", email="str_pro@example.com"
        )
        professional = ProfessionalUser.objects.create(
            user=pro_user, active=True, npi_number="2222222222"
        )
        pro_chat = OngoingChat.objects.create(
            professional_user=professional,
            chat_type=ChatType.PROFESSIONAL,
            is_patient=False,
            chat_history=[],
            summary_for_next_call=[],
        )
        display_name = professional.get_display_name().lower()
        self.assertIn(display_name, str(pro_chat).lower())


class ChatLeadsTrialDetectionTest(TransactionTestCase):
    """Test that resolve_chat_type() distinguishes trial professionals from drug-specific leads."""

    def _resolve(self, session_key):
        """Call resolve_chat_type for an anonymous user with the given session_key."""

        def _no_professional(user):
            return None

        return async_to_sync(resolve_chat_type)(
            user=None,
            is_authenticated=False,
            session_key=session_key,
            get_professional_user_fn=_no_professional,
        )

    def test_lead_without_drug_is_trial_professional(self):
        session_id = str(uuid.uuid4())
        ChatLeads.objects.create(
            name="Trial Pro",
            email="trialpro@example.com",
            phone="555-1234",
            company="Test Company",
            consent_to_contact=True,
            agreed_to_terms=True,
            session_id=session_id,
        )

        chat_type, professional_user = self._resolve(session_id)
        self.assertEqual(chat_type, ChatType.TRIAL_PROFESSIONAL)
        self.assertIsNone(professional_user)

    def test_lead_with_drug_is_patient(self):
        """A ChatLeads entry with a non-empty drug field is a drug-specific lead (patient)."""
        session_id = str(uuid.uuid4())
        ChatLeads.objects.create(
            name="Drug Lead",
            email="druglead@example.com",
            phone="555-5678",
            company="Pharma Co",
            consent_to_contact=True,
            agreed_to_terms=True,
            session_id=session_id,
            drug="Ozempic",
        )

        chat_type, professional_user = self._resolve(session_id)
        self.assertEqual(chat_type, ChatType.PATIENT)
        self.assertIsNone(professional_user)

    def test_no_lead_is_patient(self):
        """An anonymous user with no ChatLeads entry is a patient."""
        chat_type, professional_user = self._resolve(str(uuid.uuid4()))
        self.assertEqual(chat_type, ChatType.PATIENT)
        self.assertIsNone(professional_user)

    def test_authenticated_professional_resolved_server_side(self):
        """Authenticated user with ProfessionalUser record resolves as PROFESSIONAL."""
        user = User.objects.create_user(
            username="resolve_pro", password="testpass", email="resolve_pro@example.com"
        )
        professional = ProfessionalUser.objects.create(
            user=user, active=True, npi_number="3333333333"
        )

        def _get_professional(u):
            return ProfessionalUser.objects.filter(user=u, active=True).first()

        chat_type, pro_user = async_to_sync(resolve_chat_type)(
            user=user,
            is_authenticated=True,
            session_key=None,
            get_professional_user_fn=_get_professional,
        )
        self.assertEqual(chat_type, ChatType.PROFESSIONAL)
        self.assertEqual(pro_user, professional)

    def test_authenticated_non_professional_is_patient(self):
        """Authenticated user without ProfessionalUser record resolves as PATIENT."""
        user = User.objects.create_user(
            username="resolve_patient", password="testpass", email="rp@example.com"
        )

        def _get_professional(u):
            return ProfessionalUser.objects.filter(user=u, active=True).first()

        chat_type, pro_user = async_to_sync(resolve_chat_type)(
            user=user,
            is_authenticated=True,
            session_key=None,
            get_professional_user_fn=_get_professional,
        )
        self.assertEqual(chat_type, ChatType.PATIENT)
        self.assertIsNone(pro_user)

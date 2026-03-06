"""Tests for chat type detection model behavior.

Tests verify that:
1. ChatType enum and chat_type field work correctly on OngoingChat
2. Model properties derive correct values from chat_type
3. Backward compatibility between chat_type and is_patient
4. ChatLeads drug field distinguishes trial professionals from drug-specific leads
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from fhi_users.models import ProfessionalUser
from fighthealthinsurance.models import ChatLeads, ChatType, OngoingChat

User = get_user_model()


class ChatTypeModelTest(TestCase):
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

        self.assertFalse(chat.is_professional_user())
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
        self.assertIn("ongoing chat", str(pro_chat).lower())


class ChatLeadsTrialDetectionTest(TestCase):
    """Test that ChatLeads drug field correctly distinguishes trial professionals."""

    def test_lead_without_drug_is_trial_professional(self):
        import uuid

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

        lead = ChatLeads.objects.get(session_id=session_id)
        self.assertFalse(lead.drug)

    def test_lead_with_drug_is_not_trial_professional(self):
        import uuid

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

        lead = ChatLeads.objects.get(session_id=session_id)
        self.assertEqual(lead.drug, "Ozempic")

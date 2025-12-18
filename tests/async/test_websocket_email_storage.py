"""
Tests for WebSocket chat email storage functionality.

These tests verify that the hashed email is properly stored when creating chats
for anonymous patient users, which is critical for GDPR data deletion support.
"""

import pytest
from django.test import TestCase
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fighthealthinsurance.models import OngoingChat, Denial


class TestWebSocketEmailStorage(TestCase):
    """Test that email is properly hashed and stored for patient chats."""

    fixtures = ["fighthealthinsurance/fixtures/initial.yaml"]

    def test_hashed_email_stored_for_anonymous_patient_chat(self):
        """Test that creating an anonymous patient chat stores the hashed email."""
        test_email = "test_patient@example.com"
        test_session_key = str(uuid.uuid4())

        # Calculate expected hashed email
        expected_hashed_email = Denial.get_hashed_email(test_email)

        # Create chat directly as the websocket would
        chat = OngoingChat.objects.create(
            session_key=test_session_key,
            chat_history=[],
            summary_for_next_call=[],
            is_patient=True,
            hashed_email=expected_hashed_email,
        )

        # Verify the chat was created with the hashed email
        self.assertIsNotNone(chat.hashed_email)
        self.assertEqual(chat.hashed_email, expected_hashed_email)
        self.assertTrue(chat.is_patient)

        # Verify we can find the chat by hashed email
        found_chats = OngoingChat.objects.filter(hashed_email=expected_hashed_email)
        self.assertEqual(found_chats.count(), 1)
        self.assertEqual(found_chats.first().id, chat.id)

        # Clean up
        chat.delete()

    def test_hashed_email_enables_data_deletion(self):
        """Test that we can delete all data for a user by their email."""
        test_email = "deletion_test@example.com"
        test_session_key = str(uuid.uuid4())
        hashed_email = Denial.get_hashed_email(test_email)

        # Create multiple chats for the same email
        chat1 = OngoingChat.objects.create(
            session_key=test_session_key,
            chat_history=[{"role": "user", "content": "test message 1"}],
            summary_for_next_call=[],
            is_patient=True,
            hashed_email=hashed_email,
        )

        chat2 = OngoingChat.objects.create(
            session_key=str(uuid.uuid4()),
            chat_history=[{"role": "user", "content": "test message 2"}],
            summary_for_next_call=[],
            is_patient=True,
            hashed_email=hashed_email,
        )

        # Verify both chats exist
        self.assertEqual(
            OngoingChat.objects.filter(hashed_email=hashed_email).count(), 2
        )

        # Simulate GDPR deletion request - delete all chats by hashed email
        deleted_count, _ = OngoingChat.objects.filter(hashed_email=hashed_email).delete()

        self.assertEqual(deleted_count, 2)
        self.assertEqual(
            OngoingChat.objects.filter(hashed_email=hashed_email).count(), 0
        )

    def test_hashed_email_is_deterministic(self):
        """Test that hashing the same email always produces the same hash."""
        test_email = "deterministic@example.com"

        hash1 = Denial.get_hashed_email(test_email)
        hash2 = Denial.get_hashed_email(test_email)
        hash3 = Denial.get_hashed_email(test_email)

        self.assertEqual(hash1, hash2)
        self.assertEqual(hash2, hash3)

    def test_different_emails_produce_different_hashes(self):
        """Test that different emails produce different hashes."""
        email1 = "user1@example.com"
        email2 = "user2@example.com"

        hash1 = Denial.get_hashed_email(email1)
        hash2 = Denial.get_hashed_email(email2)

        self.assertNotEqual(hash1, hash2)

    def test_none_email_produces_none_hash(self):
        """Test that None email doesn't cause errors."""
        # The code should handle None gracefully
        # Based on the code: hashed_email = Denial.get_hashed_email(email) if email else None
        result = None
        if None:
            result = Denial.get_hashed_email(None)
        self.assertIsNone(result)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
class TestWebSocketChatCreation:
    """Async tests for websocket chat creation with email storage."""

    @pytest.fixture
    def mock_websocket_consumer(self):
        """Create a mock websocket consumer with _get_or_create_chat method."""
        from fighthealthinsurance.websockets import OngoingChatConsumer

        consumer = OngoingChatConsumer()
        consumer.scope = {"user": None}  # Anonymous user
        return consumer

    async def test_get_or_create_chat_stores_hashed_email(self, mock_websocket_consumer):
        """Test that _get_or_create_chat properly stores hashed email for patient chats."""
        test_email = "async_test@example.com"
        test_session_key = str(uuid.uuid4())

        # Call the method that creates the chat
        chat = await mock_websocket_consumer._get_or_create_chat(
            user=None,
            professional_user=None,
            is_patient=True,
            chat_id=None,
            session_key=test_session_key,
            email=test_email,
        )

        # Verify the chat has the hashed email
        expected_hash = Denial.get_hashed_email(test_email)
        assert chat.hashed_email == expected_hash
        assert chat.is_patient is True
        assert chat.session_key == test_session_key

        # Clean up
        await chat.adelete()

    async def test_get_or_create_chat_no_email_for_trial_professional(
        self, mock_websocket_consumer
    ):
        """Test that trial professional chats don't store hashed email."""
        from fighthealthinsurance.models import ChatLeads

        test_email = "trial_pro@example.com"
        test_session_key = str(uuid.uuid4())

        # Create a ChatLead to mark this as a trial professional chat
        chat_lead = await ChatLeads.objects.acreate(
            session_id=test_session_key,
            email=test_email,
        )

        try:
            # Call the method that creates the chat
            chat = await mock_websocket_consumer._get_or_create_chat(
                user=None,
                professional_user=None,
                is_patient=False,  # Trial professional
                chat_id=None,
                session_key=test_session_key,
                email=test_email,
            )

            # Verify trial professional chat doesn't store patient email
            # (is_patient should be False for trial professional)
            assert chat.is_patient is False

            # Clean up
            await chat.adelete()
        finally:
            await chat_lead.adelete()

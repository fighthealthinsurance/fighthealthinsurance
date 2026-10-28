"""The chat's hint about its linked case leaves out what the person emptied.

A document or long paste in a chat linked to a case is summarized with the
case's procedure and diagnosis as a hint. A field the person emptied on the
review page is not put back from extraction's copy of it.
"""

from unittest.mock import AsyncMock

from asgiref.sync import async_to_sync
from django.test import TestCase

from fighthealthinsurance.chat_interface import ChatInterface
from fighthealthinsurance.models import Appeal, Denial, OngoingChat

EMAIL = "rebecca.crumpler@example.com"


class ChatHintTest(TestCase):
    def hint_for(self, **denial_fields):
        denial = Denial.objects.create(
            hashed_email=Denial.get_hashed_email(EMAIL),
            denial_text="Denied an insulin pump.",
            candidate_procedure="Insulin pump",
            candidate_diagnosis="Type 1 diabetes",
            **denial_fields,
        )
        chat = OngoingChat.objects.create(chat_history=[])
        Appeal.objects.create(
            for_denial=denial, chat=chat, hashed_email=denial.hashed_email
        )
        interface = ChatInterface(
            send_json_message_func=AsyncMock(), chat=chat, user=None
        )
        return async_to_sync(interface._denial_context_for_chat)(chat)

    def test_extractions_copy_stands_in_for_an_empty_field(self):
        self.assertEqual(
            self.hint_for(), "Procedure: Insulin pump; Diagnosis: Type 1 diabetes"
        )

    def test_a_diagnosis_emptied_on_review_is_left_out(self):
        self.assertEqual(
            self.hint_for(review_cleared=" diagnosis "), "Procedure: Insulin pump"
        )

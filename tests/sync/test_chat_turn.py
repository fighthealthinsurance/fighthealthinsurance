"""ChatTurn: one row per chat turn with the model race behind it.

A turn row describes a person's chat, so it must go when the chat goes
(including a delete-my-data request) and must never hold any of the chat's
text. These tests pin both, and the read-only admin page.
"""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.db import models
from django.test import RequestFactory, TestCase
from django.urls import reverse

from fighthealthinsurance.helpers.data_helpers import RemoveDataHelper
from fighthealthinsurance.models import ChatTurn, Denial, OngoingChat

User = get_user_model()

# Every string a row may carry: model labels and enum values. Anything new
# that can hold text has to be added here on purpose.
LABEL_AND_ENUM_FIELDS = {
    "outcome",
    "winner_model",
    "winner_pass",
    "first_pass_model",
    "runner_up_model",
    "alternate_model",
    "preferred",
    # The shadow outcome enum, and the scorer string: the model TypeSafe
    # says answered plus the chat rubric version.
    "shadow_outcome",
    "shadow_scorer",
}
# The shadow scores are numbers and nothing else.
SHADOW_SCORE_FIELDS = {
    "shadow_winner_answers",
    "shadow_winner_verdict",
    "shadow_winner_asks_again",
    "shadow_second_answers",
    "shadow_second_verdict",
    "shadow_second_asks_again",
}
# JSON fields: lists of model labels, and the per-call metadata dicts
# (chat/turn_record.py CallLog.finish).
JSON_FIELDS = {"backends", "fallback_backends", "calls"}


def _turn(chat, **fields):
    defaults = dict(outcome="ok", use_external=True, winner_model="model-a")
    defaults.update(fields)
    return ChatTurn.objects.create(chat=chat, **defaults)


class ChatTurnHoldsNoTextTest(TestCase):
    def test_the_only_strings_are_model_labels_and_enums(self):
        text_fields = {
            f.name
            for f in ChatTurn._meta.concrete_fields
            if isinstance(f, (models.CharField, models.TextField))
            and not isinstance(f, models.UUIDField)
        }
        self.assertEqual(text_fields, LABEL_AND_ENUM_FIELDS)

    def test_no_free_text_field_at_all(self):
        self.assertFalse(
            [
                f.name
                for f in ChatTurn._meta.concrete_fields
                if isinstance(f, models.TextField)
            ]
        )

    def test_json_fields_are_the_label_lists_and_the_call_metadata(self):
        json_fields = {
            f.name
            for f in ChatTurn._meta.concrete_fields
            if isinstance(f, models.JSONField)
        }
        self.assertEqual(json_fields, JSON_FIELDS)

    def test_the_enums_are_closed(self):
        self.assertEqual(set(ChatTurn.Outcome.values), {"ok", "failed", "timeout"})
        self.assertEqual(set(ChatTurn.Preferred.values), {"", "primary", "alternate"})
        self.assertEqual(
            set(ChatTurn.ShadowOutcome.values), {"", "scored", "failed", "timeout"}
        )

    def test_the_shadow_scores_are_float_columns(self):
        shadow = {
            f.name: f
            for f in ChatTurn._meta.concrete_fields
            if f.name.startswith("shadow_")
        }
        self.assertEqual(
            set(shadow), SHADOW_SCORE_FIELDS | {"shadow_outcome", "shadow_scorer"}
        )
        for name in SHADOW_SCORE_FIELDS:
            self.assertIsInstance(shadow[name], models.FloatField, name)
        # Long enough for any model name the TypeSafe client accepts.
        self.assertGreaterEqual(
            shadow["shadow_scorer"].max_length,
            len("typesafe/" + "m" * 48 + "/chat-rubric-9999"),
        )

    def test_the_chat_link_cannot_be_null_and_cascades(self):
        chat_field = ChatTurn._meta.get_field("chat")
        self.assertFalse(chat_field.null)
        self.assertIs(chat_field.remote_field.on_delete, models.CASCADE)


class ChatTurnGoesWithItsChatTest(TestCase):
    EMAIL = "turns-go@example.com"

    def setUp(self):
        self.chat = OngoingChat.objects.create(
            hashed_email=Denial.get_hashed_email(self.EMAIL)
        )
        self.other = OngoingChat.objects.create(
            hashed_email=Denial.get_hashed_email("keep@example.com")
        )
        for chat in (self.chat, self.other):
            _turn(chat)
            _turn(chat, alternate_offered=True, preferred="primary")

    def test_deleting_the_chat_deletes_its_turns(self):
        chat_id = self.chat.id
        self.chat.delete()
        self.assertFalse(ChatTurn.objects.filter(chat_id=chat_id).exists())

    def test_deleting_a_chat_queryset_deletes_its_turns(self):
        """RemoveDataHelper deletes through a queryset, a different code
        path in Django than instance .delete()."""
        OngoingChat.objects.filter(id=self.chat.id).delete()
        self.assertEqual(ChatTurn.objects.count(), 2)

    def test_remove_data_for_email_deletes_turns(self):
        RemoveDataHelper.remove_data_for_email(self.EMAIL)
        self.assertFalse(ChatTurn.objects.filter(chat_id=self.chat.id).exists())

    def test_unrelated_chats_keep_their_turns(self):
        RemoveDataHelper.remove_data_for_email(self.EMAIL)
        self.assertEqual(ChatTurn.objects.filter(chat_id=self.other.id).count(), 2)


class ChatTurnAdminTest(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username="turn-admin", password="adminpass123", email="a@example.com"
        )
        self.model_admin = admin.site._registry[ChatTurn]
        self.request = RequestFactory().get("/")
        self.request.user = self.admin_user

    def test_it_is_registered_read_only(self):
        self.assertFalse(self.model_admin.has_add_permission(self.request))
        self.assertFalse(self.model_admin.has_change_permission(self.request))
        self.assertTrue(self.model_admin.has_view_permission(self.request))

    def test_deleting_stays_allowed_so_chats_can_still_be_deleted(self):
        """The admin refuses to delete a chat when a registered related
        model can't be deleted."""
        self.assertTrue(self.model_admin.has_delete_permission(self.request))
        chat = OngoingChat.objects.create()
        _turn(chat)
        self.client.login(username="turn-admin", password="adminpass123")
        response = self.client.post(
            reverse("admin:fighthealthinsurance_ongoingchat_delete", args=[chat.pk]),
            {"post": "yes"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(OngoingChat.objects.filter(pk=chat.pk).exists())
        self.assertEqual(ChatTurn.objects.count(), 0)

    def test_changelist_and_detail_pages_load(self):
        turn = _turn(OngoingChat.objects.create(), calls=[{"model": "model-a"}])
        self.client.login(username="turn-admin", password="adminpass123")
        response = self.client.get(
            reverse("admin:fighthealthinsurance_chatturn_changelist")
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "model-a")
        response = self.client.get(
            reverse("admin:fighthealthinsurance_chatturn_change", args=[turn.pk])
        )
        self.assertEqual(response.status_code, 200)

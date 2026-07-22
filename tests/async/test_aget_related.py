"""
Tests for fighthealthinsurance.utils.aget_related — async-safe forward
FK/OneToOne access without a sync-to-async bridge.
"""

import typing

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from fighthealthinsurance.models import OngoingChat
from fighthealthinsurance.utils import aget_related

if typing.TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()


class AgetRelatedTest(APITestCase):
    """Exercise aget_related against OngoingChat's nullable FKs."""

    async def _make_user_and_chat(self) -> typing.Tuple["User", OngoingChat]:
        user = await User.objects.acreate(
            username="aget_related_user", email="aget_related@example.com"
        )
        chat = await OngoingChat.objects.acreate(user=user, chat_history=[])
        # Re-fetch without select_related so the relation cache starts cold.
        chat = await OngoingChat.objects.aget(id=chat.id)
        return user, chat

    async def test_uncached_foreign_key_fetched_without_bridge(self):
        user, chat = await self._make_user_and_chat()
        fetched = await aget_related(chat, "user")
        self.assertEqual(fetched.pk, user.pk)

    async def test_fetch_warms_cache_so_sync_attribute_access_is_safe(self):
        user, chat = await self._make_user_and_chat()
        fetched = await aget_related(chat, "user")
        # Direct attribute access would raise SynchronousOnlyOperation in
        # async code on a cold cache; passing proves the cache was warmed.
        self.assertIs(chat.user, fetched)

    async def test_null_foreign_key_returns_none(self):
        _, chat = await self._make_user_and_chat()
        self.assertIsNone(await aget_related(chat, "professional_user"))

    async def test_select_related_instance_served_from_cache(self):
        user, chat = await self._make_user_and_chat()
        chat = await OngoingChat.objects.select_related("user").aget(id=chat.id)
        cached = chat.user
        # A fresh DB fetch would return a different object; identity proves
        # the select_related cache was reused.
        self.assertIs(await aget_related(chat, "user"), cached)

    async def test_non_relation_field_raises_type_error(self):
        _, chat = await self._make_user_and_chat()
        with self.assertRaises(TypeError):
            await aget_related(chat, "chat_type")

    async def test_reverse_relation_raises_type_error(self):
        # Reverse relations have native async on their managers already
        # (e.g. chat.appeals.afirst()); aget_related should refuse them.
        _, chat = await self._make_user_and_chat()
        with self.assertRaises(TypeError):
            await aget_related(chat, "appeals")

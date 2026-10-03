"""EmailPollingActor clears expired Django sessions every day.

A session row holds the email the later pages post and the random secret
back links are encrypted under. It expires SESSION_COOKIE_AGE after its last
save, and the actor's daily loop deletes it from django_session after that. The purge runs on the
loop's first pass, then once every 24 hours, and a failure in it leaves the
expired email clearing that shares the loop untouched.

The actor is built from its real class with Ray, the one second start-up
wait and the second Django boot patched out. Each test runs the loop for a
set number of passes: the wait at the end of the last one stops it.
"""

import datetime
import types
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from asgiref.sync import async_to_sync
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.utils import timezone

from fighthealthinsurance import views
from fighthealthinsurance.email_polling_actor import EmailPollingActor

# Access the underlying class through the Ray decorator's __ray_metadata__.
_Klass = getattr(EmailPollingActor, "__ray_metadata__", None)
_Underlying = _Klass.modified_class if _Klass else EmailPollingActor


@pytest.fixture
def actor():
    with (
        patch("fighthealthinsurance.email_polling_actor.time.sleep"),
        patch("configurations.wsgi.get_wsgi_application"),
    ):
        built = _Underlying()
    built._logger = MagicMock()
    # Nothing to send, so a pass goes straight to the daily cleanup.
    built.scheduled_sender.afind_candidates = AsyncMock(return_value=[])
    built.followup_sender.afind_candidates = AsyncMock(return_value=[])
    return built


def run_passes(actor, passes=1):
    """Run the loop the given number of times.

    The poll interval at the end of a clean pass is 8 to 15 seconds, and a
    pass that raised backs off for a minute or more, so either wait ends a
    pass. The loop stops at the end of the last one.
    """
    ended = 0

    async def fake_sleep(seconds):
        nonlocal ended
        if seconds >= 8:
            ended += 1
            if ended >= passes:
                actor.running = False

    with patch(
        "fighthealthinsurance.email_polling_actor.asyncio",
        types.SimpleNamespace(sleep=fake_sleep),
    ):
        async_to_sync(actor.run)()


def a_saved_session(email):
    store = SessionStore()
    store[views._DENIAL_REF_EMAILS_SESSION_KEY] = {"1": email}
    store.save()
    return store.session_key


def expire(session_key):
    Session.objects.filter(session_key=session_key).update(
        expire_date=timezone.now() - datetime.timedelta(seconds=1)
    )


class TestThePurge:
    @pytest.mark.django_db
    def test_expired_sessions_are_deleted_and_live_ones_kept(self, actor):
        expired = a_saved_session("gone@example.com")
        live = a_saved_session("still-here@example.com")
        expire(expired)

        async_to_sync(actor._clear_expired_sessions)()

        assert set(Session.objects.values_list("session_key", flat=True)) == {live}

    @pytest.mark.django_db
    def test_only_the_number_cleared_is_logged(self, actor):
        expired = a_saved_session("private@example.com")
        expire(expired)

        async_to_sync(actor._clear_expired_sessions)()

        assert actor._logger.method_calls == [call.info("Cleared 1 expired sessions")]


class TestTheDailyLoop:
    def test_the_first_pass_clears_expired_sessions(self, actor):
        actor._clear_expired_sessions = AsyncMock()

        run_passes(actor)

        actor._clear_expired_sessions.assert_awaited_once()

    def test_a_second_pass_on_the_same_day_does_not_purge_again(self, actor):
        actor._clear_expired_sessions = AsyncMock()

        run_passes(actor, passes=2)

        actor._clear_expired_sessions.assert_awaited_once()

    def test_a_purge_less_than_a_day_old_is_not_repeated(self, actor):
        actor._clear_expired_sessions = AsyncMock()
        actor.last_session_clear_check = timezone.now() - datetime.timedelta(hours=23)

        run_passes(actor)

        actor._clear_expired_sessions.assert_not_awaited()

    def test_a_purge_more_than_a_day_old_runs_again(self, actor):
        actor._clear_expired_sessions = AsyncMock()
        actor.last_session_clear_check = timezone.now() - datetime.timedelta(hours=25)

        run_passes(actor)

        actor._clear_expired_sessions.assert_awaited_once()

    @pytest.mark.django_db
    def test_a_failing_session_purge_still_lets_the_email_clearing_run(self, actor):
        actor._clear_expired_emails = AsyncMock()
        actor.last_email_clear_check = timezone.now() - datetime.timedelta(hours=25)

        with patch.object(
            SessionStore,
            "aclear_expired",
            AsyncMock(side_effect=RuntimeError("session store unavailable")),
        ):
            run_passes(actor)

        actor._clear_expired_emails.assert_awaited_once()

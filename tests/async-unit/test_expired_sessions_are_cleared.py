"""EmailPollingActor clears expired Django sessions every day.

A session row holds the email the later pages post and the random secret
back links are encrypted under. It expires SESSION_COOKIE_AGE after its last
save, and the actor's daily loop deletes it from django_session after that.
The purge runs on the loop's first pass, then once every 24 hours, and a
failure in it leaves the expired email clearing that shares the loop
untouched. Both cleanups are checked at the top of each pass and once a
minute through the loop's long waits, so neither a follow-up step that keeps
failing nor the pacing between follow-up sends holds them up. The same
loop starts, every ten minutes, a pass of its own that sends again any fax
whose send never started.

The actor is built from its real class with Ray, the one second start-up
wait and the second Django boot patched out. Most tests run the loop for a
set number of passes: the wait at the end of the last one stops it. The
tests of the email work run it for days on a mocked clock instead.
"""

import asyncio
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
    # The fax reconcile's pass is its own task; these loop tests only see
    # it start.
    built._start_fax_reconcile = AsyncMock()
    built.plan_documents_reminder_sender.afind_candidates = AsyncMock(return_value=[])
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


# A cleanup falls due 24 hours after its last run. The loop checks at the top
# of each pass and once a minute through its long waits, so a run may land up
# to a few minutes after it falls due, never hours.
DAY = datetime.timedelta(hours=24)
LATE_BY_AT_MOST = datetime.timedelta(minutes=5)
CLEANUPS = {
    "the session purge": "_clear_expired_sessions",
    "the expired email clearing": "_clear_expired_emails",
}


def run_on_a_mocked_clock(actor, days, names=tuple(CLEANUPS.values())):
    """Run the loop for the given number of days on a mocked clock.

    Every wait moves the clock on by its length and nothing else does. The
    clock starts at the actor's start-up time, and the loop stops at the end
    of the pass that crosses the last day. Returns, for each of the named
    steps (the cleanups unless told otherwise), the clock time at each of
    its runs.
    """
    start = actor.last_email_clear_check
    clock = types.SimpleNamespace(at=start)
    end = start + days * DAY
    runs = {name: [] for name in names}

    def recorder(name):
        async def record():
            runs[name].append(clock.at)

        return record

    for name in names:
        setattr(actor, name, AsyncMock(side_effect=recorder(name)))

    async def fake_sleep(seconds):
        clock.at += datetime.timedelta(seconds=seconds)
        if clock.at >= end:
            actor.running = False

    with (
        patch(
            "fighthealthinsurance.email_polling_actor.asyncio",
            types.SimpleNamespace(sleep=fake_sleep),
        ),
        patch(
            "fighthealthinsurance.email_polling_actor.timezone",
            types.SimpleNamespace(now=lambda: clock.at),
        ),
    ):
        async_to_sync(actor.run)()
    return start, runs


def assert_runs_once_a_day(start, runs, days, first_pass):
    """Each run lands within LATE_BY_AT_MOST of falling due, and no sooner.

    The session purge is due on the first pass and the email clearing a day
    after start-up, so over three days the purge falls due three times and
    the email clearing twice, not counting the moment the run ends.
    """
    first_due = start if first_pass else start + DAY
    assert runs, "the cleanup never ran"
    assert first_due <= runs[0] <= first_due + LATE_BY_AT_MOST, runs[0] - start
    gaps = [later - earlier for earlier, later in zip(runs, runs[1:])]
    assert all(DAY < gap <= DAY + LATE_BY_AT_MOST for gap in gaps), gaps
    assert len(runs) >= (days if first_pass else days - 1)


class TestTheEmailWorkDoesNotHoldUpTheCleanups:
    @pytest.mark.parametrize("cleanup", CLEANUPS)
    def test_a_follow_up_step_that_always_raises_does_not_stop_it(self, actor, cleanup):
        actor.followup_sender.afind_candidates = AsyncMock(
            side_effect=RuntimeError("follow-up store unavailable")
        )

        start, runs = run_on_a_mocked_clock(actor, days=3)

        assert_runs_once_a_day(
            start,
            runs[CLEANUPS[cleanup]],
            days=3,
            first_pass=cleanup == "the session purge",
        )

    @pytest.mark.parametrize("cleanup", CLEANUPS)
    def test_the_pacing_between_follow_up_sends_does_not_delay_it(self, actor, cleanup):
        # Ten follow-ups a pass: the pacing wait after them is 600 seconds a
        # send plus 42, give or take a minute, so about 100 minutes a pass.
        actor.followup_sender.afind_candidates = AsyncMock(return_value=[object()] * 10)
        actor.followup_sender.asend_all = AsyncMock(return_value=10)

        start, runs = run_on_a_mocked_clock(actor, days=3)

        assert_runs_once_a_day(
            start,
            runs[CLEANUPS[cleanup]],
            days=3,
            first_pass=cleanup == "the session purge",
        )


TEN_MINUTES = datetime.timedelta(minutes=10)


async def _hang(*args, **kwargs):
    await asyncio.Event().wait()


class TestTheFaxReconcile:
    """Every ten minutes the loop starts a pass that sends again any fax
    whose send never started (SendFaxHelper.aredispatch_stranded_faxes),
    through its long waits too. The pass runs as its own task, bounded, so
    it never holds up the email work."""

    def test_the_first_pass_starts_one_and_the_next_waits(self, actor):
        run_passes(actor, passes=2)

        actor._start_fax_reconcile.assert_awaited_once()

    def test_the_pacing_between_follow_up_sends_does_not_delay_it(self, actor):
        actor.followup_sender.afind_candidates = AsyncMock(return_value=[object()] * 10)
        actor.followup_sender.asend_all = AsyncMock(return_value=10)

        start, runs = run_on_a_mocked_clock(
            actor, days=1, names=("_start_fax_reconcile",)
        )

        runs = runs["_start_fax_reconcile"]
        assert start <= runs[0] <= start + LATE_BY_AT_MOST
        gaps = [later - earlier for earlier, later in zip(runs, runs[1:])]
        assert all(TEN_MINUTES <= gap <= TEN_MINUTES + LATE_BY_AT_MOST for gap in gaps)
        assert len(runs) >= 100

    def test_a_pass_still_running_is_left_to_finish(self, actor):
        actor.last_fax_reconcile = timezone.now() - 2 * TEN_MINUTES
        actor._fax_reconcile_task = MagicMock(done=MagicMock(return_value=False))

        run_passes(actor)

        actor._start_fax_reconcile.assert_not_awaited()

    def test_starting_a_pass_does_not_wait_for_it(self, actor):
        del actor._start_fax_reconcile
        actor._redispatch_stranded_faxes = AsyncMock(side_effect=_hang)

        async def start_then_look():
            await asyncio.wait_for(actor._run_fax_reconcile(), 2)
            await asyncio.sleep(0.05)
            task = actor._fax_reconcile_task
            still_running = not task.done()
            task.cancel()
            return still_running

        assert async_to_sync(start_then_look)() is True
        actor._redispatch_stranded_faxes.assert_awaited_once()

    @patch(
        "fighthealthinsurance.email_polling_actor.FAX_RECONCILE_PASS_LIMIT",
        datetime.timedelta(milliseconds=50),
    )
    def test_a_pass_that_hangs_is_stopped_at_its_limit(self, actor):
        actor._redispatch_stranded_faxes = AsyncMock(side_effect=_hang)

        async def bounded():
            await asyncio.wait_for(actor._fax_reconcile_pass(), 5)

        async_to_sync(bounded)()

        actor._logger.opt.return_value.error.assert_called_once_with(
            "Error re-dispatching stranded faxes"
        )

    def test_it_hands_the_work_to_the_fax_helper(self, actor):
        with patch(
            "fighthealthinsurance.helpers.fax_helpers.SendFaxHelper"
            ".aredispatch_stranded_faxes",
            AsyncMock(return_value=2),
        ) as redispatch:
            async_to_sync(_Underlying._redispatch_stranded_faxes)(actor)

        redispatch.assert_awaited_once_with()
        actor._logger.info.assert_called_once_with("Started sends for 2 stranded faxes")

    def test_a_reconcile_that_will_not_start_leaves_the_follow_ups_running(self, actor):
        actor._clear_expired_sessions = AsyncMock()
        actor._start_fax_reconcile = AsyncMock(
            side_effect=RuntimeError("no event loop to run it on")
        )

        run_passes(actor)

        actor._start_fax_reconcile.assert_awaited_once()
        actor.followup_sender.afind_candidates.assert_awaited_once()


def test_each_pass_sends_the_plan_documents_reminders_that_are_due(actor):
    actor.plan_documents_reminder_sender.afind_candidates = AsyncMock(return_value=[7])
    actor.plan_documents_reminder_sender.asend_all = AsyncMock(return_value=1)
    with patch.object(_Underlying, "_run_daily_cleanups", AsyncMock()):
        run_passes(actor)
    actor.plan_documents_reminder_sender.asend_all.assert_awaited_once_with(
        count=10, candidates=[7]
    )


def test_a_reminder_failure_leaves_the_follow_ups_running(actor):
    actor.plan_documents_reminder_sender.afind_candidates = AsyncMock(
        side_effect=RuntimeError("database went away")
    )
    with patch.object(_Underlying, "_run_daily_cleanups", AsyncMock()):
        run_passes(actor, passes=2)
    assert actor.followup_sender.afind_candidates.await_count == 2

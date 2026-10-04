import asyncio
import datetime
import os
import random
import time
from typing import Optional

from django.utils import timezone

import ray
from channels.db import database_sync_to_async

from fighthealthinsurance.utils import get_env_variable

name = "EmailPollingActor"


@ray.remote(max_restarts=-1, max_task_retries=-1)
class EmailPollingActor:
    def __init__(self):
        time.sleep(1)

        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE",
            get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"),
        )

        from configurations.wsgi import get_wsgi_application

        _application = get_wsgi_application()
        from loguru import logger

        self._logger = logger
        self._logger.info("EmailPollingActor initialized")
        # Now we can import the follow up e-mails logic
        from fighthealthinsurance.followup_emails import (
            FollowUpEmailSender,
            ThankyouEmailSender,
        )
        from fighthealthinsurance.scheduled_emails import ScheduledEmailSender

        self.followup_sender = FollowUpEmailSender()
        self.thankyou_sender = ThankyouEmailSender()
        self.scheduled_sender = ScheduledEmailSender()
        # Damping for the scheduled-email block's private error handling (see
        # run()): consecutive failures and the time to skip the block until.
        self._scheduled_failures = 0
        self._scheduled_skip_until: Optional[datetime.datetime] = None
        self.last_email_clear_check = timezone.now()
        # None means expired sessions are cleared on the first pass of run(),
        # then every 24 hours. Each deploy recreates this actor, so each
        # deploy starts with a purge.
        self.last_session_clear_check: Optional[datetime.datetime] = None
        self._logger.info("EmailPollingActor senders initialized")

    async def health_check(self) -> bool:
        """Check if the actor is healthy and running."""
        return getattr(self, "running", False)

    async def run(self) -> None:
        self._logger.info("Starting EmailPollingActor run")
        self.running = True
        error_count = 0
        while self.running:
            await asyncio.sleep(1)  # Yield
            # The daily cleanups come first in each pass, before any email
            # work, and the long waits below check them too, so neither a
            # failing email step nor the pacing between sends holds them up.
            # _run_daily_cleanups catches its own errors.
            await self._run_daily_cleanups()
            try:
                # Send queued emails whose business-hours window is now open
                # FIRST: these are time-sensitive, and the follow-up batch below
                # ends in a long jittered pacing delay (can exceed an hour) that
                # would otherwise push due intros past their business-hours
                # window. asend_all already paces sends (1-3s each); no big
                # jittered delay here so a batch can't spill past the window.
                # Own error isolation: a scheduled-path failure (find/claim/send)
                # must not abort this iteration and trip the loop's global backoff,
                # which would starve the healthy follow-up pipeline below. But
                # isolation alone would retry a persistent failure every poll
                # (~10s) forever, so consecutive failures skip the block with
                # exponential backoff instead of sleeping the whole loop.
                if (
                    self._scheduled_skip_until is None
                    or timezone.now() >= self._scheduled_skip_until
                ):
                    try:
                        self._logger.debug("Getting scheduled email candidates")
                        scheduled_candidates = (
                            await self.scheduled_sender.afind_candidates()
                        )
                        scheduled_count = len(scheduled_candidates)
                        self._logger.debug(
                            f"Scheduled email candidates: {scheduled_count}"
                        )
                        if scheduled_count > 0:
                            scheduled_sent = await self.scheduled_sender.asend_all(
                                count=10, candidates=scheduled_candidates
                            )
                            self._logger.info(f"Sent {scheduled_sent} scheduled emails")
                        self._scheduled_failures = 0
                        self._scheduled_skip_until = None
                    except Exception as e:
                        self._scheduled_failures += 1
                        skip_s = min(60 * 2 ** (self._scheduled_failures - 1), 1800)
                        self._scheduled_skip_until = (
                            timezone.now() + datetime.timedelta(seconds=skip_s)
                        )
                        self._logger.opt(exception=True).error(
                            f"Scheduled-email processing failed "
                            f"(#{self._scheduled_failures}), skipping the block "
                            f"for {skip_s}s: {e}"
                        )

                self._logger.debug("Getting follow up candidates")
                # Send follow-up emails (pass candidates to avoid double DB query)
                followup_candidates = await self.followup_sender.afind_candidates()
                followup_count = len(followup_candidates)
                self._logger.debug(f"Follow up candidates: {followup_count}")
                if followup_count > 0:
                    sent_count = await self.followup_sender.asend_all(
                        count=10, candidates=followup_candidates
                    )
                    self._logger.info(f"Sent {sent_count} follow-up emails")
                    await self._jittered_send_delay(sent_count)

                if False:
                    # Send thank-you emails to professionals
                    thankyou_candidates = await self.thankyou_sender.afind_candidates()
                    thankyou_count = len(thankyou_candidates)
                    self._logger.debug(f"Thank you candidates: {thankyou_count}")
                    if thankyou_count > 0:
                        thankyou_sent = await self.thankyou_sender.asend_all(
                            count=10, candidates=thankyou_candidates
                        )
                        self._logger.info(f"Sent {thankyou_sent} thank-you emails")
                        await self._jittered_send_delay(thankyou_sent)

                # Jittered poll interval
                await asyncio.sleep(random.uniform(8, 15))
                error_count = 0
            except Exception as e:
                error_count += 1
                # Exponential backoff with jitter, capped at 30 minutes
                exponent = min(error_count - 1, 4)
                backoff = min(60 * (2**exponent), 1800)
                jitter = random.uniform(0, backoff * 0.1)
                total_wait = backoff + jitter
                self._logger.opt(exception=True).error(
                    f"Error #{error_count} while checking messages, "
                    f"backing off {total_wait:.0f}s"
                )
                await self._wait_running_daily_cleanups(total_wait)

        self._logger.warning("EmailPollingActor stopped running")
        return None

    async def _jittered_send_delay(self, sent_count: int) -> None:
        """Apply jittered delay proportional to emails sent."""
        base_delay = 600 * sent_count + 42
        jitter = random.uniform(-60, 60)
        await self._wait_running_daily_cleanups(max(10, base_delay + jitter))

    async def _wait_running_daily_cleanups(self, seconds: float) -> None:
        """Wait the given time, running the daily cleanups as they fall due.

        The wait is slept a minute at a time with a check for due cleanups
        after each minute, so a long wait, such as the pacing after a batch
        of sends or the backoff after an error, holds a cleanup up by a
        minute at most.
        """
        remaining = seconds
        while remaining > 0:
            step = min(remaining, 60.0)
            await asyncio.sleep(step)
            remaining -= step
            await self._run_daily_cleanups()

    async def _run_daily_cleanups(self) -> None:
        """Run each daily cleanup that is due.

        Expired sessions are cleared on the first pass and then every 24
        hours; expired emails every 24 hours from start-up. Each cleanup is
        stamped as it starts and has its own try/except, so a failure in one
        leaves the other and the email work running, and is tried again the
        next day.
        """
        now = timezone.now()
        try:
            if self.last_session_clear_check is None or (
                now - self.last_session_clear_check
            ) > datetime.timedelta(hours=24):
                self.last_session_clear_check = now
                await self._clear_expired_sessions()
        except Exception:
            self._logger.opt(exception=True).error("Error clearing expired sessions")
        try:
            if (now - self.last_email_clear_check) > datetime.timedelta(hours=24):
                self.last_email_clear_check = now
                await self._clear_expired_emails()
        except Exception:
            self._logger.opt(exception=True).error("Error clearing expired emails")

    async def _clear_expired_sessions(self) -> None:
        """Delete sessions whose expiry date has passed.

        A session expires SESSION_COOKIE_AGE after it was last saved. This
        runs the configured engine's clear_expired, the same purge as
        ``manage.py clearsessions``; for the database engine that deletes the
        expired rows from django_session. Only the count is logged.
        """
        try:
            from importlib import import_module

            from django.conf import settings

            store = import_module(settings.SESSION_ENGINE).SessionStore
            # The database engines keep sessions in a table, so the rows about
            # to go can be counted for the log line. Other engines keep no
            # table to count.
            expired_count: Optional[int] = None
            if hasattr(store, "get_model_class"):
                expired_count = await (
                    store.get_model_class()
                    .objects.filter(expire_date__lt=timezone.now())
                    .acount()
                )
            await store.aclear_expired()
            if expired_count is None:
                self._logger.info("Cleared expired sessions")
            else:
                self._logger.info(f"Cleared {expired_count} expired sessions")
        except Exception:
            self._logger.opt(exception=True).error("Error clearing expired sessions")

    async def _clear_expired_emails(self) -> None:
        """Clear emails from denials 30 days after follow-up was sent for users who didn't opt in."""
        try:
            from django.db.models import Q

            from fighthealthinsurance.models import Denial, FollowUpSched

            cutoff_datetime = timezone.now() - datetime.timedelta(days=30)
            # Safety check: don't clear emails for denials created in the last 90 days
            safety_cutoff_date = datetime.date.today() - datetime.timedelta(days=90)

            # Find follow-up schedules that were sent more than 30 days ago
            denial_ids_with_sent_followups = await database_sync_to_async(
                lambda: set(
                    FollowUpSched.objects.filter(
                        follow_up_sent=True,
                        follow_up_sent_date__lt=cutoff_datetime,
                    ).values_list("denial_id__denial_id", flat=True)
                )
            )()

            # Get denials that have recent or pending follow-ups (should NOT be cleared)
            denials_with_recent_or_pending = await database_sync_to_async(
                lambda: set(
                    FollowUpSched.objects.filter(
                        Q(follow_up_sent=False)
                        | Q(follow_up_sent_date__gte=cutoff_datetime)
                    ).values_list("denial_id__denial_id", flat=True)
                )
            )()

            # Filter to denials that should have emails cleared
            # Also exclude denials created in the last 90 days for safety
            candidates = (
                Denial.objects.filter(
                    denial_id__in=denial_ids_with_sent_followups,
                    date__lt=safety_cutoff_date,  # Safety check: only clear emails for older denials
                )
                .exclude(
                    denial_id__in=denials_with_recent_or_pending,
                )
                .exclude(
                    raw_email__isnull=True,
                )
                .exclude(
                    raw_email="",
                )
            )

            # Capture denial IDs before the update
            denial_ids_to_clear = await database_sync_to_async(
                lambda: list(candidates.values_list("denial_id", flat=True))
            )()

            if denial_ids_to_clear:
                # Clear the raw_email field
                cleared_count = await database_sync_to_async(
                    lambda: Denial.objects.filter(
                        denial_id__in=denial_ids_to_clear
                    ).update(raw_email=None)
                )()

                # Also clear emails from FollowUpSched entries
                await database_sync_to_async(
                    lambda: FollowUpSched.objects.filter(
                        denial_id__in=denial_ids_to_clear
                    ).update(email="")
                )()

                self._logger.info(
                    f"Cleared emails from {cleared_count} expired denials"
                )
            else:
                self._logger.debug("No expired emails to clear")

        except Exception:
            self._logger.opt(exception=True).error("Error clearing expired emails")

import asyncio
import datetime
import os
import random
import time

from django.utils import timezone

import ray
from asgiref.sync import sync_to_async

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

        self.followup_sender = FollowUpEmailSender()
        self.thankyou_sender = ThankyouEmailSender()
        self.last_email_clear_check = timezone.now()
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
            try:
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

                # Check if we should clear expired emails (once per day)
                now = timezone.now()
                if (now - self.last_email_clear_check) > datetime.timedelta(hours=24):
                    await self._clear_expired_emails()
                    self.last_email_clear_check = now

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
                await asyncio.sleep(total_wait)

        self._logger.warning("EmailPollingActor stopped running")
        return None

    async def _jittered_send_delay(self, sent_count: int) -> None:
        """Apply jittered delay proportional to emails sent."""
        base_delay = 600 * sent_count + 42
        jitter = random.uniform(-60, 60)
        await asyncio.sleep(max(10, base_delay + jitter))

    async def _clear_expired_emails(self) -> None:
        """Clear emails from denials 30 days after follow-up was sent for users who didn't opt in."""
        try:
            from django.db.models import Q

            from fighthealthinsurance.models import Denial, FollowUpSched

            cutoff_datetime = timezone.now() - datetime.timedelta(days=30)
            # Safety check: don't clear emails for denials created in the last 90 days
            safety_cutoff_date = datetime.date.today() - datetime.timedelta(days=90)

            # Find follow-up schedules that were sent more than 30 days ago
            denial_ids_with_sent_followups = await sync_to_async(
                lambda: set(
                    FollowUpSched.objects.filter(
                        follow_up_sent=True,
                        follow_up_sent_date__lt=cutoff_datetime,
                    ).values_list("denial_id__denial_id", flat=True)
                )
            )()

            # Get denials that have recent or pending follow-ups (should NOT be cleared)
            denials_with_recent_or_pending = await sync_to_async(
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
            denial_ids_to_clear = await sync_to_async(
                lambda: list(candidates.values_list("denial_id", flat=True))
            )()

            if denial_ids_to_clear:
                # Clear the raw_email field
                cleared_count = await sync_to_async(
                    lambda: Denial.objects.filter(
                        denial_id__in=denial_ids_to_clear
                    ).update(raw_email=None)
                )()

                # Also clear emails from FollowUpSched entries
                await sync_to_async(
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

import os

import ray
import time
import asyncio
import datetime

from asgiref.sync import sync_to_async
from django.utils import timezone

from fighthealthinsurance.utils import get_env_variable

name = "EmailPollingActor"


@ray.remote(max_restarts=-1, max_task_retries=-1)
class EmailPollingActor:
    def __init__(self):
        print(f"Starting actor")
        time.sleep(1)

        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE",
            get_env_variable("DJANGO_SETTINGS_MODULE", "fighthealthinsurance.settings"),
        )

        from configurations.wsgi import get_wsgi_application

        _application = get_wsgi_application()
        print(f"wsgi started")
        # Now we can import the follow up e-mails logic
        from fighthealthinsurance.followup_emails import (
            ThankyouEmailSender,
            FollowUpEmailSender,
        )

        self.followup_sender = FollowUpEmailSender()
        self.thankyou_sender = ThankyouEmailSender()
        self.last_email_clear_check = timezone.now()
        print(f"Senders started")

    async def run(self) -> None:
        print(f"Starting run")
        self.running = True
        error_count = 0
        while self.running:
            await asyncio.sleep(1)  # Yield
            try:
                print(f"Gettting follow up candidates.")
                # Send follow-up emails
                followup_candidates = await sync_to_async(
                    self.followup_sender.find_candidates
                )()
                print(f"Top follow up candidates: {followup_candidates[0:4]}")
                if followup_candidates.count() > 0:
                    sent_count = await sync_to_async(self.followup_sender.send_all)(
                        count=10
                    )
                    print(f"Sent {sent_count} follow-up emails")

                # Send thank you emails
                thankyou_candidates = await sync_to_async(
                    self.thankyou_sender.find_candidates
                )()
                print(f"Top thank you candidates: {thankyou_candidates[0:4]}")
                if thankyou_candidates.count() > 0:
                    sent_count = await sync_to_async(self.thankyou_sender.send_all)(
                        count=10
                    )
                    print(f"Sent {sent_count} thank you emails")

                # Check if we should clear expired emails (once per day)
                now = timezone.now()
                if (now - self.last_email_clear_check) > datetime.timedelta(hours=24):
                    await self._clear_expired_emails()
                    self.last_email_clear_check = now

                await asyncio.sleep(10)
                error_count = 0
            except Exception as e:
                error_count += 1
                print(f"Error {e} while checking messages.")
                await asyncio.sleep(60)

        print(f"Done running? what?")
        return None

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

                print(f"Cleared emails from {cleared_count} expired denials")
            else:
                print("No expired emails to clear")

        except Exception as e:
            print(f"Error clearing expired emails: {e}")

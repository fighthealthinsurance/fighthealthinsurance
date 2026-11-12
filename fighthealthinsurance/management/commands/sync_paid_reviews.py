"""Answer the RentAHuman submissions waiting on review for every posted paid
Arena bounty: the poll that backs up the webhook (paid_reviews.sync_bounty)."""

import requests
from django.core.management.base import BaseCommand, CommandError

from fighthealthinsurance import paid_reviews
from fighthealthinsurance.models import PaidReviewBounty


class Command(BaseCommand):
    help = "Check and answer pending RentAHuman submissions for paid Arena reviews"

    def handle(self, *args, **options):
        try:
            client = paid_reviews.RentAHumanClient()
        except paid_reviews.RentAHumanError as e:
            raise CommandError(str(e))
        # Closed bounties too: a worker may submit a code earned before the
        # join link closed.
        bounties = PaidReviewBounty.objects.exclude(status="draft").exclude(
            rah_bounty_id=""
        )
        for bounty in bounties:
            try:
                outcomes = paid_reviews.sync_bounty(bounty, client)
            except (paid_reviews.RentAHumanError, requests.RequestException) as e:
                self.stderr.write(f"Bounty {bounty.id}: {e}")
                continue
            self.stdout.write(f"Bounty {bounty.id}: {outcomes or 'nothing pending'}")

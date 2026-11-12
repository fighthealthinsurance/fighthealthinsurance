"""Answer the RentAHuman submissions waiting on review for every open paid
Arena bounty: the poll that backs up the webhook (paid_reviews.sync_bounty)."""

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
        bounties = PaidReviewBounty.objects.filter(status="open").exclude(
            rah_bounty_id=""
        )
        for bounty in bounties:
            try:
                outcomes = paid_reviews.sync_bounty(bounty, client)
            except paid_reviews.RentAHumanError as e:
                self.stderr.write(f"Bounty {bounty.id}: {e}")
                continue
            self.stdout.write(f"Bounty {bounty.id}: {outcomes or 'nothing pending'}")

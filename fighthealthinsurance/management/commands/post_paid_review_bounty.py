"""Post a batch of paid Appeal Arena reviewers as a RentAHuman bounty.

Without --post this is a dry run: nothing is saved, and RentAHuman is asked
for its preview (dryRun) only when an API key is set. With --post the bounty
is saved, posted, and opened. See docs/chooser.md, "Paid reviewers".
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from fighthealthinsurance import paid_reviews
from fighthealthinsurance.models import PaidReviewBounty


class Command(BaseCommand):
    help = "Post (or preview) a RentAHuman bounty for paid Arena reviews"

    def add_arguments(self, parser):
        parser.add_argument("--spots", type=int, default=10, help="Workers to hire")
        parser.add_argument(
            "--price", type=Decimal, default=Decimal("5"), help="Dollars per worker"
        )
        parser.add_argument(
            "--reviews", type=int, default=10, help="Counted reviews per worker"
        )
        parser.add_argument(
            "--title",
            default="Judge AI-written health insurance appeal letters (remote)",
        )
        parser.add_argument(
            "--post", action="store_true", help="Really post it (spends money)"
        )
        parser.add_argument(
            "--close", type=int, metavar="BOUNTY_ID", help="Close a bounty's join link"
        )

    def handle(self, *args, **options):
        if options["close"]:
            closed = PaidReviewBounty.objects.filter(id=options["close"]).update(
                status="closed"
            )
            if not closed:
                raise CommandError(f"No bounty {options['close']}")
            self.stdout.write(
                f"Closed bounty {options['close']}'s join link. Close it on "
                "RentAHuman too if it still has open spots."
            )
            return

        spots, price, reviews = options["spots"], options["price"], options["reviews"]
        if spots < 1 or reviews < 1:
            raise CommandError("--spots and --reviews must be at least 1")
        if price < 5:
            raise CommandError("RentAHuman's minimum is $5 per worker")
        bounty = PaidReviewBounty(
            join_token=paid_reviews.new_join_token(),
            title=options["title"][:200],
            price_dollars=price,
            spots=spots,
            reviews_required=reviews,
        )
        cost = paid_reviews.total_cost(price, spots)
        self.stdout.write(
            f"{spots} workers x ${price} for {reviews} reviews each: "
            f"{spots * reviews} reviews, ${cost} with RentAHuman's 18% fee "
            f"(${(cost / (spots * reviews)).quantize(Decimal('0.01'))} a review)."
        )

        try:
            client = paid_reviews.RentAHumanClient()
        except paid_reviews.RentAHumanError as e:
            if options["post"]:
                raise CommandError(str(e))
            self.stdout.write(f"Dry run, offline ({e}). Bounty text:")
            self.stdout.write(
                paid_reviews.bounty_payload(bounty, dry_run=True)["description"]
            )
            return

        if not options["post"]:
            preview = client.create_bounty(
                paid_reviews.bounty_payload(bounty, dry_run=True)
            )
            self.stdout.write(f"RentAHuman preview: {preview}")
            self.stdout.write("Run again with --post to post it.")
            return

        bounty.status = "draft"
        bounty.save()
        try:
            posted = client.create_bounty(
                paid_reviews.bounty_payload(bounty, dry_run=False)
            )
        except paid_reviews.RentAHumanError as e:
            raise CommandError(f"Posting failed; bounty {bounty.id} left as draft: {e}")
        bounty.rah_bounty_id = paid_reviews.find_id(posted, "bountyId", "id", "_id")
        bounty.status = "open"
        bounty.save()
        self.stdout.write(
            f"Posted bounty {bounty.id} (RentAHuman {bounty.rah_bounty_id or '?'}). "
            f"Join link: {paid_reviews.join_url(bounty)}"
        )
        if not bounty.rah_bounty_id:
            self.stdout.write(
                f"Could not read RentAHuman's bounty id from {posted}; set "
                "rah_bounty_id in the admin so submissions can be matched."
            )
        if str(posted).find("pending_deposit") >= 0:
            self.stdout.write(
                "The bounty is waiting on a deposit: top up the RentAHuman wallet."
            )

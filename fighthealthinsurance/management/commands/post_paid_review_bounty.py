"""Post a batch of paid Appeal Arena reviewers as a RentAHuman bounty.

Without --post this is a dry run: nothing is saved, and RentAHuman is asked
for its preview (dryRun) only when an API key is set. With --post the bounty
is saved, posted, and opened. See docs/chooser.md, "Paid reviewers".
"""

from decimal import Decimal

import requests

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
        parser.add_argument(
            "--retry",
            type=int,
            metavar="BOUNTY_ID",
            help="Post a bounty left as a draft again, with the same idempotency "
            "key, so a post that reached RentAHuman is not made twice",
        )

    def handle(self, *args, **options):
        if options["retry"]:
            bounty = PaidReviewBounty.objects.filter(
                id=options["retry"], status="draft"
            ).first()
            if bounty is None:
                raise CommandError(f"No draft bounty {options['retry']}")
            try:
                client = paid_reviews.RentAHumanClient()
            except paid_reviews.RentAHumanError as e:
                raise CommandError(str(e))
            self._post(client, bounty)
            return
        if options["close"]:
            closed = PaidReviewBounty.objects.filter(id=options["close"]).update(
                status="closed"
            )
            if not closed:
                raise CommandError(f"No bounty {options['close']}")
            self.stdout.write(
                f"Closed bounty {options['close']}'s join link to new workers; "
                "runs already started can still finish. Close it on RentAHuman "
                "too if it still has open spots."
            )
            return

        spots, price, reviews = options["spots"], options["price"], options["reviews"]
        if spots < 1 or reviews < 2:
            raise CommandError("--spots must be at least 1 and --reviews at least 2")
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
        self._post(client, bounty)

    def _post(self, client, bounty):
        """Post a saved draft. Its idempotency key comes from its join token,
        so --retry on a post whose answer was lost gets the same bounty back
        rather than a second one. The join link opens only once RentAHuman's
        id for it is known, since submissions are matched by that id."""
        try:
            posted = client.create_bounty(
                paid_reviews.bounty_payload(bounty, dry_run=False)
            )
        except (paid_reviews.RentAHumanError, requests.RequestException) as e:
            raise CommandError(
                f"Posting failed; bounty {bounty.id} left as a draft. Run "
                f"--retry {bounty.id} to try again with the same key: {e}"
            )
        bounty.rah_bounty_id = paid_reviews.find_id(posted, "bountyId", "id", "_id")
        if not bounty.rah_bounty_id:
            bounty.save()
            raise CommandError(
                f"Could not read RentAHuman's bounty id from {posted}; bounty "
                f"{bounty.id} stays a draft with its join link closed. Set "
                "rah_bounty_id and status=open in the admin once the id is known."
            )
        bounty.status = "open"
        bounty.save()
        self.stdout.write(
            f"Posted bounty {bounty.id} (RentAHuman {bounty.rah_bounty_id}). "
            f"Join link: {paid_reviews.join_url(bounty)}"
        )
        if "pending_deposit" in str(posted):
            self.stdout.write(
                "The bounty is waiting on a deposit: top up the RentAHuman wallet "
                "(rentahuman_setup --deposit)."
            )
        if "pending_review" in str(posted):
            self.stdout.write(
                "RentAHuman's AI content review is checking the bounty; it goes "
                "live when that passes."
            )

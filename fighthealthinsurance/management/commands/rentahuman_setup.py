"""Set up the RentAHuman account for paid Arena reviews (paid_reviews.py).

--status shows the wallet balance and registered webhooks. --webhook
registers this site's webhook (for submission.submitted) and prints the
signing secret to put in RENTAHUMAN_WEBHOOK_SECRET; the secret is shown
only once. --test-webhook sends RentAHuman's test event to a registered
endpoint. --deposit N gives a Stripe Checkout link to add N dollars to the
wallet: nothing is charged until someone pays on that page.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError

from fighthealthinsurance import paid_reviews


class Command(BaseCommand):
    help = "Check or set up the RentAHuman account used for paid Arena reviews"

    def add_arguments(self, parser):
        parser.add_argument("--status", action="store_true")
        parser.add_argument("--webhook", action="store_true")
        parser.add_argument("--test-webhook", metavar="ENDPOINT_ID")
        parser.add_argument("--deposit", type=Decimal, metavar="DOLLARS")

    def handle(self, *args, **options):
        try:
            client = paid_reviews.RentAHumanClient()
        except paid_reviews.RentAHumanError as e:
            raise CommandError(str(e))
        try:
            if options["status"] or not any(
                options[k] for k in ("webhook", "test_webhook", "deposit")
            ):
                self._status(client)
            if options["webhook"]:
                self._webhook(client)
            if options["test_webhook"]:
                answer = client.test_webhook_endpoint(options["test_webhook"])
                self.stdout.write(f"Test event queued: {answer}")
            if options["deposit"]:
                if not Decimal("1") <= options["deposit"] <= Decimal("10000"):
                    raise CommandError("Deposits are $1 to $10,000")
                answer = client.deposit_checkout(options["deposit"])
                link = answer.get("checkoutUrl") or answer
                self.stdout.write(f"Pay here to add ${options['deposit']}: {link}")
        except paid_reviews.RentAHumanError as e:
            raise CommandError(str(e))

    def _status(self, client):
        self.stdout.write(f"Wallet: {client.wallet_balance()}")
        endpoints = client.webhook_endpoints()
        self.stdout.write(f"Webhook endpoints: {endpoints or 'none'}")
        self.stdout.write(f"This site's webhook URL: {paid_reviews.webhook_url()}")

    def _webhook(self, client):
        url = paid_reviews.webhook_url()
        existing = [e for e in client.webhook_endpoints() if e.get("url") == url]
        if existing:
            self.stdout.write(
                f"Already registered: {existing[0]}. Delete it on RentAHuman "
                "first to get a new secret."
            )
            return
        answer = client.create_webhook_endpoint(url, ["submission.submitted"])
        secret = paid_reviews.find_id(answer, "secret")
        endpoint_id = paid_reviews.find_id(answer, "id", "endpointId")
        self.stdout.write(f"Registered {url} as endpoint {endpoint_id or '?'}.")
        if secret:
            self.stdout.write(
                f"Set RENTAHUMAN_WEBHOOK_SECRET={secret} (shown only once)."
            )
        else:
            self.stdout.write(f"No secret in RentAHuman's answer: {answer}")

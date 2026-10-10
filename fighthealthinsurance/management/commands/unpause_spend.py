"""Lift a provider's credit or quota pause (ml/spend.py) before 00:00 UTC.

A provider that refuses for credit or quota is paused on every pod for the
rest of the UTC day. After a top-up, lift it by hand from any pod::

    python manage.py unpause_spend anthropic
    python manage.py unpause_spend deepinfra --use chat

Other pods lift it at their next ledger refresh. A provider still out of
credit is paused again by its next refusal.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from fighthealthinsurance.ml import spend

USES = ("*", spend.CHAT, spend.LETTERS, spend.TRIAGE, spend.ASSISTANT, spend.OTHER)


class Command(BaseCommand):
    help = (
        "Lift a provider's credit or quota pause before 00:00 UTC, on every "
        f"pod at its next refresh (about {int(spend.REFRESH_SECONDS)}s). "
        f"Providers: {', '.join(spend.PROVIDERS)}."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "provider",
            choices=spend.PROVIDERS,
            help=f"The paused provider: {', '.join(spend.PROVIDERS)}.",
        )
        parser.add_argument(
            "--use",
            default="*",
            choices=USES,
            help='The paused use (default "*", the every-use pause a credit '
            "refusal sets).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        provider = options["provider"]
        use = options["use"]
        name = spend.counter(provider, use)
        # This process has its own copy of the ledger, and a worker thread
        # that may not run before it exits: read the shared pauses first, and
        # store the lift before returning.
        try:
            spend.sync_now()
        except Exception as e:
            raise CommandError(
                f"Spend ledger not reachable, nothing lifted: {type(e).__name__}: {e}"
            )
        lifted = spend.unpause(provider, use, reason="by hand, manage.py unpause_spend")
        try:
            spend.sync_now()
        except Exception as e:
            # The lift may or may not have been stored before the failure.
            # Running the command again is safe either way: it says "not
            # paused" once the lift is stored.
            raise CommandError(
                f"Lifted {name} here, but could not confirm it reached the "
                f"shared ledger ({type(e).__name__}: {e}); run it again to check."
                if lifted
                else f"Spend ledger not reachable: {type(e).__name__}: {e}"
            )
        if lifted:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Lifted the pause on {name}; other pods lift it at their "
                    f"next refresh (about {int(spend.REFRESH_SECONDS)}s)."
                )
            )
        else:
            self.stdout.write(f"{name} is not paused today; nothing to lift.")
        left = [p for p in spend.active_pauses() if p.startswith(provider + ":")]
        if left:
            self.stdout.write(
                f"Still paused: {', '.join(left)} (lift with --use <use>)."
            )

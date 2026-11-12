"""Run one HumanJudge round by hand: send the Arena drafts not sent yet,
fetch new verdicts and update each touched case's "humanjudge" pick.

The chooser refill actor does the same every tick; this is for a first
backfill, or to check the key and model id after setting them. Off (and
says so) unless HUMANJUDGE_API_KEY is set. See chooser_humanjudge.
"""

from typing import Any

from asgiref.sync import async_to_sync
from django.core.management.base import BaseCommand

from fighthealthinsurance import chooser_humanjudge


class Command(BaseCommand):
    help = "Send Arena drafts to HumanJudge and store its new verdicts"

    def handle(self, *args: Any, **options: Any) -> None:
        summary = async_to_sync(chooser_humanjudge.sync)()
        if summary is None:
            self.stdout.write(
                "HumanJudge is off: set HUMANJUDGE_API_KEY (and leave "
                "CHOOSER_HUMANJUDGE unset or true)"
            )
            return
        self.stdout.write(
            f"Sent {summary['sent']} draft(s); {summary['cases_with_new_verdicts']} "
            f"case(s) got new verdicts; {summary['picks']} have a HumanJudge pick"
        )

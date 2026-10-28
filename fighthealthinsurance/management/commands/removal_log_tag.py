"""Print the tag a data removal for an email is logged under.

Removals log a tag instead of the email, and the files they delete log the
same tag. To check one went through, run this on a pod and search the logs
for the tag it prints::

    python manage.py removal_log_tag

It asks for the email, so the address stays out of shell history.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from fighthealthinsurance.log_tags import email_tag


class Command(BaseCommand):
    help = "Print the log tag of a data removal for an email."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "email", nargs="?", help="Leave out to be asked for it instead."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        email = options["email"] or input("Email: ")
        self.stdout.write(email_tag(email))

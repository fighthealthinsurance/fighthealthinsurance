"""Combined local development database setup command.

Replaces separate invocations of migrate, loaddata, ensure_adminuser, and make_user
with a single management command to avoid repeated Django startup overhead.
"""

from typing import Any

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run all local dev DB setup: migrate, load fixtures, create test users."

    def handle(self, *args: str, **options: Any) -> None:
        self.stdout.write("Running migrations...")
        call_command("migrate", verbosity=1)

        self.stdout.write("Loading fixtures...")
        call_command("loaddata", "initial")
        call_command("loaddata", "followup")
        call_command("loaddata", "plan_source")

        self.stdout.write("Ensuring admin user...")
        call_command("ensure_adminuser", username="admin", password="admin")

        self.stdout.write("Creating test users...")
        call_command(
            "make_user",
            username="test@test.com",
            domain="testfarts1",
            password="farts12345678",
            email="test@test.com",
            visible_phone_number="42",
            is_provider=True,
            first_name="TestFarts",
        )
        call_command(
            "make_user",
            username="test-patient@test.com",
            domain="testfarts1",
            password="farts12345678",
            email="test-patient@test.com",
            visible_phone_number="42",
            is_provider=False,
        )

        self.stdout.write(self.style.SUCCESS("Local DB setup complete."))

"""Combined local development database setup command.

Replaces separate invocations of migrate, loaddata, ensure_adminuser, and make_user
with a single management command to avoid repeated Django startup overhead.
"""

import os
from typing import Any

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run all local dev DB setup: migrate, load fixtures, create test users."

    def handle(self, *args: str, **options: Any) -> None:
        if not settings.DEBUG:
            self.stderr.write(
                self.style.ERROR("This command can only be run with DEBUG=True.")
            )
            return

        self.stdout.write("Running migrations...")
        call_command("migrate", verbosity=1)

        self.stdout.write("Loading fixtures...")
        call_command("loaddata", "initial")
        call_command("loaddata", "followup")
        call_command("loaddata", "plan_source")
        # insurance_companies must come after plan_source because the
        # InsurancePlan rows reference PlanSource pks (100/300/700/1100/...).
        call_command("loaddata", "insurance_companies")
        # pa_requirements rows reference InsuranceCompany pks; load after.
        call_command("loaddata", "pa_requirements")

        self.stdout.write("Ensuring admin user...")
        admin_password = os.environ.get("LOCAL_ADMIN_PASSWORD", "admin")
        call_command("ensure_adminuser", username="admin", password=admin_password)

        self.stdout.write("Creating test users...")
        test_password = os.environ.get("LOCAL_TEST_PASSWORD", "farts12345678")
        call_command(
            "make_user",
            username="test@test.com",
            domain="testfarts1",
            password=test_password,
            email="test@test.com",
            visible_phone_number="42",
            is_provider=True,
            first_name="TestFarts",
        )
        call_command(
            "make_user",
            username="test-patient@test.com",
            domain="testfarts1",
            password=test_password,
            email="test-patient@test.com",
            visible_phone_number="42",
            is_provider=False,
        )

        self.stdout.write(self.style.SUCCESS("Local DB setup complete."))

"""
Management command to detect potential username conflicts before email-only migration.
This command identifies:
1. Multiple users with the same email address
2. Users with phone-based usernames that don't match their email
3. Domain conflicts that might arise from email-only authentication
"""

import csv
import json
from collections import defaultdict
from typing import Dict, List, Any

from django.core.management.base import BaseCommand, CommandParser
from django.contrib.auth import get_user_model
from django.db.models import Count
from loguru import logger

from fhi_users.models import UserDomain, PatientUser, ProfessionalUser

User = get_user_model()


class Command(BaseCommand):
    help = "Detect potential username conflicts before email-only migration"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--output-format",
            choices=["csv", "json", "console"],
            default="console",
            help="Output format for the conflict report",
        )
        parser.add_argument(
            "--output-file",
            type=str,
            help="Output file path (required for csv/json formats)",
        )

    def handle(self, *args, **options) -> None:
        output_format = options["output_format"]
        output_file = options["output_file"]

        if output_format in ["csv", "json"] and not output_file:
            self.stdout.write(
                self.style.ERROR("--output-file is required for csv/json formats")
            )
            return

        conflicts = self.detect_conflicts()

        if output_format == "console":
            self.print_console_report(conflicts)
        elif output_format == "csv":
            self.write_csv_report(conflicts, output_file)
        elif output_format == "json":
            self.write_json_report(conflicts, output_file)

        self.stdout.write(
            self.style.SUCCESS(
                f"Conflict detection complete. Found {len(conflicts['duplicate_emails'])} duplicate emails, "
                f"{len(conflicts['username_email_mismatches'])} username/email mismatches, "
                f"and {len(conflicts['domain_conflicts'])} potential domain conflicts."
            )
        )

    def detect_conflicts(self) -> Dict[str, List[Any]]:
        """Detect various types of conflicts that could arise from email-only migration."""

        conflicts = {
            "duplicate_emails": [],
            "username_email_mismatches": [],
            "domain_conflicts": [],
            "orphaned_usernames": [],
        }

        # 1. Find duplicate email addresses
        duplicate_emails = (
            User.objects.values("email")
            .annotate(count=Count("email"))
            .filter(count__gt=1, email__isnull=False)
            .exclude(email="")
        )

        for email_data in duplicate_emails:
            email = email_data["email"]
            users = User.objects.filter(email=email)
            user_details = []

            for user in users:
                domain_id = None
                if "🐼" in user.username:
                    domain_id = user.username.split("🐼")[-1]

                user_details.append(
                    {
                        "id": user.id,
                        "username": user.username,
                        "email": user.email,
                        "domain_id": domain_id,
                        "is_active": user.is_active,
                        "date_joined": user.date_joined.isoformat(),
                    }
                )

            conflicts["duplicate_emails"].append(
                {
                    "email": email,
                    "count": email_data["count"],
                    "users": user_details,
                }
            )

        # 2. Find username/email mismatches (where username != email before 🐼)
        all_users = User.objects.all()
        for user in all_users:
            if "🐼" in user.username:
                username_part = user.username.split("🐼")[0]
                domain_id = user.username.split("🐼")[-1]

                if username_part != user.email:
                    conflicts["username_email_mismatches"].append(
                        {
                            "user_id": user.id,
                            "username": user.username,
                            "username_part": username_part,
                            "email": user.email,
                            "domain_id": domain_id,
                            "is_active": user.is_active,
                        }
                    )

        # 3. Find potential domain conflicts (multiple domains for same email domain)
        email_domains = defaultdict(set)
        for user in all_users:
            if user.email and "@" in user.email:
                email_domain = user.email.split("@")[1].lower()
                if "🐼" in user.username:
                    user_domain_id = user.username.split("🐼")[-1]
                    email_domains[email_domain].add(user_domain_id)

        for email_domain, domain_ids in email_domains.items():
            if len(domain_ids) > 1:
                conflicts["domain_conflicts"].append(
                    {
                        "email_domain": email_domain,
                        "user_domain_ids": list(domain_ids),
                        "count": len(domain_ids),
                    }
                )

        # 4. Find orphaned usernames (users without emails)
        orphaned_users = User.objects.filter(email__isnull=True) | User.objects.filter(
            email=""
        )
        for user in orphaned_users:
            conflicts["orphaned_usernames"].append(
                {
                    "user_id": user.id,
                    "username": user.username,
                    "is_active": user.is_active,
                    "date_joined": user.date_joined.isoformat(),
                }
            )

        return conflicts

    def print_console_report(self, conflicts: Dict[str, List[Any]]) -> None:
        """Print conflict report to console."""

        self.stdout.write(
            self.style.WARNING("\n=== USERNAME CONFLICT DETECTION REPORT ===\n")
        )

        # Duplicate emails
        if conflicts["duplicate_emails"]:
            self.stdout.write(
                self.style.ERROR(
                    f"DUPLICATE EMAILS ({len(conflicts['duplicate_emails'])}):"
                )
            )
            for dup in conflicts["duplicate_emails"]:
                self.stdout.write(f"  Email: {dup['email']} ({dup['count']} users)")
                for user in dup["users"]:
                    self.stdout.write(
                        f"    - ID: {user['id']}, Username: {user['username']}, Active: {user['is_active']}"
                    )
            self.stdout.write("")

        # Username/email mismatches
        if conflicts["username_email_mismatches"]:
            self.stdout.write(
                self.style.ERROR(
                    f"USERNAME/EMAIL MISMATCHES ({len(conflicts['username_email_mismatches'])}):"
                )
            )
            for mismatch in conflicts["username_email_mismatches"][
                :10
            ]:  # Show first 10
                self.stdout.write(
                    f"  ID: {mismatch['user_id']}, Username part: {mismatch['username_part']}, Email: {mismatch['email']}"
                )
            if len(conflicts["username_email_mismatches"]) > 10:
                self.stdout.write(
                    f"  ... and {len(conflicts['username_email_mismatches']) - 10} more"
                )
            self.stdout.write("")

        # Domain conflicts
        if conflicts["domain_conflicts"]:
            self.stdout.write(
                self.style.ERROR(
                    f"DOMAIN CONFLICTS ({len(conflicts['domain_conflicts'])}):"
                )
            )
            for conflict in conflicts["domain_conflicts"]:
                self.stdout.write(
                    f"  Email domain: {conflict['email_domain']} maps to {conflict['count']} user domains"
                )
                self.stdout.write(
                    f"    Domain IDs: {', '.join(conflict['user_domain_ids'])}"
                )
            self.stdout.write("")

        # Orphaned usernames
        if conflicts["orphaned_usernames"]:
            self.stdout.write(
                self.style.ERROR(
                    f"ORPHANED USERNAMES ({len(conflicts['orphaned_usernames'])}):"
                )
            )
            for orphan in conflicts["orphaned_usernames"][:10]:  # Show first 10
                self.stdout.write(
                    f"  ID: {orphan['user_id']}, Username: {orphan['username']}, Active: {orphan['is_active']}"
                )
            if len(conflicts["orphaned_usernames"]) > 10:
                self.stdout.write(
                    f"  ... and {len(conflicts['orphaned_usernames']) - 10} more"
                )
            self.stdout.write("")

        if not any(conflicts.values()):
            self.stdout.write(
                self.style.SUCCESS("No conflicts detected! Migration should be safe.")
            )

    def write_csv_report(
        self, conflicts: Dict[str, List[Any]], output_file: str
    ) -> None:
        """Write conflict report to CSV file."""
        with open(output_file, "w", newline="") as csvfile:
            writer = csv.writer(csvfile)

            # Write duplicate emails
            writer.writerow(["DUPLICATE EMAILS"])
            writer.writerow(
                ["Email", "Count", "User IDs", "Usernames", "Active States"]
            )
            for dup in conflicts["duplicate_emails"]:
                user_ids = [str(u["id"]) for u in dup["users"]]
                usernames = [u["username"] for u in dup["users"]]
                active_states = [str(u["is_active"]) for u in dup["users"]]
                writer.writerow(
                    [
                        dup["email"],
                        dup["count"],
                        ";".join(user_ids),
                        ";".join(usernames),
                        ";".join(active_states),
                    ]
                )

            writer.writerow([])  # Empty row

            # Write username/email mismatches
            writer.writerow(["USERNAME/EMAIL MISMATCHES"])
            writer.writerow(
                ["User ID", "Username", "Username Part", "Email", "Domain ID", "Active"]
            )
            for mismatch in conflicts["username_email_mismatches"]:
                writer.writerow(
                    [
                        mismatch["user_id"],
                        mismatch["username"],
                        mismatch["username_part"],
                        mismatch["email"],
                        mismatch["domain_id"],
                        mismatch["is_active"],
                    ]
                )

        self.stdout.write(self.style.SUCCESS(f"CSV report written to {output_file}"))

    def write_json_report(
        self, conflicts: Dict[str, List[Any]], output_file: str
    ) -> None:
        """Write conflict report to JSON file."""
        with open(output_file, "w") as jsonfile:
            json.dump(conflicts, jsonfile, indent=2, default=str)

        self.stdout.write(self.style.SUCCESS(f"JSON report written to {output_file}"))

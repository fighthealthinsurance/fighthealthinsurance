"""
Management command to migrate usernames to email-only format.
This command:
1. Converts all usernames from 'email🐼domain_id' to just 'email'
2. Handles conflicts by creating backup records
3. Provides dry-run mode for safe testing
4. Creates migration log for rollback if needed
"""

import json
from datetime import datetime
from typing import Dict, List, Any

from django.core.management.base import BaseCommand, CommandParser
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from fhi_users.models import UserDomain, PatientUser, ProfessionalUser

User = get_user_model()


class Command(BaseCommand):
    help = "Migrate usernames from domain-scoped to email-only format"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be changed without making actual changes",
        )
        parser.add_argument(
            "--log-file",
            type=str,
            default=f"username_migration_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            help="Log file for migration actions (for rollback)",
        )
        parser.add_argument(
            "--handle-conflicts",
            choices=["skip", "archive", "merge"],
            default="skip",
            help="How to handle username conflicts",
        )

    def handle(self, *args, **options) -> None:
        dry_run = options["dry_run"]
        log_file = options["log_file"]
        conflict_strategy = options["handle_conflicts"]

        if dry_run:
            self.stdout.write(
                self.style.WARNING("DRY RUN MODE - No changes will be made")
            )

        migration_log = {
            "timestamp": timezone.now().isoformat(),
            "dry_run": dry_run,
            "conflict_strategy": conflict_strategy,
            "actions": [],
            "conflicts": [],
            "errors": [],
        }

        try:
            with transaction.atomic():
                self.migrate_usernames(migration_log, dry_run, conflict_strategy)

                if dry_run:
                    # Rollback the transaction for dry run
                    transaction.set_rollback(True)

            # Write log file
            with open(log_file, "w") as f:
                json.dump(migration_log, f, indent=2, default=str)

            self.print_summary(migration_log)

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Migration failed: {str(e)}"))
            migration_log["errors"].append(str(e))
            # Still write log file for debugging
            with open(log_file, "w") as f:
                json.dump(migration_log, f, indent=2, default=str)
            raise

    def migrate_usernames(
        self, migration_log: Dict, dry_run: bool, conflict_strategy: str
    ) -> None:
        """Perform the actual username migration."""

        users_to_migrate = User.objects.filter(username__contains="🐼")
        total_users = users_to_migrate.count()

        self.stdout.write(f"Found {total_users} users to migrate")

        migrated_count = 0
        conflict_count = 0

        for user in users_to_migrate:
            try:
                # Extract email and domain from current username
                if "🐼" not in user.username:
                    continue

                username_parts = user.username.split("🐼")
                current_email_part = username_parts[0]
                domain_id = username_parts[-1]

                # Determine target username (should be the user's email)
                target_username = user.email if user.email else current_email_part

                # Check for conflicts
                existing_user = None
                try:
                    existing_user = User.objects.get(username=target_username)
                    if existing_user.id != user.id:
                        # We have a conflict
                        conflict_count += 1
                        conflict_info = {
                            "current_user_id": user.id,
                            "current_username": user.username,
                            "target_username": target_username,
                            "existing_user_id": existing_user.id,
                            "existing_username": existing_user.username,
                            "strategy_used": conflict_strategy,
                        }
                        migration_log["conflicts"].append(conflict_info)

                        if conflict_strategy == "skip":
                            self.stdout.write(f"SKIP: Conflict for {target_username}")
                            continue
                        elif conflict_strategy == "archive":
                            # Archive the existing user by appending timestamp
                            archived_username = f"{existing_user.username}_archived_{int(timezone.now().timestamp())}"
                            if not dry_run:
                                existing_user.username = archived_username
                                existing_user.is_active = False
                                existing_user.save()
                            conflict_info["archived_username"] = archived_username
                        elif conflict_strategy == "merge":
                            # For now, skip merge as it's complex - would need to merge all related objects
                            self.stdout.write(
                                f"MERGE: Not implemented yet, skipping {target_username}"
                            )
                            continue

                except User.DoesNotExist:
                    # No conflict, proceed with migration
                    pass

                # Perform the migration
                old_username = user.username
                new_username = target_username

                action = {
                    "user_id": user.id,
                    "old_username": old_username,
                    "new_username": new_username,
                    "email": user.email,
                    "domain_id": domain_id,
                    "migrated": not dry_run,
                }

                if not dry_run:
                    user.username = new_username
                    user.save()

                migration_log["actions"].append(action)
                migrated_count += 1

                if migrated_count % 100 == 0:
                    self.stdout.write(
                        f"Migrated {migrated_count}/{total_users} users..."
                    )

            except Exception as e:
                error_info = {
                    "user_id": user.id,
                    "username": user.username,
                    "error": str(e),
                }
                migration_log["errors"].append(error_info)
                self.stdout.write(
                    self.style.ERROR(f"Error migrating user {user.id}: {str(e)}")
                )

        migration_log["summary"] = {
            "total_users": total_users,
            "migrated_count": migrated_count,
            "conflict_count": conflict_count,
            "error_count": len(migration_log["errors"]),
        }

    def print_summary(self, migration_log: Dict) -> None:
        """Print migration summary."""
        summary = migration_log["summary"]

        self.stdout.write(self.style.SUCCESS("\n=== MIGRATION SUMMARY ==="))
        self.stdout.write(f"Total users found: {summary['total_users']}")
        self.stdout.write(f"Successfully migrated: {summary['migrated_count']}")
        self.stdout.write(f"Conflicts encountered: {summary['conflict_count']}")
        self.stdout.write(f"Errors: {summary['error_count']}")

        if migration_log["dry_run"]:
            self.stdout.write(
                self.style.WARNING("\nThis was a DRY RUN - no actual changes were made")
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nMigration completed successfully!"))

        self.stdout.write(
            f"\nDetailed log saved to: {migration_log.get('log_file', 'migration log')}"
        )

        if migration_log["conflicts"]:
            self.stdout.write(
                self.style.WARNING(
                    f"\nConflicts were handled using strategy: {migration_log['conflict_strategy']}"
                )
            )

        if migration_log["errors"]:
            self.stdout.write(
                self.style.ERROR(
                    f"\nErrors occurred during migration. Check the log file for details."
                )
            )

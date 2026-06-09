"""Migration hygiene guards.

Catches two failure modes before they hit the deploy pipeline:

- model changes without a generated migration (``makemigrations --check``),
- conflicting leaf migrations from parallel branches (multiple 0NNN_* leaves),
  which previously broke every test suite at setup with an opaque
  ``Conflicting migrations detected`` error instead of one clear failure.
"""

from io import StringIO

from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase

# Scoped to first-party apps: third-party apps (e.g. django-mfa2) ship
# migrations that DEFAULT_AUTO_FIELD makes Django want to regenerate, which
# isn't actionable from this repo.
FIRST_PARTY_APPS = ["fighthealthinsurance", "fhi_users", "charts"]


class MigrationStateTests(TestCase):
    def test_no_model_changes_missing_migrations(self):
        try:
            call_command(
                "makemigrations",
                *FIRST_PARTY_APPS,
                "--check",
                "--dry-run",
                verbosity=1,
                stdout=StringIO(),
                stderr=StringIO(),
            )
        except SystemExit:
            self.fail(
                "Model changes without migrations detected; run "
                "`python manage.py makemigrations`"
            )

    def test_no_conflicting_leaf_migrations(self):
        loader = MigrationLoader(None, ignore_no_migrations=True)
        conflicts = loader.detect_conflicts()
        self.assertFalse(
            conflicts,
            f"Conflicting leaf migrations detected: {conflicts}; run "
            "`python manage.py makemigrations --merge` or renumber",
        )

    def test_renamed_rxnorm_migration_replaces_original_name(self):
        """0175_rxnormconcept shipped to prod as 0173_rxnormconcept before
        being renamed (#787). Without ``replaces`` (and its original 0172
        dependency), databases that applied it under the old name re-create
        the table on the next deploy and the migration job crash-loops."""
        loader = MigrationLoader(None, ignore_no_migrations=True)
        migration = loader.disk_migrations[
            ("fighthealthinsurance", "0175_rxnormconcept")
        ]
        self.assertIn(
            ("fighthealthinsurance", "0173_rxnormconcept"),
            migration.replaces,
        )
        self.assertIn(
            ("fighthealthinsurance", "0172_payerpriorauthrequirement"),
            migration.dependencies,
        )

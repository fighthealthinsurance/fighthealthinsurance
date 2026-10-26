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

# The apps whose migrations live in this repo. A miss here is fixed by
# running makemigrations; a miss in a third-party app needs its AppConfig
# pinned instead (see fhi_users/mfa_app_config.py).
FIRST_PARTY_APPS = ["fighthealthinsurance", "fhi_users", "charts"]


class MigrationStateTests(TestCase):
    def test_no_installed_app_has_model_changes_missing_migrations(self):
        """build.sh's setup_templates.sh runs an unscoped
        ``makemigrations --check`` and, when it fails, generates and applies
        migrations on the spot, so a third-party app's drift (django-mfa2's
        ids against DEFAULT_AUTO_FIELD) must not make it fail."""
        try:
            call_command(
                "makemigrations",
                "--check",
                "--dry-run",
                verbosity=1,
                stdout=StringIO(),
                stderr=StringIO(),
            )
        except SystemExit:
            self.fail(
                "An installed app has model changes without migrations; for a "
                "third-party app, pin its AppConfig's default_auto_field"
            )

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

"""Deploy-time fixture loading guards.

The k8s ``web-migrations`` Job and the dev container both load fixtures via
``scripts/start-server.sh``. These tests pin the script's ``loaddata``
sequence to the fixtures the application expects at runtime and verify the
sequence actually loads (in deploy order, idempotently), so a fixture added
to the repo but forgotten in the deploy script fails CI instead of shipping
an empty table to prod (as nearly happened with ``pa_requirements``).
"""

import re
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from fighthealthinsurance.models import (
    AppealTemplates,
    DataSource,
    InsuranceCompany,
    PayerPriorAuthRequirement,
    PlanSource,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
START_SERVER = REPO_ROOT / "scripts" / "start-server.sh"
FIXTURES_DIR = REPO_ROOT / "fighthealthinsurance" / "fixtures"

# The fixtures every deployment must load, in deploy order.
# ``pa_requirements`` references InsuranceCompany rows by pk, so it must
# come after ``insurance_companies``.
DEPLOY_FIXTURES = [
    "initial",
    "followup",
    "plan_source",
    "insurance_companies",
    "pa_requirements",
]

# YAML fixtures that intentionally are NOT loaded at deploy time. Add an
# entry here (with a reason) instead of letting the directory-coverage test
# fail when introducing a fixture that shouldn't ship to prod.
NON_DEPLOY_FIXTURES = {
    "chooser_test_data",  # test-only data for chooser tests
}


def _loaddata_sequences(script_text: str) -> list[list[str]]:
    """Extract each block's ordered ``manage.py loaddata`` fixture names.

    Blocks are separated on the Dev-environment guard so the MIGRATIONS job
    sequence and the dev-startup sequence are checked independently.
    """
    blocks = script_text.split('if [ "$ENVIRONMENT" == "Dev" ]')
    pattern = re.compile(r"^\s*python manage\.py loaddata (\S+)\s*$", re.MULTILINE)
    return [pattern.findall(block) for block in blocks]


class StartServerFixtureSequenceTests(TestCase):
    """The deploy script must load every deploy fixture, in order."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.script_text = START_SERVER.read_text()

    def test_migrations_block_loads_all_deploy_fixtures_in_order(self):
        migrations_seq = _loaddata_sequences(self.script_text)[0]
        self.assertEqual(
            migrations_seq,
            DEPLOY_FIXTURES,
            "scripts/start-server.sh MIGRATIONS block fixture sequence "
            "diverged from the expected deploy fixtures",
        )

    def test_dev_block_loads_all_deploy_fixtures_in_order(self):
        sequences = _loaddata_sequences(self.script_text)
        self.assertEqual(
            len(sequences),
            2,
            "Expected a Dev-environment block in scripts/start-server.sh",
        )
        self.assertEqual(
            sequences[1],
            DEPLOY_FIXTURES,
            "scripts/start-server.sh Dev block fixture sequence diverged "
            "from the expected deploy fixtures",
        )

    def test_every_yaml_fixture_is_deployed_or_explicitly_excluded(self):
        yaml_fixtures = {p.stem for p in FIXTURES_DIR.glob("*.yaml")}
        unaccounted = yaml_fixtures - set(DEPLOY_FIXTURES) - NON_DEPLOY_FIXTURES
        self.assertFalse(
            unaccounted,
            f"New fixture(s) {sorted(unaccounted)} are neither loaded by "
            "scripts/start-server.sh (add to DEPLOY_FIXTURES and the deploy "
            "script) nor explicitly excluded in NON_DEPLOY_FIXTURES",
        )


class DeployFixtureLoadTests(TestCase):
    """The deploy fixture sequence must load cleanly and idempotently."""

    def _assert_loaded(self):
        self.assertGreater(InsuranceCompany.objects.count(), 0)
        self.assertGreater(PayerPriorAuthRequirement.objects.count(), 0)
        self.assertGreater(PlanSource.objects.count(), 0)
        self.assertGreater(DataSource.objects.count(), 0)
        self.assertGreater(AppealTemplates.objects.count(), 0)

    def test_deploy_sequence_loads_in_deploy_order(self):
        for fixture in DEPLOY_FIXTURES:
            call_command("loaddata", fixture, verbosity=0)
        self._assert_loaded()

    def test_deploy_sequence_is_idempotent_on_rerun(self):
        for fixture in DEPLOY_FIXTURES:
            call_command("loaddata", fixture, verbosity=0)
        counts = {
            "company": InsuranceCompany.objects.count(),
            "pa": PayerPriorAuthRequirement.objects.count(),
            "plan_source": PlanSource.objects.count(),
            "templates": AppealTemplates.objects.count(),
        }
        for fixture in DEPLOY_FIXTURES:
            call_command("loaddata", fixture, verbosity=0)
        self.assertEqual(
            counts,
            {
                "company": InsuranceCompany.objects.count(),
                "pa": PayerPriorAuthRequirement.objects.count(),
                "plan_source": PlanSource.objects.count(),
                "templates": AppealTemplates.objects.count(),
            },
        )

"""Only the test configurations mark the process as a test run.

Test, TestSync and TestActor set TESTING=True and point the CMS Coverage and
NICE APIs at http://127.0.0.1:1. Those writes used to sit in the test class
bodies, and Python runs every class body when settings.py is imported, so a
Dev process got them too: TESTING switched off test-aware code such as the
startup model probe, and the CMS and NICE lookups went to a dead address.
They now run in a pre_setup that django-configurations calls only for the
configuration in use.

Each case imports the settings in a fresh interpreter with
DJANGO_CONFIGURATION set, through the django-configurations importer as
manage.py does, and reports what the three variables ended up as. The
child's environment is a copy of this one without the three, and without the
MinIO settings, because loading Prod builds its storage client from them.

The child is not under pytest, so under Dev or Prod get_env_variable would
fall back to the repo's .env, a developer's real keys included. The child
points that fallback at no file before it imports anything else, as the
async-unit conftest does for this process, and reports every attempt to
parse a .env without parsing it. Every case checks there were none.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_TEST_ONLY_VARIABLES = ("TESTING", "CMS_COVERAGE_API_URL", "NICE_API_BASE_URL")

# ENVIRONMENT too: settings.py reads it through get_env_variable on every
# import, so without it that read always misses the environment and reaches
# the .env fallback, which is the path the no-.env check below guards.
_LEFT_OUT_OF_THE_CHILD = _TEST_ONLY_VARIABLES + ("ENVIRONMENT",)

_LOAD_SETTINGS_AND_REPORT = f"""
import json
import os

from fighthealthinsurance import env_utils

dotenv_reads = []


# Records the attempt and answers as if the file were missing, so even if the
# line below stopped working the child would still never parse a real .env.
def _record_instead_of_parsing(source, *args, **kwargs):
    dotenv_reads.append(str(source))
    raise FileNotFoundError(source)


env_utils.RepositoryEnv = _record_instead_of_parsing
env_utils.LOCAL_DOTENV_PATH = None

from configurations import importer

importer.install()
from django.conf import settings

settings.DEBUG
print("RESULT " + json.dumps({{
    "variables": {{name: os.environ.get(name) for name in {_TEST_ONLY_VARIABLES!r}}},
    "dotenv_reads": dotenv_reads,
}}))
"""


def _environment_after_loading(configuration: str) -> Dict[str, Optional[str]]:
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in _LEFT_OUT_OF_THE_CHILD and not name.startswith("EX_MINIO_")
    }
    env["DJANGO_SETTINGS_MODULE"] = "fighthealthinsurance.settings"
    env["DJANGO_CONFIGURATION"] = configuration
    # Prod reads its secret key from the environment; a placeholder will do.
    env["SECRET_KEY"] = "placeholder-secret-key-for-a-settings-import"
    result = subprocess.run(
        [sys.executable, "-c", _LOAD_SETTINGS_AND_REPORT],
        env=env,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    lines = [line for line in result.stdout.splitlines() if line.startswith("RESULT ")]
    assert len(lines) == 1, result.stdout[-3000:]
    report = json.loads(lines[0][len("RESULT ") :])
    assert report["dotenv_reads"] == [], "the settings import tried to read a .env"
    return report["variables"]


@pytest.mark.parametrize("configuration", ["Dev", "Prod"])
def test_a_non_test_configuration_leaves_the_test_variables_unset(configuration):
    assert _environment_after_loading(configuration) == {
        "TESTING": None,
        "CMS_COVERAGE_API_URL": None,
        "NICE_API_BASE_URL": None,
    }


@pytest.mark.parametrize("configuration", ["Test", "TestSync", "TestActor"])
def test_a_test_configuration_sets_all_three(configuration):
    assert _environment_after_loading(configuration) == {
        "TESTING": "True",
        "CMS_COVERAGE_API_URL": "http://127.0.0.1:1",
        "NICE_API_BASE_URL": "http://127.0.0.1:1",
    }

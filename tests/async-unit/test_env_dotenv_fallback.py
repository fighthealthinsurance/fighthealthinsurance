"""get_env_variable: the process environment first, then .env on a local run.

The model backend settings (fighthealthinsurance/ml/ml_models.py and
ml_router.py) and every other get_env_variable caller read a variable from
the process environment when it is there, and otherwise from the repo's
.env, but only on a local run. Never under the Test, TestSync or TestActor
configurations or under pytest, so a developer's real keys in .env can't
reach the test suite, and never in a deployment, which takes its secrets
from Kubernetes.

Every test here points the helper at a .env of its own under tmp_path, never
the repo's, and simulates the kind of process it needs inside the test only:
the environment through patch.dict, the pytest check through patch.object.
"""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Optional, Sequence
from unittest.mock import patch

import pytest

from fighthealthinsurance import env_utils
from fighthealthinsurance.env_utils import get_env_variable, is_deployed_environment
from fighthealthinsurance.ml.ml_models import RemoteAnthropic
from fighthealthinsurance.ml.ml_router import MLRouter
from fighthealthinsurance.ml.model_health_check import sanitize_error

# A name no real configuration uses, so the environment never already has it.
_SETTING = "FHI_DOTENV_FALLBACK_TEST_SETTING"

# What dotenv_allowed() reads from the environment, cleared for every test.
_GUARD_ENV_VARS = ("DJANGO_CONFIGURATION", "KUBERNETES_SERVICE_HOST", "FHI_DEPLOYED")


def _write_dotenv(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text)
    return path


@contextmanager
def _process(
    dotenv: Path,
    configuration: str = "Dev",
    under_pytest: bool = False,
    env: Optional[Dict[str, str]] = None,
    absent: Sequence[str] = (),
) -> Iterator[None]:
    """Run the body as one kind of process, restoring everything after.

    ``configuration`` becomes DJANGO_CONFIGURATION, ``under_pytest`` stands in
    for the pytest check (always true in this suite for real), ``env`` adds
    variables and ``absent`` removes more. The deployment markers and
    _SETTING start out unset. The defaults describe a local Dev run.
    """
    with patch.dict(os.environ):
        for name in (*_GUARD_ENV_VARS, _SETTING, *absent):
            os.environ.pop(name, None)
        os.environ["DJANGO_CONFIGURATION"] = configuration
        os.environ.update(env or {})
        with (
            patch.object(env_utils, "LOCAL_DOTENV_PATH", dotenv),
            patch.object(env_utils, "_running_under_pytest", return_value=under_pytest),
        ):
            yield


class TestTheEnvironmentComesFirst:
    def test_the_environment_beats_dotenv(self, tmp_path):
        dotenv = _write_dotenv(tmp_path, f"{_SETTING}=from-dotenv\n")
        with _process(dotenv, env={_SETTING: "from-env"}):
            assert get_env_variable(_SETTING) == "from-env"

    def test_an_empty_environment_value_still_beats_dotenv(self, tmp_path):
        """Set but empty is set: each caller keeps its own empty-string rule."""
        dotenv = _write_dotenv(tmp_path, f"{_SETTING}=from-dotenv\n")
        with _process(dotenv, env={_SETTING: ""}):
            assert get_env_variable(_SETTING, "default") == ""

    def test_dotenv_fills_in_on_a_local_run_when_the_environment_lacks_it(
        self, tmp_path
    ):
        dotenv = _write_dotenv(tmp_path, f"# a comment\n{_SETTING}='from-dotenv'\n")
        with _process(dotenv):
            assert get_env_variable(_SETTING, "default") == "from-dotenv"

    def test_the_default_when_neither_has_it(self, tmp_path):
        dotenv = _write_dotenv(tmp_path, "SOMETHING_ELSE=1\n")
        with _process(dotenv):
            assert get_env_variable(_SETTING, "default") == "default"
            assert get_env_variable(_SETTING) is None

    def test_a_missing_dotenv_file_means_the_default(self, tmp_path):
        with _process(tmp_path / "no-such.env"):
            assert get_env_variable(_SETTING, "default") == "default"


class TestDotenvIsNeverReadInTestsOrDeployments:
    @pytest.mark.parametrize("configuration", ["Test", "TestSync", "TestActor"])
    def test_ignored_under_a_test_configuration(self, tmp_path, configuration):
        dotenv = _write_dotenv(tmp_path, f"{_SETTING}=from-dotenv\n")
        with _process(dotenv, configuration=configuration, under_pytest=False):
            assert get_env_variable(_SETTING, "default") == "default"

    def test_ignored_under_pytest_whatever_the_configuration(self, tmp_path):
        dotenv = _write_dotenv(tmp_path, f"{_SETTING}=from-dotenv\n")
        with _process(dotenv, configuration="Dev", under_pytest=True):
            assert get_env_variable(_SETTING, "default") == "default"

    def test_this_suite_counts_as_running_under_pytest(self):
        """The real input behind the stand-in above is on here."""
        assert env_utils._running_under_pytest() is True

    def test_ignored_in_a_kubernetes_deployment(self, tmp_path):
        dotenv = _write_dotenv(tmp_path, f"{_SETTING}=from-dotenv\n")
        with _process(dotenv, env={"KUBERNETES_SERVICE_HOST": "10.0.0.1"}):
            assert get_env_variable(_SETTING, "default") == "default"

    def test_ignored_in_an_opted_in_deployment(self, tmp_path):
        dotenv = _write_dotenv(tmp_path, f"{_SETTING}=from-dotenv\n")
        with _process(dotenv, env={"FHI_DEPLOYED": "1"}):
            assert get_env_variable(_SETTING, "default") == "default"

    def test_the_deployment_check_reads_only_the_environment(self, tmp_path):
        """A copied .env must not make a laptop look deployed, or not."""
        dotenv = _write_dotenv(
            tmp_path, "KUBERNETES_SERVICE_HOST=10.0.0.1\nFHI_DEPLOYED=1\n"
        )
        with _process(dotenv):
            assert is_deployed_environment() is False


class TestTheBackendsUseTheFallback:
    def test_a_hosted_key_only_in_dotenv_configures_the_backend_locally(self, tmp_path):
        dotenv = _write_dotenv(tmp_path, "ANTHROPIC_API_KEY=fake-key-in-dotenv\n")
        with _process(dotenv, absent=["ANTHROPIC_API_KEY"]):
            assert RemoteAnthropic.config_status() == ("configured", None)
            assert RemoteAnthropic.models() != []

    def test_the_same_key_under_a_test_configuration_configures_nothing(self, tmp_path):
        dotenv = _write_dotenv(tmp_path, "ANTHROPIC_API_KEY=fake-key-in-dotenv\n")
        with _process(dotenv, configuration="Test", absent=["ANTHROPIC_API_KEY"]):
            assert RemoteAnthropic.config_status()[0] == "not_configured"
            assert RemoteAnthropic.models() == []

    def test_the_router_allow_list_can_come_from_dotenv(self, tmp_path):
        dotenv = _write_dotenv(
            tmp_path, "ENABLED_REMOTE_MODELS=azure-openai/gpt-5.5, sonar\n"
        )
        with _process(dotenv, absent=["ENABLED_REMOTE_MODELS"]):
            assert MLRouter._enabled_model_names() == {"azure-openai/gpt-5.5", "sonar"}


class TestRedactionCoversDotenvKeys:
    def test_a_key_only_in_dotenv_is_redacted_from_provider_errors(self, tmp_path):
        """Health-check errors are logged, stored and emailed after redaction,
        which used to know only the environment's secrets."""
        secret = "fake-dotenv-secret-value"
        dotenv = _write_dotenv(tmp_path, f"FAKE_PROVIDER_API_KEY={secret}\n")
        with _process(dotenv, absent=["FAKE_PROVIDER_API_KEY"]):
            cleaned = sanitize_error(f"401 from provider: key {secret} rejected")
        assert secret not in cleaned
        assert "[REDACTED]" in cleaned

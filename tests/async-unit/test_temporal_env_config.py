"""Temporal reads only its host, fax queue and payload keys from the env.

Everything else Temporal needs (namespaces, the other task queues, TLS, the
activity pool) is fixed in settings.py, and the on/off switches come from the
fhi-feature-flags ConfigMap (k8s/feature-flags.yaml) every app pod loads.
These pin both sides so a pod can't drift onto another namespace or queue
because of a stray variable in a Secret.
"""

import re
from pathlib import Path

import pytest
import yaml

from fighthealthinsurance.settings import _temporal_host

REPO_DIR = Path(__file__).resolve().parent.parent.parent
SETTINGS = REPO_DIR / "fighthealthinsurance" / "settings.py"
FEATURE_FLAGS = REPO_DIR / "k8s" / "feature-flags.yaml"

# The only TEMPORAL_* values (besides the *_ENABLED switches) settings.py may
# read from the environment.
_TEMPORAL_ENV_VALUES = {
    "TEMPORAL_HOSTNAME",
    "TEMPORAL_TASK_QUEUE",
    "TEMPORAL_PAYLOAD_KEY",
    "TEMPORAL_RESEARCH_PAYLOAD_KEY",
}


def _flags_configmap() -> dict:
    return yaml.safe_load(FEATURE_FLAGS.read_text())["data"]


@pytest.mark.parametrize(
    "hostname, expected",
    [
        ("temporal-frontend", "temporal-frontend:7233"),
        ("temporal-frontend:7233", "temporal-frontend:7233"),
        ("temporal.example.com:443", "temporal.example.com:443"),
        ("  temporal-frontend  ", "temporal-frontend:7233"),
        ("", "localhost:7233"),
        ("[::1]", "[::1]:7233"),
        ("[::1]:7234", "[::1]:7234"),
    ],
)
def test_temporal_host_from_hostname(hostname, expected):
    assert _temporal_host(hostname) == expected


def test_settings_reads_no_other_temporal_values_from_env():
    """Only the four values and the TEMPORAL_*_ENABLED switches."""
    read = set(
        re.findall(r"os\.getenv\(\s*\"(TEMPORAL_[A-Z_]+)\"", SETTINGS.read_text())
    )
    unexpected = {
        name
        for name in read
        if name not in _TEMPORAL_ENV_VALUES and not name.endswith("_ENABLED")
    }
    assert not unexpected, f"settings.py reads {sorted(unexpected)} from the env"


def test_feature_flags_configmap_sets_the_temporal_host_by_hostname():
    assert _flags_configmap()["TEMPORAL_HOSTNAME"] == "temporal-frontend"


@pytest.mark.parametrize(
    "name",
    [n for n in _flags_configmap() if n not in _TEMPORAL_ENV_VALUES],
)
def test_feature_flags_configmap_keys_are_settings_flags(name):
    """A key settings.py doesn't read as a "true"-is-on flag does nothing."""
    settings_text = SETTINGS.read_text()
    quoted = re.escape(f'"{name}"')
    env_flag = re.search(rf"_env_flag\(\s*{quoted}", settings_text)
    equals_true = re.search(
        rf"os\.getenv\(\s*{quoted}\s*,\s*\"false\"\s*\)\.lower\(\)\s*==\s*\"true\"",
        settings_text,
    )
    assert env_flag or equals_true, f"{name} is not a settings.py env flag"


@pytest.mark.parametrize("value", list(_flags_configmap().values()))
def test_feature_flags_configmap_values_are_strings(value):
    """A bare YAML true/false is rejected by the API server for ConfigMap data."""
    assert isinstance(value, str)

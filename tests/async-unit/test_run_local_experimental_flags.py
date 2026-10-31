"""run_local.sh --all-experiments must turn on real flags.

The script exports each name in its EXPERIMENTAL_FLAGS array as "true". A
name settings.py no longer reads (a flag renamed or removed), or one read by
a parser that "true" doesn't satisfy, would still be printed as enabled at
startup while doing nothing. These pin the list to settings.py.
"""

import re
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent.parent
RUN_LOCAL = REPO_DIR / "scripts" / "run_local.sh"
SETTINGS = REPO_DIR / "fighthealthinsurance" / "settings.py"


def _experimental_flags() -> list[str]:
    match = re.search(
        r"^EXPERIMENTAL_FLAGS=\((.*?)^\)", RUN_LOCAL.read_text(), re.M | re.S
    )
    assert match, "EXPERIMENTAL_FLAGS=( ... ) not found in run_local.sh"
    names = []
    for line in match.group(1).splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def test_experimental_flags_list_is_not_empty():
    assert _experimental_flags()


def test_experimental_flags_have_no_duplicates():
    names = _experimental_flags()
    assert len(names) == len(set(names))


@pytest.mark.parametrize("name", _experimental_flags())
def test_experimental_flag_is_read_by_settings_and_true_turns_it_on(name):
    """settings.py reads it with _env_flag (1/true/yes/on) or with
    os.getenv(name, "false").lower() == "true"; "true" satisfies both."""
    settings_text = SETTINGS.read_text()
    quoted = re.escape(f'"{name}"')
    env_flag = re.search(rf"_env_flag\(\s*{quoted}", settings_text)
    equals_true = re.search(
        rf"os\.getenv\(\s*{quoted}\s*,\s*\"false\"\s*\)\.lower\(\)\s*==\s*\"true\"",
        settings_text,
    )
    assert env_flag or equals_true, (
        f"{name} is in run_local.sh's EXPERIMENTAL_FLAGS but settings.py does "
        "not read it as an env flag that 'true' turns on"
    )

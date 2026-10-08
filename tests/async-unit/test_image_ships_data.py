"""The images ship the repo-root data folder.

medicaid_api reads its Medicaid agency table from DATA_DIR (the repo-root
data folder next to the package), and process_denial reads the preventive-care
code lists from ./data. Both quietly fall back when the files are missing, so a
build that leaves the folder out passes every test and only shows in production
as a Medicaid lookup that always errors and preventive-care codes that never
match. These tests pin the copy in both Dockerfiles.
"""

import fnmatch
import posixpath
import sys
from pathlib import Path

from fighthealthinsurance import medicaid_api

REPO_DIR = Path(__file__).resolve().parent.parent.parent
WEB_DOCKERFILE = REPO_DIR / "k8s" / "Dockerfile"
RAY_DOCKERFILE = REPO_DIR / "k8s" / "ray" / "CombinedDockerfile"
REQUIRED_DATA_FILES = (
    medicaid_api.DEFAULT_FILE,
    "preventitivecodes.csv",
    "preventive_diagnosis.csv",
)


def _lines(path: Path) -> list[str]:
    return [" ".join(line.split()) for line in path.read_text().splitlines()]


def test_the_web_image_adds_the_data_folder_where_the_code_reads_it():
    assert (
        "ADD --chown=www-data:www-data data /opt/fighthealthinsurance/data"
        in _lines(WEB_DOCKERFILE)
    )


def test_the_ray_image_copies_the_data_folder_next_to_the_package():
    assert "COPY data ./fhi/data" in _lines(RAY_DOCKERFILE)


def test_data_dir_is_the_repo_root_data_folder():
    assert medicaid_api.DATA_DIR == REPO_DIR / "data"


def test_the_files_the_code_reads_are_in_the_data_folder():
    for name in REQUIRED_DATA_FILES:
        assert (REPO_DIR / "data" / name).is_file(), name


def _dockerignore_excludes(path: str) -> bool:
    """Whether .dockerignore leaves ``path`` out of the build context.

    Docker's rules: patterns are cleaned and anchored at the root, a
    pattern that matches a parent folder covers what is inside it, ``!``
    brings a path back, and the last matching line wins. fnmatch's ``*``
    also crosses ``/``, so this can only over-report an exclusion.
    """
    parts = path.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    excluded = False
    for line in (REPO_DIR / ".dockerignore").read_text().splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        keep = pattern.startswith("!")
        pattern = posixpath.normpath(pattern.lstrip("!").strip()).lstrip("/")
        if any(fnmatch.fnmatchcase(p, pattern) for p in prefixes):
            excluded = not keep
    return excluded


def test_dockerignore_keeps_the_files_the_code_reads():
    for name in REQUIRED_DATA_FILES:
        assert not _dockerignore_excludes(f"data/{name}"), name


def test_the_dockerignore_check_sees_a_broad_exclusion(tmp_path, monkeypatch):
    for rules in ("data/**", "*.csv", "data", "./data/", "da*"):
        (tmp_path / ".dockerignore").write_text(f"# rules\n{rules}\n")
        monkeypatch.setattr(sys.modules[__name__], "REPO_DIR", tmp_path)
        assert _dockerignore_excludes("data/preventive_diagnosis.csv"), rules


def test_the_dockerignore_check_honours_a_later_exception(tmp_path, monkeypatch):
    (tmp_path / ".dockerignore").write_text("data\n!data/preventive_diagnosis.csv\n")
    monkeypatch.setattr(sys.modules[__name__], "REPO_DIR", tmp_path)
    assert not _dockerignore_excludes("data/preventive_diagnosis.csv")

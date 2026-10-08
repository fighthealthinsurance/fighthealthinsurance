"""The images ship the repo-root data folder.

medicaid_api reads its Medicaid agency table from DATA_DIR (the repo-root
data folder next to the package), and process_denial reads the preventive-care
code lists from ./data. Both quietly fall back when the files are missing, so a
build that leaves the folder out passes every test and only shows in production
as a Medicaid lookup that always errors and preventive-care codes that never
match. These tests pin the copy in both Dockerfiles.
"""

from pathlib import Path

from fighthealthinsurance import medicaid_api

REPO_DIR = Path(__file__).resolve().parent.parent.parent
WEB_DOCKERFILE = REPO_DIR / "k8s" / "Dockerfile"
RAY_DOCKERFILE = REPO_DIR / "k8s" / "ray" / "CombinedDockerfile"


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
    for name in (
        medicaid_api.DEFAULT_FILE,
        "preventitivecodes.csv",
        "preventive_diagnosis.csv",
    ):
        assert (REPO_DIR / "data" / name).is_file(), name


def test_dockerignore_keeps_the_data_folder():
    ignored = {
        line.strip().rstrip("/")
        for line in (REPO_DIR / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert not ignored & {"data", "/data", "data/*", "**/data"}

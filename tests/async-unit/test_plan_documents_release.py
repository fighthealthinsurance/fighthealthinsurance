"""The pinned public plan-document corpus and its deploy path.

The corpus lives in fighthealthinsurance/public-plan-document-collection and
is published as GitHub releases. plan_documents.lock pins one release;
scripts/fetch_plan_documents.sh downloads and checks it at build time;
k8s/Dockerfile ships it in the web image; the web-plan-documents-import Job
(IMPORT_PLAN_DOCUMENTS in scripts/start-server.sh) imports it; and the
Update Plan Documents workflow moves the lock to the latest release
(docs/plan-document-corpus.md).

Every test here is hermetic: the fetch script reads a release served from a
temporary directory over file://, and the workflow's shell steps run against
a stand-in curl. Nothing reaches the network.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_DIR = Path(__file__).resolve().parent.parent.parent
LOCK = REPO_DIR / "plan_documents.lock"
FETCH = REPO_DIR / "scripts" / "fetch_plan_documents.sh"
BUILD = REPO_DIR / "scripts" / "build.sh"
START_SERVER = REPO_DIR / "scripts" / "start-server.sh"
DEPLOY = REPO_DIR / "k8s" / "deploy.yaml"
WORKFLOW = REPO_DIR / ".github" / "workflows" / "update-plan-documents.yml"
CORPUS_REPO = "fighthealthinsurance/public-plan-document-collection"
TAG = "corpus-20261107-abcdef0"
ZIP_BYTES = b"PK\x05\x06" + b"\x00" * 18  # an empty zip; the script never opens it


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _script_env(**extra: str) -> dict:
    # Only what the scripts need: no credentials and no inherited overrides
    # (tox passes the whole environment through).
    return {"PATH": os.environ["PATH"], "LC_ALL": "C", **extra}


# --- plan_documents.lock -------------------------------------------------


def test_the_lock_has_exactly_the_contract_keys():
    assert set(json.loads(LOCK.read_text())) == {"repo", "tag", "asset", "sha256"}


def test_the_lock_points_at_the_public_corpus_repo_and_its_zip():
    lock = json.loads(LOCK.read_text())
    assert (lock["repo"], lock["asset"]) == (CORPUS_REPO, "plan-docs.zip")


def test_a_pinned_tag_in_the_lock_carries_its_digest():
    lock = json.loads(LOCK.read_text())
    if lock["tag"]:
        assert re.fullmatch(r"corpus-[0-9]{8}-[0-9a-f]{7}", lock["tag"])
        assert re.fullmatch(r"[0-9a-f]{64}", lock["sha256"])
    else:
        assert lock["sha256"] == ""


def test_the_lock_is_in_the_format_the_workflow_writes():
    """The workflow rewrites the whole file; the same layout keeps its diff
    to the two lines that changed."""
    text = LOCK.read_text()
    assert text == json.dumps(json.loads(text), indent=2) + "\n"


def test_the_corpus_folder_exists_in_every_checkout():
    """k8s/Dockerfile ADDs plan_documents/, which fails the build if the
    folder is missing; the committed .gitkeep keeps it there with no corpus."""
    assert (REPO_DIR / "plan_documents" / ".gitkeep").is_file()


def test_git_ignores_the_fetched_zip():
    lines = (REPO_DIR / ".gitignore").read_text().splitlines()
    assert "/plan_documents/plan-docs.zip" in lines


# --- scripts/fetch_plan_documents.sh ------------------------------------


@pytest.fixture
def release(tmp_path):
    """A release served from a directory, as file://, in GitHub's layout."""
    root = tmp_path / "server"
    asset_dir = root / CORPUS_REPO / "releases" / "download" / TAG
    asset_dir.mkdir(parents=True)
    (asset_dir / "plan-docs.zip").write_bytes(ZIP_BYTES)
    return root


def _write_lock(path: Path, tag: str = TAG, sha256: str = _sha(ZIP_BYTES)) -> Path:
    lock = {"repo": CORPUS_REPO, "tag": tag, "asset": "plan-docs.zip", "sha256": sha256}
    path.write_text(json.dumps(lock))
    return path


def _fetch(lock: Path, dest: Path, base: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(FETCH), "--lock", str(lock), "--dest", str(dest)],
        env=_script_env(FHI_PLAN_DOCUMENTS_TEST_BASE_URL=base.as_uri()),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_fetch_script_is_executable():
    assert FETCH.stat().st_mode & stat.S_IXUSR


def test_an_empty_tag_fetches_nothing_and_succeeds(tmp_path, release):
    dest = tmp_path / "out" / "plan-docs.zip"
    result = _fetch(_write_lock(tmp_path / "lock", tag="", sha256=""), dest, release)
    assert (result.returncode, dest.exists()) == (0, False), result.stderr


def test_an_empty_tag_removes_a_zip_left_from_an_older_lock(tmp_path, release):
    dest = tmp_path / "plan-docs.zip"
    dest.write_bytes(b"an older corpus")
    _fetch(_write_lock(tmp_path / "lock", tag="", sha256=""), dest, release)
    assert not dest.exists()


def test_a_matching_digest_installs_the_zip(tmp_path, release):
    dest = tmp_path / "out" / "plan-docs.zip"
    result = _fetch(_write_lock(tmp_path / "lock"), dest, release)
    assert result.returncode == 0, result.stderr
    assert dest.read_bytes() == ZIP_BYTES


def test_a_wrong_digest_fails_and_installs_nothing(tmp_path, release):
    out = tmp_path / "out"
    dest = out / "plan-docs.zip"
    result = _fetch(_write_lock(tmp_path / "lock", sha256="0" * 64), dest, release)
    assert result.returncode != 0
    # Not the file, and no half-written temp file beside it either.
    assert list(out.iterdir()) == []


def test_a_file_that_already_matches_is_not_downloaded_again(tmp_path):
    dest = tmp_path / "plan-docs.zip"
    dest.write_bytes(ZIP_BYTES)
    empty_server = tmp_path / "empty"  # a download from here would fail
    empty_server.mkdir()
    result = _fetch(_write_lock(tmp_path / "lock"), dest, empty_server)
    assert (result.returncode, dest.read_bytes()) == (0, ZIP_BYTES), result.stderr


def test_a_stale_file_is_removed_even_when_the_download_fails(tmp_path):
    """The build must never go on to ship a corpus other than the pinned one."""
    dest = tmp_path / "plan-docs.zip"
    dest.write_bytes(b"an older corpus")
    empty_server = tmp_path / "empty"
    empty_server.mkdir()
    result = _fetch(_write_lock(tmp_path / "lock"), dest, empty_server)
    assert (result.returncode != 0, dest.exists()) == (True, False)


def test_a_tag_without_a_digest_is_refused(tmp_path, release):
    dest = tmp_path / "plan-docs.zip"
    result = _fetch(_write_lock(tmp_path / "lock", sha256=""), dest, release)
    assert (result.returncode != 0, dest.exists()) == (True, False)


def test_a_lock_with_a_path_in_the_tag_is_refused(tmp_path, release):
    dest = tmp_path / "plan-docs.zip"
    result = _fetch(_write_lock(tmp_path / "lock", tag="../../evil"), dest, release)
    assert result.returncode != 0


def test_the_test_override_accepts_only_file_urls(tmp_path):
    """So it can never point a build at another web host. Refused before any
    request is made."""
    result = subprocess.run(
        ["bash", str(FETCH), "--lock", str(_write_lock(tmp_path / "lock"))],
        env=_script_env(FHI_PLAN_DOCUMENTS_TEST_BASE_URL="https://example.invalid"),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2


def test_real_downloads_are_https_only_redirects_included():
    text = FETCH.read_text()
    assert 'BASE_URL="https://github.com"' in text
    assert 'CURL_PROTO=(--proto "=https" --proto-redir "=https")' in text


# --- the build and the deploy --------------------------------------------


def test_build_fetches_the_corpus_inside_the_build_branch():
    """Not after the `fi`: a --no-build host has neither the network nor
    the file, and needs neither."""
    text = BUILD.read_text()
    branch = text[
        text.index('source "${SCRIPT_DIR}/setup_templates.sh"') : text.index(
            "FHI_VERSION="
        )
    ]
    fetch = branch.index('"${SCRIPT_DIR}/fetch_plan_documents.sh"')
    assert fetch < branch.index("\nfi\n")


def test_build_deletes_the_last_import_job_before_the_apply():
    text = BUILD.read_text()
    delete = text.index(
        "kubectl delete job web-plan-documents-import -n totallylegitco --ignore-not-found"
    )
    assert delete < text.index("envsubst < k8s/deploy.yaml | kubectl apply -f -")


def _import_job() -> dict:
    raw = DEPLOY.read_text().replace("${FHI_BASE}:${FHI_VERSION}", "image")
    return next(
        doc
        for doc in yaml.safe_load_all(raw)
        if doc
        and doc["kind"] == "Job"
        and doc["metadata"]["name"] == "web-plan-documents-import"
    )


def test_the_import_job_has_its_bounds():
    spec = _import_job()["spec"]
    assert (
        spec["backoffLimit"],
        spec["activeDeadlineSeconds"],
        spec["ttlSecondsAfterFinished"],
    ) == (1, 5400, 3600)


def test_the_import_job_selects_the_import_branch():
    container = _import_job()["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["IMPORT_PLAN_DOCUMENTS"] == "1"


def test_the_import_job_has_a_writable_tmp_on_a_read_only_root():
    pod = _import_job()["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert {"name": "tmp", "mountPath": "/tmp"} in container["volumeMounts"]
    assert any(v["name"] == "tmp" and "emptyDir" in v for v in pod["volumes"])


def _import_branch() -> str:
    text = START_SERVER.read_text()
    start = text.index('elif [ -n "$IMPORT_PLAN_DOCUMENTS" ]; then')
    return text[start : text.index("\nelif ", start + 1)]


def test_start_server_imports_the_zip_as_the_complete_corpus():
    assert (
        "python manage.py import_plan_documents "
        '--source "$PLAN_DOCUMENTS_ZIP" --store-file --deactivate-missing --complete'
    ) in _import_branch()


def test_start_server_skips_the_import_with_success_when_there_is_no_zip():
    branch = _import_branch()
    absent = branch[branch.index('if [ ! -f "$PLAN_DOCUMENTS_ZIP" ]') :]
    assert absent.index("exit 0") < absent.index("fi")


def test_start_server_waits_for_migrations_then_loads_insurers_before_importing():
    branch = _import_branch()
    order = [
        branch.index("until python manage.py migrate --check"),
        branch.index("python manage.py loaddata plan_source insurance_companies"),
        branch.index("python manage.py import_plan_documents"),
    ]
    assert order == sorted(order)


def test_a_failed_insurer_loaddata_is_retried_in_the_wait_loop():
    # It can race web-migrations' identical loaddata; under set -e a bare
    # command there would fail the Job instead of trying again.
    branch = _import_branch()
    condition = branch[
        branch.index("until python manage.py migrate --check") : branch.index(
            "; do"
        )
    ]
    assert "&&" in condition
    assert "python manage.py loaddata plan_source insurance_companies" in condition


def test_the_migrations_wait_is_bounded():
    branch = _import_branch()
    loop = branch[branch.index("until python manage.py migrate --check") :]
    assert re.search(r"-ge \d+ \]; then\s+echo [^\n]*\n\s+exit 1", loop)


# --- .github/workflows/update-plan-documents.yml -------------------------


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps() -> list:
    return _workflow()["jobs"]["update-plan-documents"]["steps"]


def test_the_workflow_runs_monthly_and_on_demand():
    triggers = _workflow()[True]  # YAML 1.1 reads the bare key `on` as True
    (cron,) = [s["cron"] for s in triggers["schedule"]]
    minute, hour, day, month, weekday = cron.split()
    assert (month, weekday, "workflow_dispatch" in triggers) == ("*", "*", True)
    assert day.isdigit()
    # A jittered minute, not the top or bottom of the hour.
    assert int(minute) not in (0, 30)


def test_no_run_script_interpolates_an_expression():
    """Release fields (and everything else) reach the shell through env:
    only, so a crafted tag or name can never become shell code."""
    for step in _steps():
        if "run" in step:
            assert "${{" not in step["run"], step["name"]


def test_the_pull_request_carries_only_the_lock():
    (pr,) = [s for s in _steps() if "create-pull-request" in s.get("uses", "")]
    assert pr["with"]["add-paths"].split() == ["plan_documents.lock"]
    assert pr["with"]["branch"] == "chore/update-plan-documents"
    assert pr["with"]["token"] == (
        "${{ secrets.BROWSERSLIST_UPDATE_TOKEN || github.token }}"
    )


def test_the_checkout_does_not_persist_credentials():
    (checkout,) = [s for s in _steps() if s.get("uses", "").startswith("actions/checkout")]
    assert checkout["with"]["persist-credentials"] is False


FAKE_CURL = r"""#!/bin/bash
# Serves URLs from $SRV/<url without the scheme>; never touches the network.
out=""; w=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -w) w="$2"; shift ;;
    -H|--proto|--proto-redir|--connect-timeout|--max-time|--retry|--retry-delay|--max-filesize) shift ;;
    -*) ;;
    *) url="$1" ;;
  esac
  shift
done
file="$SRV/${url#https://}"
if [ -f "$file" ]; then
  cp "$file" "$out"
  [ -z "$w" ] || printf 200
  exit 0
fi
if [ -n "$w" ]; then printf 404; exit 0; fi
exit 22
"""


@pytest.fixture
def workflow_run(tmp_path):
    """The workflow's shell steps in a scratch checkout, with a stand-in curl
    serving the GitHub API and release downloads from a directory."""
    srv = tmp_path / "srv"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o755)
    work = tmp_path / "work"
    (work / "scripts").mkdir(parents=True)
    _write_lock(work / "plan_documents.lock", tag="", sha256="")
    (work / "scripts" / "fetch_plan_documents.sh").write_text(FETCH.read_text())
    (work / "scripts" / "fetch_plan_documents.sh").chmod(0o755)
    output = tmp_path / "github_output"
    output.write_text("")

    def publish(tag: str, assets=("plan-docs.zip", "plan-docs.zip.sha256")) -> None:
        api = srv / "api.github.com" / "repos" / CORPUS_REPO / "releases"
        api.mkdir(parents=True, exist_ok=True)
        release = {"tag_name": tag, "name": "$(touch pwned)", "body": "`touch pwned`"}
        release["assets"] = [{"name": a} for a in assets]
        (api / "latest").write_text(json.dumps(release))
        download = srv / "github.com" / CORPUS_REPO / "releases" / "download" / tag
        download.mkdir(parents=True, exist_ok=True)
        (download / "plan-docs.zip").write_bytes(ZIP_BYTES)
        (download / "plan-docs.zip.sha256").write_text(
            f"{_sha(ZIP_BYTES)}  plan-docs.zip\n"
        )

    def run(step_name: str, **env: str) -> subprocess.CompletedProcess:
        (step,) = [s for s in _steps() if s.get("name") == step_name]
        return subprocess.run(
            ["bash", "-e", "-c", step["run"]],
            cwd=work,
            env=_script_env(
                PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                SRV=str(srv),
                GITHUB_OUTPUT=str(output),
                GH_TOKEN="not-a-real-token",
                RELEASE_DIR=str(tmp_path / "release"),
                **env,
            ),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def outputs() -> dict:
        lines = output.read_text().splitlines()
        return dict(line.split("=", 1) for line in lines if line)

    return publish, run, outputs, work


READ_STEP = "Read the latest corpus release"
VERIFY_STEP = "Take the release's digest and verify plan-docs.zip against it"


def test_the_workflow_pins_a_verified_new_release(workflow_run):
    publish, run, outputs, work = workflow_run
    publish(TAG)
    assert run(READ_STEP).returncode == 0
    found = outputs()
    result = run(VERIFY_STEP, REPO=found["repo"], TAG=found["tag"])
    assert result.returncode == 0, result.stderr
    assert json.loads((work / "plan_documents.lock").read_text()) == {
        "repo": CORPUS_REPO,
        "tag": TAG,
        "asset": "plan-docs.zip",
        "sha256": _sha(ZIP_BYTES),
    }


def test_the_workflow_refuses_a_tag_outside_the_corpus_format(workflow_run):
    publish, run, outputs, work = workflow_run
    publish("corpus-20261107-abcdef0;touch pwned")
    result = run(READ_STEP)
    assert (result.returncode != 0, "tag" in outputs()) == (True, False)
    assert not (work / "pwned").exists()


def test_the_workflow_refuses_a_release_missing_an_asset(workflow_run):
    publish, run, outputs, _ = workflow_run
    publish(TAG, assets=("plan-docs.zip",))
    assert run(READ_STEP).returncode != 0


def test_the_workflow_is_a_no_op_without_any_release(workflow_run):
    _, run, outputs, _ = workflow_run
    result = run(READ_STEP)
    assert (result.returncode, outputs()) == (0, {"found": "false"}), result.stderr


def test_the_workflow_leaves_the_lock_alone_when_it_already_pins_the_latest(
    workflow_run,
):
    publish, run, outputs, work = workflow_run
    publish(TAG)
    _write_lock(work / "plan_documents.lock")
    before = (work / "plan_documents.lock").read_text()
    result = run(VERIFY_STEP, REPO=CORPUS_REPO, TAG=TAG)
    assert (result.returncode, "sha256" in outputs()) == (0, False), result.stderr
    assert (work / "plan_documents.lock").read_text() == before


def test_the_workflow_refuses_a_zip_that_does_not_match_the_published_digest(
    workflow_run,
):
    publish, run, outputs, work = workflow_run
    publish(TAG)
    download = (
        work.parent / "srv" / "github.com" / CORPUS_REPO / "releases" / "download" / TAG
    )
    (download / "plan-docs.zip").write_bytes(b"tampered")
    result = run(VERIFY_STEP, REPO=CORPUS_REPO, TAG=TAG)
    assert result.returncode != 0
    assert json.loads((work / "plan_documents.lock").read_text())["tag"] == ""

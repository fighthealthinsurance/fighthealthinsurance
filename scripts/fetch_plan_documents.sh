#!/bin/bash
# Fetch the public plan-document corpus that plan_documents.lock pins.
#
# The corpus (insurers' public plan documents: EOCs, SBCs, formularies) lives
# in its own public repository, fighthealthinsurance/public-plan-document-collection,
# which publishes each version as a GitHub release with one zip bundle in
# FHI's import format. plan_documents.lock at the repo root names the release
# this checkout ships:
#
#   {"repo": "<owner>/<name>", "tag": "corpus-YYYYMMDD-<sha7>",
#    "asset": "plan-docs.zip", "sha256": "<64 hex>"}
#
# This script downloads that asset, checks it against the pinned sha256 and
# installs it at plan_documents/plan-docs.zip (gitignored). scripts/build.sh
# runs it before the image build; k8s/Dockerfile copies plan_documents/ into
# the web image; the web-plan-documents-import Job (k8s/deploy.yaml,
# IMPORT_PLAN_DOCUMENTS in scripts/start-server.sh) imports it.
# docs/plan-document-corpus.md has the whole story.
#
#   * An empty tag means no corpus is pinned yet: nothing is downloaded, a
#     stale plan-docs.zip from an older lock is removed, and the script exits
#     0. The import Job then logs that there is nothing to import.
#   * A file already at the destination with the pinned digest is kept and
#     nothing is downloaded, so re-running is free.
#   * Bytes that fail verification are never installed, and a pre-existing
#     file that does not match the lock is removed first, so the image never
#     carries a corpus other than the pinned one. Any failure exits non-zero:
#     the build must not go on with the wrong corpus.
#   * The download is https only, redirects included, and capped at 2 GiB
#     (the importer's MAX_ARCHIVE_PATH_BYTES).
#
# Usage:
#   fetch_plan_documents.sh [--lock PATH] [--dest PATH]
#
#   --lock PATH   The lock file (default: plan_documents.lock in the repo).
#   --dest PATH   Where to install (default: plan_documents/plan-docs.zip).
#
# Environment (TESTS ONLY, never in a build):
#   FHI_PLAN_DOCUMENTS_TEST_BASE_URL  A file:// URL used in place of
#       https://github.com, so the tests can serve a release from a temporary
#       directory without the network. Only file:// is accepted, and it turns
#       https off for that run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"

LOCK="${REPO_ROOT}/plan_documents.lock"
DEST="${REPO_ROOT}/plan_documents/plan-docs.zip"
MAX_BYTES=2147483648 # 2 GiB, plan_document_bundle.MAX_ARCHIVE_PATH_BYTES

while [ $# -gt 0 ]; do
  case "$1" in
    --lock)
      shift
      [ $# -gt 0 ] || { echo "--lock needs a path" >&2; exit 2; }
      LOCK="$1"
      ;;
    --dest)
      shift
      [ $# -gt 0 ] || { echo "--dest needs a path" >&2; exit 2; }
      DEST="$1"
      ;;
    -h|--help)
      # The usage block from this file's own header comment.
      awk '/^# Usage:/,/^$/ {sub(/^# ?/, ""); print}' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

BASE_URL="https://github.com"
CURL_PROTO=(--proto "=https" --proto-redir "=https")
if [ -n "${FHI_PLAN_DOCUMENTS_TEST_BASE_URL:-}" ]; then
  case "${FHI_PLAN_DOCUMENTS_TEST_BASE_URL}" in
    file://*) ;;
    *)
      echo "FHI_PLAN_DOCUMENTS_TEST_BASE_URL must be a file:// URL (it is for tests only)" >&2
      exit 2
      ;;
  esac
  BASE_URL="${FHI_PLAN_DOCUMENTS_TEST_BASE_URL%/}"
  CURL_PROTO=(--proto "=file" --proto-redir "=file")
  echo "TEST ONLY: reading the release from ${BASE_URL}, not GitHub" >&2
fi

[ -f "${LOCK}" ] || { echo "No lock file at ${LOCK}" >&2; exit 1; }

# The lock's four fields, checked, one per line. -I so nothing in the
# current directory or the environment can change what python3 imports.
if ! FIELDS="$(python3 -I - "${LOCK}" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as f:
    lock = json.load(f)
if not isinstance(lock, dict):
    sys.exit("the lock must be a JSON object")
rules = {
    "repo": r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
    "tag": r"(|[A-Za-z0-9][A-Za-z0-9._-]*)",
    "asset": r"[A-Za-z0-9][A-Za-z0-9._-]*",
    "sha256": r"(|[0-9a-fA-F]{64})",
}
for key, rule in rules.items():
    value = lock.get(key)
    if not isinstance(value, str) or not re.fullmatch(rule, value):
        sys.exit(f"the lock's {key!r} is missing or malformed")
extra = sorted(set(lock) - set(rules))
if extra:
    sys.exit(f"the lock has unknown keys: {extra}")
if lock["tag"] and not lock["sha256"]:
    sys.exit("the lock names a tag but no sha256; refusing an unverified corpus")
for key in rules:
    print(lock[key])
# A last line that is never empty, so an empty sha256 is not trimmed away
# by the command substitution and the reads below all succeed.
print("end")
PY
)"; then
  echo "Malformed lock file ${LOCK}" >&2
  exit 1
fi
{
  read -r REPO
  read -r TAG
  read -r ASSET
  read -r EXPECTED_SHA
} <<<"${FIELDS}"
EXPECTED_SHA="$(printf '%s' "${EXPECTED_SHA}" | tr '[:upper:]' '[:lower:]')"

if [ -z "${TAG}" ]; then
  echo "No plan-document corpus pinned in ${LOCK} (empty tag); nothing to fetch"
  if [ -e "${DEST}" ]; then
    echo "Removing ${DEST}, left from an older lock"
    rm -f "${DEST}"
  fi
  exit 0
fi

# sha256sum on Linux, shasum on macOS. Prints the bare digest for a file.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    echo "Neither sha256sum nor shasum is available; cannot verify the corpus" >&2
    return 1
  fi
}

if [ -f "${DEST}" ]; then
  ACTUAL_SHA="$(sha256_of "${DEST}")"
  if [ "${ACTUAL_SHA}" = "${EXPECTED_SHA}" ]; then
    echo "Plan-document corpus ${TAG} already present and verified: ${DEST}"
    exit 0
  fi
  echo "Existing ${DEST} does not match ${TAG}; removing it and downloading again"
  # Now, not after a successful download: if the download below fails, the
  # build must not go on to ship the old bytes.
  rm -f "${DEST}"
fi

URL="${BASE_URL}/${REPO}/releases/download/${TAG}/${ASSET}"
DEST_DIR="$(dirname "${DEST}")"
mkdir -p "${DEST_DIR}"
TMP_FILE="$(mktemp "${DEST_DIR}/.plan-docs.zip.XXXXXX")"
trap 'rm -f "${TMP_FILE}"' EXIT

echo "Downloading the plan-document corpus ${TAG} from ${URL}"
# Finite deadlines so a host that accepts the connection and then stalls
# fails the build instead of hanging it.
if ! curl -fsSL "${CURL_PROTO[@]}" --connect-timeout 10 --max-time 1800 \
  --retry 3 --retry-delay 5 --max-filesize "${MAX_BYTES}" \
  -o "${TMP_FILE}" "${URL}"; then
  echo "Download of ${URL} failed" >&2
  exit 1
fi

ACTUAL_SHA="$(sha256_of "${TMP_FILE}")"
if [ "${ACTUAL_SHA}" != "${EXPECTED_SHA}" ]; then
  echo "The downloaded corpus failed its checksum; refusing to install it" >&2
  echo "  expected ${EXPECTED_SHA}" >&2
  echo "  actual   ${ACTUAL_SHA}" >&2
  echo "  A release asset should never change; check ${REPO} release ${TAG}." >&2
  exit 1
fi

chmod 0644 "${TMP_FILE}"
mv "${TMP_FILE}" "${DEST}"
trap - EXIT
echo "Plan-document corpus ${TAG} installed: ${DEST}"

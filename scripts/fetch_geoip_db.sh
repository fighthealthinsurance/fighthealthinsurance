#!/bin/bash
# Fetch and verify the geoip2fast CITY database that FHI_GEOIP_CITY_DB points at.
#
# Why this exists: the pip package only bundles country/ASN databases, so
# guess_us_state (chat state hint) and get_asn_info soft-fail -- returning
# nothing and logging one startup warning -- until a city-level file is
# installed. This script is the single place that knows where that file comes
# from, so scripts/run_local.sh (dev) and k8s/Dockerfile (deployments) install
# the identical, identically-verified bytes.
#
# SECURITY: geoip2fast loads this file with pickle.load(), so whoever controls
# its bytes controls the web process -- which holds the DB credentials and the
# PHI field-encryption keys. Treat it as code, not data:
#   * the digest is pinned in scripts/geoip2fast-city-asn-ipv6.sha256,
#   * bytes that fail verification are never installed (and a pre-existing
#     copy that no longer matches is removed, so nothing downstream can
#     export a path to unverified bytes),
#   * with no digest pinned we install nothing at all unless a human passes
#     --allow-unverified, which is for a dev box and never for an image.
# A mismatch exits non-zero only under --require: like a failed download it
# otherwise leaves the app to soft-fail and warn, so a republished upstream
# LATEST asset degrades the feature instead of blocking every image build.
# Record the digest on a machine you trust with `--print-sha` and commit it;
# deriving it from a download this script just made verifies nothing.
#
# Usage:
#   fetch_geoip_db.sh [--dest PATH] [--require] [--allow-unverified]
#   fetch_geoip_db.sh --print-sha
#
#   --dest PATH          Where to install (default: geoip_data/ in the repo).
#   --require            Exit non-zero if the database was not installed.
#                        Use in a build that must not ship without GeoIP.
#   --allow-unverified   Install even with no pinned digest. Dev boxes only;
#                        also settable as GEOIP_ALLOW_UNVERIFIED=1.
#   --print-sha          Download to a temp file, print its sha256, and exit
#                        without installing anything.
#
# Environment overrides:
#   GEOIP2FAST_DB_URL     Where to download from (e.g. an internal mirror).
#   GEOIP2FAST_DB_SHA256  Expected digest; wins over the pinned file.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null || dirname "${SCRIPT_DIR}")"

DB_FILENAME="geoip2fast-city-asn-ipv6.dat.gz"
# geoip2fast publishes the databases under the moving LATEST tag -- that is the
# URL the package's own updater uses (GEOIP_UPDATE_DAT_URL in geoip2fast.py).
# The pinned digest, not the tag, is what makes a fetch reproducible here: a
# republished LATEST asset fails verification instead of landing silently.
DEFAULT_URL="https://github.com/rabuchaim/geoip2fast/releases/download/LATEST/${DB_FILENAME}"
DB_URL="${GEOIP2FAST_DB_URL:-${DEFAULT_URL}}"
SHA_FILE="${SCRIPT_DIR}/${DB_FILENAME%.dat.gz}.sha256"

DEST="${REPO_ROOT}/geoip_data/${DB_FILENAME}"
REQUIRE=false
ALLOW_UNVERIFIED="${GEOIP_ALLOW_UNVERIFIED:-0}"
PRINT_SHA=false

while [ $# -gt 0 ]; do
  case "$1" in
    --dest)
      shift
      [ $# -gt 0 ] || { echo "--dest needs a path" >&2; exit 2; }
      DEST="$1"
      ;;
    --require) REQUIRE=true ;;
    --allow-unverified) ALLOW_UNVERIFIED=1 ;;
    --print-sha) PRINT_SHA=true ;;
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

# sha256sum on Linux, shasum on macOS. Prints the bare digest for a file.
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    echo "Neither sha256sum nor shasum is available; cannot verify the GeoIP database" >&2
    return 1
  fi
}

# The pinned digest: first non-comment, non-empty token in the sha file.
pinned_sha() {
  [ -f "${SHA_FILE}" ] || return 0
  grep -v '^[[:space:]]*#' "${SHA_FILE}" | tr -s '[:space:]' '\n' | grep -m1 '^[0-9a-fA-F]\{64\}$' || true
}

download_to() {
  local target="$1"
  echo "Downloading GeoIP city database from ${DB_URL}"
  curl -fsSL --retry 3 --retry-delay 5 -o "${target}" "${DB_URL}"
}

# Normalize to lowercase: pinned_sha and the env override both accept
# uppercase hex (PowerShell/certutil emit it), but sha256sum prints lowercase
# and the comparisons below are string equality.
EXPECTED_SHA="$(printf '%s' "${GEOIP2FAST_DB_SHA256:-$(pinned_sha)}" | tr '[:upper:]' '[:lower:]')"

if [ "${PRINT_SHA}" = true ]; then
  TMP_SHA_FILE="$(mktemp)"
  trap 'rm -f "${TMP_SHA_FILE}"' EXIT
  download_to "${TMP_SHA_FILE}" || { echo "Download failed" >&2; exit 1; }
  echo "sha256 of ${DB_URL}:"
  sha256_of "${TMP_SHA_FILE}"
  echo "Record this in ${SHA_FILE} only if this machine and network are ones you trust."
  exit 0
fi

not_installed() {
  # $1: why. Loud, actionable, and fatal only under --require, so an
  # unpinned build still produces an image (the app soft-fails and warns).
  echo "GeoIP city database NOT installed: $1" >&2
  echo "  Chat state guessing and ASN lookups will return nothing." >&2
  echo "  See the README 'GeoIP' section to pin a digest and enable them." >&2
  if [ "${REQUIRE}" = true ]; then
    exit 1
  fi
  exit 0
}

if [ -z "${EXPECTED_SHA}" ] && [ "${ALLOW_UNVERIFIED}" != "1" ]; then
  not_installed "no digest is pinned in ${SHA_FILE} (record one with '$0 --print-sha')"
fi

# Already installed and matching: nothing to do. Re-running is free.
if [ -f "${DEST}" ]; then
  if [ -z "${EXPECTED_SHA}" ]; then
    echo "GeoIP city database already present (unverified): ${DEST}"
    exit 0
  fi
  ACTUAL_SHA="$(sha256_of "${DEST}")" || exit 1
  if [ "${ACTUAL_SHA}" = "${EXPECTED_SHA}" ]; then
    echo "GeoIP city database already installed and verified: ${DEST}"
    exit 0
  fi
  echo "Existing ${DEST} does not match the pinned digest; removing and re-downloading" >&2
  # Remove it NOW, not after a successful re-download: if the download below
  # fails, leaving the mismatching file behind would let run_local.sh's
  # file-exists check export FHI_GEOIP_CITY_DB for bytes that just failed
  # verification -- the exact pickle.load() path the pin exists to block.
  rm -f "${DEST}" || exit 1
fi

mkdir -p "$(dirname "${DEST}")" || exit 1
TMP_FILE="$(mktemp "$(dirname "${DEST}")/.geoip2fast.XXXXXX")" || exit 1
trap 'rm -f "${TMP_FILE}"' EXIT

if ! download_to "${TMP_FILE}"; then
  not_installed "download from ${DB_URL} failed"
fi

if [ -n "${EXPECTED_SHA}" ]; then
  ACTUAL_SHA="$(sha256_of "${TMP_FILE}")" || exit 1
  if [ "${ACTUAL_SHA}" != "${EXPECTED_SHA}" ]; then
    echo "GeoIP database failed checksum -- refusing to install" >&2
    echo "  expected ${EXPECTED_SHA}" >&2
    echo "  actual   ${ACTUAL_SHA}" >&2
    echo "  Upstream publishes under the moving LATEST tag, so this usually" >&2
    echo "  means they republished the asset; if you deliberately want the" >&2
    echo "  new release, re-pin ${SHA_FILE}." >&2
    rm -f "${TMP_FILE}"
    # Same exit contract as a failed download: the unverified bytes are gone
    # either way, and only --require turns "no database" into a hard failure.
    not_installed "downloaded file failed sha256 verification"
  fi
else
  echo "WARNING: installing an UNVERIFIED GeoIP database (--allow-unverified)." >&2
  echo "         Never do this for an image or anything that reaches a server." >&2
fi

# Read-only for everyone but the owner: the web process only ever reads it,
# and geoip2fast unpickles it, so a writable path is a code-execution path.
chmod 0644 "${TMP_FILE}" || exit 1
mv "${TMP_FILE}" "${DEST}" || exit 1
trap - EXIT
echo "GeoIP city database installed: ${DEST}"

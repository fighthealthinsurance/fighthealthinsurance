#!/bin/bash
# Ensure Git LFS-tracked assets hold real bytes before anything copies them.
#
# collectstatic (and the Docker ADD that ships its output) copies raw file bytes,
# so an LFS-tracked asset left as a ~130-byte pointer stub would be published as a
# broken image. This checks the postcondition -- "is any LFS-tracked file still a
# pointer stub?" -- rather than inferring from the environment. That way a source
# tarball with real bytes and no .git directory passes cleanly, a checkout that was
# never smudged gets repaired, and a file that `git lfs pull` could not replace
# (e.g. it is locally modified, which pull skips while still exiting 0) is still
# caught instead of silently shipping.
#
# Usage: ensure_lfs_assets.sh [--warn-only]
#   --warn-only  Warn and exit 0 instead of failing, for local/dev builds.

set -u

WARN_ONLY=false
if [ "${1:-}" = "--warn-only" ]; then
  WARN_ONLY=true
fi

# Resolve the repo root from THIS script's location, not the caller's cwd: the
# callers below invoke it from wherever the build happens to be, and in a tree
# with no .git (a source tarball) a cwd-based fallback would look for
# .gitattributes in the wrong place and silently skip validation.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel 2>/dev/null || dirname "${SCRIPT_DIR}")"
GITATTRIBUTES="${REPO_ROOT}/.gitattributes"
LFS_MAGIC="version https://git-lfs.github.com/spec/v1"

# Nothing is tracked by LFS -> nothing to do.
if [ ! -f "${GITATTRIBUTES}" ] || ! grep -q "filter=lfs" "${GITATTRIBUTES}"; then
  exit 0
fi

# Print every LFS-tracked path that is still an unmaterialized pointer stub.
unmaterialized_lfs_files() {
  local pattern path
  while read -r pattern _; do
    # Unquoted on purpose so the .gitattributes pattern globs. A pattern that
    # matches nothing stays literal and is skipped by the -f test below.
    # shellcheck disable=SC2086
    for path in ${REPO_ROOT}/${pattern}; do
      if [ -f "${path}" ] && head -c "${#LFS_MAGIC}" "${path}" | grep -qF "${LFS_MAGIC}"; then
        echo "${path}"
      fi
    done
  done < <(grep "filter=lfs" "${GITATTRIBUTES}")
}

PENDING="$(unmaterialized_lfs_files)"

# Materialize them when this is a git checkout that has git-lfs available.
if [ -n "${PENDING}" ] && command -v git-lfs >/dev/null 2>&1 &&
  git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "${REPO_ROOT}" lfs pull || true
  PENDING="$(unmaterialized_lfs_files)"
fi

if [ -z "${PENDING}" ]; then
  exit 0
fi

echo "Git LFS assets are still pointer stubs and would ship as broken files:" >&2
while IFS= read -r stub; do
  echo "       ${stub}" >&2
done <<<"${PENDING}"
if command -v git-lfs >/dev/null 2>&1; then
  echo "       Try 'git lfs pull' in ${REPO_ROOT}, and check for local modifications." >&2
else
  echo "       git-lfs is not installed; install it and run 'git lfs pull'." >&2
fi

if [ "${WARN_ONLY}" = true ]; then
  echo "WARNING: continuing anyway -- these images may render broken." >&2
  exit 0
fi
exit 1

#!/bin/bash
# SessionStart hook for Claude Code on the web.
#
# Prepares the container so tests, linters, and the dev server work without a
# per-session cold install:
#   - installs the Python test/lint toolchain (tox),
#   - pre-builds the tox environments for the most-used suites in parallel,
#   - installs the JS deps concurrently with the tox builds,
#   - warms the webpack + collectstatic build so the first test run (and
#     manual server runs) skip it.
# Runs synchronously so the session only starts once deps are ready. Re-runs
# (session resume, /clear, compact) skip all of it via a checksum stamp, the
# same pattern scripts/run_local.sh uses for requirements.
#
# Local (non-web) development is unchanged: it uses the conda/venv setup in
# CLAUDE.md, so this hook no-ops outside the remote container.
set -euo pipefail

# Only run inside Claude Code on the web (remote) containers.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Envs pre-built for the suites used most (see CLAUDE.md). The selenium,
# sync-actor, and temporal envs are skipped to keep session start fast; the
# first `tox -e` on one of those pays a one-time env install instead.
TOX_ENVS="py313-django52-sync,py313-django52-async,py313-django52-async-unit,py313-black,mypy"

# Everything the install work depends on, hashed into a stamp. A matching
# stamp means the previous run's tox envs / node_modules are still valid, so
# hook re-runs can skip straight to done. The hook file itself is included so
# editing the hook invalidates the stamp.
STAMP_FILE=".tox/.fhi_session_start_stamp"
current_stamp() {
  md5sum requirements.txt requirements-dev.txt tox.ini \
    fighthealthinsurance/static/js/package-lock.json \
    "${BASH_SOURCE[0]}" 2>/dev/null | md5sum | cut -d ' ' -f 1
}

persist_path() {
  # Persist tox on PATH for the session (guard against duplicate appends on
  # resume/clear/compact re-runs of the hook).
  if [ -n "${CLAUDE_ENV_FILE:-}" ] && ! grep -qs 'fhi-session-start-path' "$CLAUDE_ENV_FILE"; then
    {
      echo '# fhi-session-start-path'
      # shellcheck disable=SC2016  # literal on purpose: expands when the env file is sourced
      echo 'export PATH="$HOME/.local/bin:$PATH"'
    } >> "$CLAUDE_ENV_FILE"
  fi
}

if [ -f "$STAMP_FILE" ] \
    && [ "$(cat "$STAMP_FILE")" = "$(current_stamp)" ] \
    && [ -x "$HOME/.local/bin/tox" ] \
    && [ -d fighthealthinsurance/static/js/node_modules ]; then
  persist_path
  echo "FHI deps already prepared (stamp match); skipping session-start install."
  exit 0
fi

# Trust the agent proxy CA for pip/git when it is present (no-op elsewhere).
if [ -f /root/.ccr/ca-bundle.crt ]; then
  export PIP_CERT="/root/.ccr/ca-bundle.crt"
  export GIT_SSL_CAINFO="/root/.ccr/ca-bundle.crt"
fi

PYTHON="$(command -v python3.13 || command -v python3)"

# 1. Install tox into the user site (idempotent; reuses cached wheels).
"$PYTHON" -m pip install --user --upgrade pip
"$PYTHON" -m pip install --user tox
export PATH="$HOME/.local/bin:$PATH"

# .tox holds the stamp and the log files below; tox itself is fine with the
# directory already existing.
mkdir -p .tox

# 2. Install JS deps in the background while the tox envs build; the webpack
#    build (scripts/test_setup.sh, run by the async/selenium/actor test envs
#    and for manual verification) needs them. Output goes to a log so it
#    doesn't interleave with the parallel tox output.
NPM_LOG=".tox/fhi-npm-install.log"
npm_pid=""
if command -v npm >/dev/null 2>&1; then
  (cd fighthealthinsurance/static/js && npm install --no-audit --no-fund) \
    > "$NPM_LOG" 2>&1 &
  npm_pid=$!
fi

# 3. Pre-build the tox environments the tests/linters use, in parallel.
#    `--notest` installs every dependency into .tox/<env> but skips running
#    the suites, so the session's `tox -e ...` reuses the env and starts
#    fast. Note: the type-check env is `mypy` (the `py313-mypy` env resolves
#    to an empty command list in tox 4, so it runs nothing).
if ! tox --notest -p auto --parallel-no-spinner -e "$TOX_ENVS"; then
  echo "ERROR: tox environment build failed." >&2
  echo "If the failure above is a 'git clone ... 403' for one of the" >&2
  echo "git+https dependencies in requirements.txt (static-thumbnails," >&2
  echo "django-cookie-consent, django-encrypted-model-fields, llm-result-utils)," >&2
  echo "this environment's network policy is blocking GitHub egress for those" >&2
  echo "repos. Grant the web environment access to them (or vendor them)." >&2
  exit 1
fi

# Reap the npm install started in step 2.
if [ -n "$npm_pid" ] && ! wait "$npm_pid"; then
  echo "ERROR: npm install failed:" >&2
  cat "$NPM_LOG" >&2
  exit 1
fi

# 4. Warm the webpack + collectstatic/compress build using one of the tox
#    envs built above (build_static.sh needs Django on PATH for manage.py).
#    This fills the checksum caches build_static.sh keys off, so the
#    async/selenium/actor test envs' scripts/test_setup.sh step and manual
#    server runs skip the whole static build. Best-effort: a failure here
#    leaves the static build to the first test run instead of blocking the
#    session.
STATIC_LOG=".tox/fhi-static-warm.log"
if [ -x .tox/py313-django52-async/bin/python ]; then
  if PATH="$PWD/.tox/py313-django52-async/bin:$PATH" ./scripts/build_static.sh \
      > "$STATIC_LOG" 2>&1; then
    echo "Static build warmed (webpack + collectstatic cached for test runs)."
  else
    tail -n 20 "$STATIC_LOG" >&2 || true
    echo "WARNING: static build warm-up failed (see $STATIC_LOG); the first" >&2
    echo "test run will rebuild static assets instead." >&2
  fi
fi

persist_path
current_stamp > "$STAMP_FILE" || true
echo "Fight Health Insurance session-start hook complete in ${SECONDS}s."

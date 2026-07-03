#!/bin/bash
# SessionStart hook for Claude Code on the web.
#
# Installs the Python test/lint toolchain (tox) and pre-builds the tox
# environments used by this repo's tests and linters, plus the JS deps, so
# `tox -e ...`, black, and mypy work without a per-session cold install.
# Runs synchronously so the session only starts once deps are ready.
#
# Local (non-web) development is unchanged: it uses the conda/venv setup in
# CLAUDE.md, so this hook no-ops outside the remote container.
set -euo pipefail

# Only run inside Claude Code on the web (remote) containers.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

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

# 2. Pre-build the tox environments the tests/linters use. `--notest` installs
#    every dependency into .tox/<env> (cached with the container image) but
#    skips running the suites, so the session's `tox -e ...` reuses the env
#    and starts fast. Note: the type-check env is `mypy` (the `py313-mypy`
#    env resolves to an empty command list in tox 4, so it runs nothing).
#    Add -e py313-django52-async / -selenium here if you want those cached too.
if ! tox --notest \
  -e py313-django52-sync,py313-django52-async-unit,py313-black,mypy; then
  echo "ERROR: tox environment build failed." >&2
  echo "If the failure above is a 'git clone ... 403' for one of the" >&2
  echo "git+https dependencies in requirements.txt (static-thumbnails," >&2
  echo "django-cookie-consent, django-encrypted-model-fields, llm-result-utils)," >&2
  echo "this environment's network policy is blocking GitHub egress for those" >&2
  echo "repos. Grant the web environment access to them (or vendor them)." >&2
  exit 1
fi

# 3. Install JS deps so the webpack build (scripts/test_setup.sh, run by the
#    async/selenium/actor test envs and for manual verification) works.
if command -v npm >/dev/null 2>&1; then
  (cd fighthealthinsurance/static/js && npm install --no-audit --no-fund)
fi

# 4. Persist tox on PATH for the session (guard against duplicate appends on
#    resume/clear/compact re-runs of the hook).
if [ -n "${CLAUDE_ENV_FILE:-}" ] && ! grep -qs 'fhi-session-start-path' "$CLAUDE_ENV_FILE"; then
  {
    echo '# fhi-session-start-path'
    # shellcheck disable=SC2016  # literal on purpose: expands when the env file is sourced
    echo 'export PATH="$HOME/.local/bin:$PATH"'
  } >> "$CLAUDE_ENV_FILE"
fi

echo "Fight Health Insurance session-start hook complete."

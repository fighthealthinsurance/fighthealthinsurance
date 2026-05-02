#!/bin/bash
# Set up the database for CI async tests.
# Retries uv pip install on transient network errors.

set -e

# Retry helper: runs a command up to max_attempts times with exponential backoff.
retry() {
  local max_attempts=3
  local attempt=1
  until "$@"; do
    if [ "${attempt}" -ge "${max_attempts}" ]; then
      echo "Command failed after ${max_attempts} attempts: $*" >&2
      return 1
    fi
    echo "Attempt ${attempt} failed. Waiting before retry..." >&2
    sleep $((attempt * 5))
    attempt=$((attempt + 1))
  done
}

uv venv
# shellcheck disable=SC1091
source .venv/bin/activate
retry uv pip install -r requirements.txt
python manage.py makemigrations
python manage.py migrate
ENVIRONMENT=Test ./manage.py migrate

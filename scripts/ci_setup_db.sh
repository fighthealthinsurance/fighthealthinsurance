#!/bin/bash
# Set up the database for CI async tests.
# Retries uv pip install on transient network errors.

set -e

SCRIPT_DIR="$(dirname "$0")"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/lib/retry.sh"

uv venv
# shellcheck disable=SC1091
source .venv/bin/activate
retry uv pip install -r requirements.txt
python manage.py makemigrations
python manage.py migrate
ENVIRONMENT=Test ./manage.py migrate

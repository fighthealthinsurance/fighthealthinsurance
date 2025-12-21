#!/bin/bash
set -ex

pwd

# Activate the venv if present.
if [ -f ./build_venv/bin/activate ]; then
  source ./build_venv/bin/activate
elif [ -f ./.venv/bin/activate ]; then
  source ./.venv/bin/activate
fi

if command -v tox >/dev/null 2>&1; then
  # Check if fhi_users directory exists for ty
  if [ -d "./fhi_users" ]; then
    # Try interpreter-matched ty first (py310-ty/py311-ty/py312-ty), then generic, then plain ty.
    ENV_NAME="py$(python - <<'PY'
import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")
PY
)-ty"
    tox -vv -e "${ENV_NAME}" || tox -vv -e ty || ty check fighthealthinsurance fhi_users
  else
    # fhi_users not present, only check fighthealthinsurance
    ENV_NAME="py$(python - <<'PY'
import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")
PY
)-ty"
    tox -vv -e "${ENV_NAME}" || tox -vv -e ty || ty check fighthealthinsurance
  fi
else
  # Direct ty call - check for fhi_users
  if [ -d "./fhi_users" ]; then
    ty check fighthealthinsurance fhi_users
  else
    ty check fighthealthinsurance
  fi
fi

./manage.py makemigrations --check || (./manage.py makemigrations && ./manage.py migrate)
./manage.py validate_templates &
./manage.py collectstatic --no-input &
wait

pushd ./static/js
npm i
npm run build
popd


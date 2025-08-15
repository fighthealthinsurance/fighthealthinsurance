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
  # Check if fhi_users directory exists for mypy
  if [ -d "./fhi_users" ]; then
    # Try interpreter-matched mypy first (py310-mypy/py311-mypy/py312-mypy), then generic, then plain mypy.
    ENV_NAME="py$(python - <<'PY'
import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")
PY
)-mypy"
    tox -vv -e "${ENV_NAME}" || tox -vv -e mypy || mypy -p fighthealthinsurance -p fhi_users
  else
    # fhi_users not present, only check fighthealthinsurance
    ENV_NAME="py$(python - <<'PY'
import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")
PY
)-mypy"
    tox -vv -e "${ENV_NAME}" || tox -vv -e mypy || mypy -p fighthealthinsurance
  fi
else
  # Direct mypy call - check for fhi_users
  if [ -d "./fhi_users" ]; then
    mypy -p fighthealthinsurance -p fhi_users
  else
    mypy -p fighthealthinsurance
  fi
fi

./manage.py migrate
./manage.py makemigrations
./manage.py migrate
./manage.py validate_templates
./manage.py collectstatic --no-input

pushd ./static/js
npm i
npm run build
popd


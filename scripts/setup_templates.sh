#!/bin/bash
set -ex

pwd

# Activate the venv if present.
if [ -f ./build_venv/bin/activate ]; then
  source ./build_venv/bin/activate
elif [ -f ./.venv/bin/activate ]; then
  source ./.venv/bin/activate
fi

# Materialize Git LFS assets before collectstatic. A few outlet logos on the
# media-references page are stored via Git LFS (see .gitattributes). collectstatic
# copies raw bytes, so if these are still LFS pointer stubs (e.g. the checkout ran
# without git-lfs) the build would ship ~130-byte text files as .png. Smudge them
# to real files now, and fail loudly rather than silently shipping broken images.
if [ -f .gitattributes ] && grep -q "filter=lfs" .gitattributes; then
  if command -v git-lfs >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git lfs pull
  else
    echo "ERROR: this checkout uses Git LFS (see .gitattributes) but git-lfs is unavailable;" >&2
    echo "       logo images would ship as broken pointer files. Install git-lfs and re-run." >&2
    exit 1
  fi
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

./manage.py makemigrations --check || (./manage.py makemigrations && ./manage.py migrate)
./manage.py validate_templates &
./manage.py collectstatic --no-input &
wait

pushd ./static/js
npm i
npm run build
popd


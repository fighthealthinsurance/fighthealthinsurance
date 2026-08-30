#!/bin/bash
set -ex

pwd

# Shared collectstatic ignore list (node_modules, TS sources, build config).
source "$(dirname "${BASH_SOURCE[0]}")/collectstatic_ignores.sh"

# Activate the venv if present.
if [ -f ./build_venv/bin/activate ]; then
  source ./build_venv/bin/activate
elif [ -f ./.venv/bin/activate ]; then
  source ./.venv/bin/activate
fi

# Materialize Git LFS assets before collectstatic. A few outlet logos on the
# media-references page are stored via Git LFS (see .gitattributes), and
# collectstatic copies raw bytes, so an unsmudged pointer stub would ship as a
# broken .png. This is the deploy build, so fail rather than publish broken images.
"$(dirname "${BASH_SOURCE[0]}")/ensure_lfs_assets.sh"

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

# Build the JS bundles in the SOURCE tree before collectstatic, so the fresh
# dist/ gets collected. Building inside the collected static/js used to work
# only because collectstatic copied package.json and the .ts sources there;
# since collectstatic_ignores.sh stopped publishing those (no node_modules in
# the image), a fresh checkout has nothing to build in static/js. Same layout
# as scripts/ci_npm_build.sh and scripts/build_static.sh.
JS_PATH=fighthealthinsurance/static/js
pushd "${JS_PATH}"
npm i
npm run build
popd

./manage.py validate_templates &
./manage.py collectstatic --no-input "${COLLECTSTATIC_IGNORES[@]}" &
wait

